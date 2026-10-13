"""Real API + SQLite + Git; Docker/host gateway intentionally replaced by a boundary fake."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.manifest import load_manifest
from awesome_deploy.source import GitSource, _bounded_run
from awesome_deploy.worker import Worker


class HostBoundary:
    def __init__(self):
        self.active = None
        self.fail_verify = False
        self.cleaned = []

    def freeze(self, snapshot, job, checkpoint):
        assert job["app_id"] is None or isinstance(job["app_id"], str)
        assert len(snapshot["commit"]) == 40
        return snapshot

    def build(self, job, checkpoint):
        return {name: "sha256:" + "a" * 64 for name in job["snapshot"]["manifest"]["services"]}

    def start(self, job, checkpoint):
        assert job["app_id"] and job["execution"]["ports"]

    def health(self, job):
        pass

    def ensure_https(self, job):
        pass

    def publish(self, job):
        self.active = job["id"]

    def verify(self, job):
        if self.fail_verify:
            raise DeployError("PROBE_FAILED", "Candidate route verification failed")

    def rollback(self, job):
        self.active = job["execution"]["previous_release"]

    def cleanup(self, job):
        self.cleaned.append(job["id"])


def test_examples_match_schema():
    for file in Path("examples").glob("*/awesome.yaml"):
        result = load_manifest(file.read_text(), Settings())
        assert result["routes"]
        assert all(s["kind"] == "web" for s in result["services"].values())


def test_api_git_worker_first_deploy_update_and_failed_verify(tmp_path):
    local = tmp_path / "repo"
    local.mkdir()
    for file in Path("examples/simple-web").iterdir():
        (local / file.name).write_bytes(file.read_bytes())

    def git(*args):
        return subprocess.check_output(["git", "-C", str(local), *args], stderr=subprocess.PIPE)

    git("init", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    sha = git("rev-parse", "HEAD").decode().strip()
    auth = tmp_path / "admin.json"
    initialize_credentials(auth, "operator", "integration-password")
    settings = Settings(
        state_dir=tmp_path / "state",
        auth_file=auth,
        public_url="http://testserver",
        port_start=43000,
        port_end=43100,
    )
    remote = "https://github.com/team/project.git"

    def local_runner(args, **kwargs):
        # Only this explicit test transport may access file repositories.
        args = [
            "protocol.file.allow=always"
            if arg == "protocol.file.allow=never"
            else str(local)
            if arg == remote
            else arg
            for arg in args
        ]
        return _bounded_run(args, **kwargs)

    app = create_app(settings)
    host = HostBoundary()
    worker = Worker(settings, app.state.store, GitSource(settings, runner=local_runner), host)
    with TestClient(app) as client:
        body = {"repository": remote, "ref": "main", "subdomain": "demo"}

        def submit(key, payload):
            r = client.post(
                "/api/v1/deployments",
                json=payload,
                headers={"Idempotency-Key": key},
                auth=("operator", "integration-password"),
            )
            assert r.status_code == 202, r.text
            return r.json()["id"]

        first = submit("first", body)
        assert worker.run_once()
        first_job = app.state.store.get(first)
        assert first_job["status"] == "succeeded" and first_job["snapshot"]["commit"] == sha
        assert host.active == first
        assert submit("first", body) == first and not worker.run_once()
        update = {**body, "appId": first_job["app_id"]}
        second = submit("second", update)
        assert worker.run_once()
        assert host.active == second and first in host.cleaned
        host.fail_verify = True
        third = submit("third", update)
        assert worker.run_once()
        assert app.state.store.get(third)["status"] == "failed"
        assert host.active == second and third in host.cleaned
        assert app.state.store.apps()[0]["active_release_id"] == second
        result = client.get("/api/v1/deployments/" + second, auth=("operator", "integration-password")).json()
        assert result["url"] == "https://demo.awesome.guanghexinzhi.cn"
