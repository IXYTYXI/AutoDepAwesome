from pathlib import Path
import os
import shutil
import stat
import subprocess
import tarfile
import io

import pytest
import yaml

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


SHA = "a" * 40

def manifest():
    return yaml.safe_dump({
        "schemaVersion": 1, "app": {"name": "Demo", "description": "Test", "subdomain": "demo"},
        "services": {"web": {"kind": "web", "build": {"context": "backend", "dockerfile": "Dockerfile"},
                              "port": 8080, "healthcheck": {"type": "http", "path": "/health"}}},
        "routes": [{"path": "/", "match": "prefix", "service": "web"}],
    })


class FakeRunner:
    def __init__(self, files=None, tree=None, fail_archive=False):
        self.calls = []
        self.files = files if files is not None else {"awesome.yaml": manifest(), "backend/Dockerfile": "FROM scratch\n"}
        self.tree = tree or b"100644 blob abc\tawesome.yaml\0"
        self.fail_archive = fail_archive

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, (SHA + "\n").encode(), b"")
        if "ls-tree" in args:
            return subprocess.CompletedProcess(args, 0, self.tree, b"")
        if "archive" in args:
            if self.fail_archive:
                raise DeployError("GIT_FAILED", "Archive unavailable")
            dest = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
            with tarfile.open(dest, "w") as archive:
                for name, content in self.files.items():
                    item = tarfile.TarInfo(name)
                    if isinstance(content, tuple):
                        item.type = tarfile.SYMTYPE
                        item.linkname = content[1]
                        archive.addfile(item)
                    else:
                        blob = content if isinstance(content, bytes) else content.encode()
                        item.size = len(blob)
                        archive.addfile(item, io.BytesIO(blob))
        return subprocess.CompletedProcess(args, 0, b"", b"")


def job(**execution):
    return {"id": "job-1", "input": {"repository": "https://github.com/example/demo", "ref": "main", "subdomain": "demo"}, "execution": execution}


def test_extract_normalizes_internal_directories_under_private_worker_umask(tmp_path):
    from awesome_deploy.source import GitSource

    state = tmp_path / "state"
    sources = state / "sources"
    target = sources / "job-1.partial"
    state.mkdir(mode=0o700)
    sources.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    private_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (tmp_path, state, sources, target)}
    archive_path = tmp_path / "source.tar"
    files = {
        "src/nested/app.py": (b"print('unchanged source')\n", 0o644),
        "implicit/parents/run.sh": (b"#!/bin/sh\nexit 0\n", 0o755),
        "README.md": (b"unchanged root file\n", 0o644),
    }
    with tarfile.open(archive_path, "w") as archive:
        for name in ("src", "src/nested", "empty/child"):
            item = tarfile.TarInfo(name)
            item.type, item.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(item)
        for name, (content, mode) in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(content), mode
            archive.addfile(item, io.BytesIO(content))
    previous_umask = os.umask(0o077)
    try:
        GitSource(Settings(state_dir=state))._extract(archive_path, target)
        copied = tmp_path / "build-context"
        shutil.copytree(target, copied)
    finally:
        os.umask(previous_umask)
    for base in (target, copied):
        for name in ("src", "src/nested", "implicit", "implicit/parents", "empty", "empty/child"):
            assert stat.S_IMODE((base / name).stat().st_mode) == 0o755, name
        for name, (content, mode) in files.items():
            assert (base / name).read_bytes() == content
            assert stat.S_IMODE((base / name).stat().st_mode) == mode
        assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in private_modes} == private_modes


def test_prepare_retains_private_state_and_source_root_under_worker_umask(tmp_path):
    from awesome_deploy.source import GitSource

    state = tmp_path / "state"
    previous_umask = os.umask(0o077)
    try:
        snapshot = GitSource(Settings(state_dir=state), runner=FakeRunner()).prepare(job(), lambda patch: None)
    finally:
        os.umask(previous_umask)
    source = Path(snapshot["source_dir"])
    for path in (state, state / "git", state / "git" / "job-1", state / "sources", source):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    assert stat.S_IMODE((source / "backend").stat().st_mode) == 0o755
    assert (source / "backend" / "Dockerfile").read_text() == "FROM scratch\n"


def test_source_module_available():
    from importlib.util import find_spec
    assert find_spec("awesome_deploy.source") is not None, "secure Git acquisition is required"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ssh://git@github.com/a/b", "https://evil.example/a/b",
    "https://token@github.com/a/b", "https://github.com:443/a/b", "https://github.com/a/b?x=1",
    "https://github.com/a/b#main", "https://github.com/a/../b", "https://github.com/a/%2e%2e/b",
    "https://github.com/a/$(whoami)", "https://github.com/a/b\n", "https://github.com/a/b/extra",
    "https://github.com//b", "https://github.com/a\\b/repo", "https://github.com/a/b.git/",
])
def test_repository_allowlist(url):
    from awesome_deploy.source import validate_repository
    with pytest.raises(DeployError):
        validate_repository(url, Settings())


