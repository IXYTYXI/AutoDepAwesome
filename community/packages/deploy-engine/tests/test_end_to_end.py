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
        self.running = set()
        self.boot_id = "initial-boot"
        self.recovered = []

    def recovery_boot_id(self):
        return self.boot_id

    def recover_active(self, job, checkpoint):
        self.recovered.append(job["id"])
        if job["id"] in self.running:
            return {"restarted": []}
        self.running.add(job["id"])
        return {"restarted": list(job["snapshot"]["manifest"]["services"])}

    def freeze(self, snapshot, job, checkpoint):
        assert job["app_id"] is None or isinstance(job["app_id"], str)
        assert len(snapshot["commit"]) == 40
        return snapshot

    def build(self, job, checkpoint):
        return {name: "sha256:" + "a" * 64 for name in job["snapshot"]["manifest"]["services"]}

    def start(self, job, checkpoint):
        assert job["app_id"] and job["execution"]["ports"]
        self.running.add(job["id"])

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
        self.running.discard(job["id"])


def test_examples_match_schema():
    for file in Path("examples").glob("*/awesome.yaml"):
        result = load_manifest(file.read_text(), Settings())
        assert result["routes"]
        assert all(s["kind"] == "web" for s in result["services"].values())


def test_api_git_worker_first_deploy_update_and_failed_verify(tmp_path):
    local = tmp_path / "repo"
    local.mkdir()
    for file in Path("examples/simple-web").iterdir():
        if file.is_file():
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

        # A host reboot stops every container. Even if the submitted branch has
        # moved, a fresh worker must restore only the persisted active version.
        before = app.state.store.get(second)
        (local / "server.py").write_text("raise RuntimeError('new unsubmitted revision')\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "moved ref")
        assert git("rev-parse", "HEAD").decode().strip() != sha
        host.running.clear()
        host.recovered.clear()
        host.boot_id = "after-host-reboot"

        class NoNewSource:
            def prepare(self, job, checkpoint):
                raise AssertionError("Reboot recovery must not fetch repository contents")

        rebooted = Worker(settings, app.state.store, NoNewSource(), host)
        assert rebooted.run_once()
        assert host.running == {second}
        assert host.recovered == [second]
        assert host.active == second
        after = app.state.store.get(second)
        assert after["snapshot"] == before["snapshot"]
        assert after["execution"]["images"] == before["execution"]["images"]
        assert after["execution"]["ports"] == before["execution"]["ports"]
        assert after["status"] == "succeeded"
        assert not rebooted.run_once()
        assert host.recovered == [second]