def test_canonical_urls_and_gitlab_groups():
    from awesome_deploy.source import validate_repository
    assert validate_repository("https://GitHub.com/team/repo", Settings()) == "https://github.com/team/repo.git"
    assert validate_repository("https://gitlab.com/a/b/c.git", Settings()) == "https://gitlab.com/a/b/c.git"


@pytest.mark.parametrize("ref", ["", "-main", "main;id", "$(id)", "main\n", "a..b", "a@{1}", "a:b", "a b", "/main", "main.lock", "main\\x", "foo//bar"])
def test_ref_rejects_shell_and_git_revision_syntax(ref):
    from awesome_deploy.source import validate_ref
    with pytest.raises(DeployError):
        validate_ref(ref)


def test_commit_checkpoint_precedes_any_source_processing(tmp_path):
    from awesome_deploy.source import GitSource
    runner = FakeRunner(fail_archive=True)
    checkpoints = []
    def checkpoint(patch):
        assert not any("archive" in call[0] or "ls-tree" in call[0] for call in runner.calls)
        checkpoints.append(patch)
    with pytest.raises(DeployError):
        GitSource(Settings(state_dir=tmp_path), runner=runner).prepare(job(), checkpoint)
    assert checkpoints == [{"commit": SHA}]


def test_resume_uses_persisted_commit_and_exports_no_git(tmp_path):
    from awesome_deploy.source import GitSource
    runner = FakeRunner()
    snapshot = GitSource(Settings(state_dir=tmp_path), runner=runner).prepare(job(commit=SHA), lambda patch: None)
    assert snapshot["commit"] == SHA
    assert snapshot["repository"] == "https://github.com/example/demo.git"
    source = Path(snapshot["source_dir"])
    assert source == tmp_path / "sources" / "job-1"
    assert (source / "backend/Dockerfile").is_file()
    assert not (source / ".git").exists()
    assert not any("rev-parse" in call[0] for call in runner.calls)
    assert all("main" not in call[0] for call in runner.calls)
    assert snapshot["manifest"]["services"]["web"]["resources"] == {"cpu": .5, "memoryMiB": 500}
    for args, options in runner.calls:
        assert options["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert options["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "http.followRedirects=false" in args
        assert "protocol.file.allow=never" in args
        assert "credential.helper=" in args


def test_private_credentials_only_use_configured_askpass(tmp_path, monkeypatch):
    from awesome_deploy.source import GitSource
    monkeypatch.setenv("GIT_ASKPASS", "/evil/askpass")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "999")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    runner = FakeRunner()
    snapshot = GitSource(Settings(state_dir=tmp_path, git_askpass="/opt/approved-askpass"), runner=runner).prepare(job(), lambda p: None)
    assert "do-not-leak" not in str(snapshot)
    for _, options in runner.calls:
        assert options["env"]["GIT_ASKPASS"] == "/opt/approved-askpass"
        assert "SECRET_TOKEN" not in options["env"]
        assert "GIT_CONFIG_COUNT" not in options["env"]


@pytest.mark.parametrize("files", [
    {"awesome.yaml": ("symlink", "/etc/passwd")},
    {"../escaped": "escape", "awesome.yaml": manifest()},
    {".git/config": "credentials", "awesome.yaml": manifest()},
    {".gitmodules": "[submodule x]", "awesome.yaml": manifest()},
    {"awesome.yaml": manifest(), "backend": ("symlink", "/tmp")},
])
def test_export_rejects_symlinks_traversal_and_git_metadata(tmp_path, files):
    from awesome_deploy.source import GitSource
    with pytest.raises(DeployError):
        GitSource(Settings(state_dir=tmp_path), runner=FakeRunner(files)).prepare(job(), lambda p: None)
    assert not (tmp_path / "escaped").exists()


def test_submodule_rejected_even_if_archive_omits_it(tmp_path):
    from awesome_deploy.source import GitSource
    runner = FakeRunner(tree=b"160000 commit " + SHA.encode() + b"\tbackend\0")
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(state_dir=tmp_path), runner=runner).prepare(job(), lambda p: None)
    assert raised.value.code == "UNSUPPORTED_CAPABILITY"
    assert not any("archive" in call[0] for call in runner.calls)


def test_size_budget_and_manifest_subdomain_binding(tmp_path):
    from awesome_deploy.source import GitSource
    runner = FakeRunner({"awesome.yaml": manifest(), "huge": "x" * 1024 * 1024})
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(state_dir=tmp_path, repo_size_limit_mib=1), runner=runner).prepare(job(), lambda p: None)
    assert raised.value.code == "SOURCE_TOO_LARGE"
    data = job()
    data["input"]["subdomain"] = "other"
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(state_dir=tmp_path), runner=FakeRunner()).prepare(data, lambda p: None)
    assert raised.value.code == "SUBDOMAIN_MISMATCH"


def test_missing_or_symlink_build_files_rejected(tmp_path):
    from awesome_deploy.source import GitSource
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(state_dir=tmp_path), runner=FakeRunner({"awesome.yaml": manifest()})).prepare(job(), lambda p: None)
    assert raised.value.code == "INVALID_BUILD_PATH"


@pytest.mark.parametrize("files,reason", [
    ({"backend/Dockerfile": "FROM scratch\n"}, "MANIFEST_MISSING"),
    ({"awesome.yaml/nested": "a directory instead of a file"}, "MANIFEST_MISSING"),
    ({"awesome.yaml": "#" * (256 * 1024 + 1)}, "MANIFEST_TOO_LARGE"),
    ({"awesome.yaml": b"\xff\xfeinvalid"}, "MANIFEST_ENCODING"),
])
def test_manifest_source_failure_identifies_missing_size_or_encoding(tmp_path, files, reason):
    from awesome_deploy.source import GitSource
    persisted = {}
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(state_dir=tmp_path), runner=FakeRunner(files)).prepare(job(), persisted.update)
    assert raised.value.code == "INVALID_MANIFEST"
    assert raised.value.as_dict().get("reason") == reason
    assert raised.value.path == "awesome.yaml"
    assert persisted == {"commit": SHA}
    assert not (tmp_path / "sources/job-1.partial").exists()


def test_job_id_cannot_escape_state(tmp_path):
    from awesome_deploy.source import GitSource
    data = job()
    data["id"] = "../../outside"
    with pytest.raises(DeployError):
        GitSource(Settings(state_dir=tmp_path), runner=FakeRunner()).prepare(data, lambda p: None)


def test_real_git_export_with_explicit_local_test_transport(tmp_path):
    from awesome_deploy.source import GitSource
    local = tmp_path / "local-repo"
    local.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(local)], check=True, capture_output=True)
    (local / "awesome.yaml").write_text(manifest())
    (local / "backend").mkdir()
    (local / "backend/Dockerfile").write_text("FROM scratch\n")
    subprocess.run(["git", "-C", str(local), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"], check=True, capture_output=True)
    first = subprocess.check_output(["git", "-C", str(local), "rev-parse", "HEAD"]).decode().strip()
    def local_test_runner(args, **options):
        args = ["protocol.file.allow=always" if x == "protocol.file.allow=never" else str(local) if x == "https://github.com/example/demo.git" else x for x in args]
        return subprocess.run(args, cwd=options["cwd"], env=options["env"], capture_output=True, timeout=options["timeout"], check=False)
    persisted = {}
    snapshot = GitSource(Settings(state_dir=tmp_path / "state"), runner=local_test_runner).prepare(job(), persisted.update)
    assert snapshot["commit"] == first == persisted["commit"]
    (local / "backend/Dockerfile").write_text("FROM alpine\n")
    subprocess.run(["git", "-C", str(local), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-am", "moving branch"], check=True, capture_output=True)
    snapshot = GitSource(Settings(state_dir=tmp_path / "state"), runner=local_test_runner).prepare(job(**persisted), persisted.update)
    assert snapshot["commit"] == first
    assert (Path(snapshot["source_dir"]) / "backend/Dockerfile").read_text() == "FROM scratch\n"


def test_bounded_runner_times_out_and_caps_output(tmp_path):
    import sys
    from awesome_deploy.source import _bounded_run
    with pytest.raises(DeployError) as raised:
        _bounded_run([sys.executable, "-c", "import time;time.sleep(5)"], cwd=tmp_path, env={}, timeout=.05)
    assert raised.value.code == "GIT_TIMEOUT"
    with pytest.raises(DeployError) as raised:
        _bounded_run([sys.executable, "-c", "import sys;sys.stdout.write('x'*5000000)"], cwd=tmp_path, env={}, timeout=5)
    assert raised.value.code == "GIT_OUTPUT_LIMIT"


def test_previous_resource_values_flow_through_source(tmp_path):
    from awesome_deploy.source import GitSource
    prior = {"services": {"web": {"resources": {"cpu": 2, "memoryMiB": 1024}}}}
    snapshot = GitSource(Settings(state_dir=tmp_path, max_app_memory_mib=1024), runner=FakeRunner()).prepare(job(previous_manifest=prior), lambda p: None)
    assert snapshot["manifest"]["services"]["web"]["resources"] == {"cpu": 2, "memoryMiB": 1024}


def test_production_runner_inherits_worker_lock_descriptor(tmp_path):
    import fcntl
    import os
    import sys
    from awesome_deploy.source import _bounded_run
    with (tmp_path / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not os.get_inheritable(lock.fileno())
        result = _bounded_run([sys.executable, "-c", f"import os;os.fstat({lock.fileno()})"],
                              cwd=tmp_path, env={}, timeout=5, lock_fd=lock.fileno())
        assert result.returncode == 0, result.stderr


def test_source_propagates_worker_lock_to_every_git_child(tmp_path):
    from awesome_deploy.source import GitSource
    runner = FakeRunner()
    source = GitSource(Settings(state_dir=tmp_path), runner=runner)
    with (tmp_path / "worker.lock").open("w") as lock:
        source.lock_fd = lock.fileno()
        source.prepare(job(), lambda patch: None)
        assert runner.calls
        assert all(options.get("lock_fd") == lock.fileno() for _, options in runner.calls)
