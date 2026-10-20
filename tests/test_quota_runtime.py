import copy
import json
import hashlib
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.storage import ManagedStorage

SPEC = {"name": "images", "mountPath": "/data", "sizeMiB": 64, "maxInodes": 100}


def reference():
    digest = hashlib.sha256(json.dumps({"app_id": "app", "service": "web", "name": "images"},
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"backend": "xfs-project", "storage_id": digest, "generation": "a" * 32}


class Helper:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.record = {"reference": reference(), "path": "/quota/objects/" + "a" * 32 + "/data",
                       "identity": {"app_id": "app", "service": "web", "name": "images"},
                       "size_mib": 64, "max_inodes": 100, "state": "ready", "revision": 1}

    def call(self, op, *, timeout=120, **kwargs):
        self.calls.append((op, kwargs))
        if self.failure:
            raise DeployError(self.failure, "unavailable")
        if op == "inventory":
            return {"records": [{**self.record, "writers": []}]}
        return copy.deepcopy(self.record)


def storage(helper=None, backend="xfs-project"):
    return ManagedStorage(lambda *a, **kw: pytest.fail("quota must not create Docker volumes"),
                          SimpleNamespace(storage_backend=backend), quota=helper or Helper())


def job():
    return {"app_id": "app", "id": "release", "snapshot": {"manifest": {"version": 2,
            "services": {"web": {"storage": SPEC}}}}, "execution": {"storage_volumes": {"web": reference()}}}


def test_identity_does_not_change_with_quota_declaration():
    assert ManagedStorage.identity("app", "web", SPEC) == ManagedStorage.identity(
        "app", "web", {"name": "images", "mountPath": "/data"})


def test_quota_storage_uses_helper_and_persists_only_stable_reference():
    helper = Helper()
    managed = storage(helper)
    assert managed.ensure("app", "web", SPEC) == reference()
    assert helper.calls[0][0] == "ensure"
    assert helper.calls[0][1]["limits"] == {"size_mib": 64, "max_inodes": 100}
    assert "path" not in helper.calls[0][1]
    assert managed.mount_argument(job(), "web") == "type=bind,source=" + helper.record["path"] + ",destination=/data,bind-propagation=rprivate"


def test_legacy_reference_requires_migration_on_quota_backend():
    with pytest.raises(DeployError) as exc:
        storage().ensure("app", "web", SPEC, {"name": "old", "created_at": "x", "instance": "b" * 32})
    assert exc.value.code == "STORAGE_MIGRATION_REQUIRED"


def test_quota_declaration_and_reference_cannot_fall_back_to_local():
    for spec, expected in [(SPEC, None), ({"name": "images", "mountPath": "/data"}, reference())]:
        with pytest.raises(DeployError) as exc:
            storage(backend="local").ensure("app", "web", spec, expected)
        assert exc.value.code == "QUOTA_BACKEND_REQUIRED"


def test_historical_manifest_can_reuse_quota_reference_without_limits():
    helper = Helper()
    assert storage(helper).ensure("app", "web", {"name": "images", "mountPath": "/data"}, reference()) == reference()
    assert "limits" not in helper.calls[0][1]


def test_stop_mount_identity_does_not_require_live_quota():
    helper = Helper()
    helper.failure = "QUOTA_NOT_ENFORCED"
    row = {"Mounts": [{"Type": "bind", "Source": helper.record["path"], "Destination": "/data", "RW": True}]}
    storage(helper).validate_mounts(job(), "web", row, readiness=False)
    assert helper.calls == []
    with pytest.raises(DeployError):
        storage(helper).validate_mounts(job(), "web", row)


def test_untrusted_helper_paths_and_identity_fail_closed():
    for field, value in [("path", "/quota/path,with-option"), ("reference", {**reference(), "generation": "b" * 32})]:
        helper = Helper()
        helper.record[field] = value
        with pytest.raises(DeployError):
            storage(helper).ensure("app", "web", SPEC, reference())


def test_quota_client_bounded_protocol_and_helper_error(tmp_path):
    from awesome_deploy.quota_client import QuotaClient
    script = tmp_path / "helper.py"
    script.write_text("import sys,json\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'ok':False,'error':{'code':'QUOTA_NOT_ENFORCED','message':'disabled'}}),flush=True)\n")
    with pytest.raises(DeployError) as exc:
        QuotaClient([sys.executable, str(script)]).call("check")
    assert exc.value.code == "QUOTA_NOT_ENFORCED"
    script.write_text("print('x'*70000,flush=True)\n")
    with pytest.raises(DeployError) as exc:
        QuotaClient([sys.executable, str(script)]).call("check")
    assert exc.value.code == "QUOTA_PROTOCOL"


def test_quota_session_retains_gate_and_supports_retirement(tmp_path):
    from awesome_deploy.quota_client import QuotaClient
    script = tmp_path / "helper.py"
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({'ok':True,'result':{'op':r['op']}}),flush=True)\n if r['op']=='release': break\n")
    with QuotaClient([sys.executable, str(script)]).session("guard", items=[]) as session:
        assert session.result == {"op": "guard"}
        assert session.fd >= 0
        assert session.call("retire", container_id="a" * 64) == {"op": "retire"}


def test_prepare_storage_rejects_v2_without_quota_backend(tmp_path):
    from awesome_deploy.runtime import DeploymentBackend
    backend = object.__new__(DeploymentBackend)
    backend.settings = SimpleNamespace(storage_backend="local")
    current = job()
    current["execution"] = {}
    current["snapshot"]["manifest"].pop("version")
    current["snapshot"]["manifest"]["schemaVersion"] = 2
    current["snapshot"]["manifest"]["services"]["web"]["storage"] = {"name": "images", "mountPath": "/data"}
    backend._docker = lambda *a, **kw: pytest.fail("Version 2 must be rejected before any Docker call")
    with pytest.raises(DeployError) as exc:
        backend.prepare_storage(current, lambda change: None)
    assert exc.value.code == "QUOTA_BACKEND_REQUIRED"


@pytest.mark.parametrize("condition", ["running", "absent", "stop-failed", "unknown"])
def test_quota_patrol_stops_exact_writer_without_readiness(monkeypatch, tmp_path, condition):
    from awesome_deploy.runtime import DeploymentBackend
    from awesome_deploy.quota_client import QuotaClient
    writer = {"container_id": "c" * 64, "container_name": "awesome-release-web",
              "app_id": "app", "release_id": "release", "service": "web"}
    report = {"blocked": [{"reference": reference(), "writers": [writer], "reason": "QUOTA_NOT_ENFORCED"}], "unknown": []}
    if condition == "unknown":
        report = {"blocked": [], "unknown": [{"reference": reference(), "reason": "QUOTA_UNAVAILABLE"}]}
    calls = []
    class Session:
        fd = 456
        result = report
        def call(self, op, **kwargs):
            calls.append((op, kwargs))
    @contextmanager
    def session(*args, **kwargs):
        yield Session()
    monkeypatch.setattr(QuotaClient, "session", session)
    backend = object.__new__(DeploymentBackend)
    backend.settings = SimpleNamespace(storage_backend="xfs-project", state_dir=tmp_path)
    backend.quota_guard_fd = None
    def docker(args, **kwargs):
        calls.append(args)
        if args[:2] == ["container", "ls"]:
            return "" if condition == "absent" else writer["container_id"]
        if args[:2] == ["container", "stop"] and condition == "stop-failed":
            raise DeployError("COMMAND_TIMEOUT", "Unknown stop result")
        if args[:2] == ["container", "inspect"]:
            if condition == "absent":
                raise DeployError("COMMAND_FAILED", "Container is absent")
            return json.dumps([{ "Id": writer["container_id"], "Name": "/" + writer["container_name"],
                               "Config": {"Labels": {"awesome.managed": "true", "awesome.app": "app", "awesome.job": "release"}},
                               "State": {"Running": not any(c[:2] == ["container", "stop"] for c in calls if isinstance(c, list))}}])
        return ""
    backend._docker = docker
    result = backend.quota_patrol()
    if condition in {"running", "absent"}:
        assert result["stopped"] == [writer["container_id"]]
        assert ("retire", {"container_id": writer["container_id"]}) in calls
        if condition == "running":
            assert ["container", "stop", "--time", "30", writer["container_id"]] in calls
    else:
        assert result["stopped"] == []
        assert not any(isinstance(call, tuple) and call[0] == "retire" for call in calls)
        if condition == "unknown":
            assert calls == []
        else:
            assert result["stop_failures"] == [{"container_id": writer["container_id"], "code": "COMMAND_TIMEOUT"}]
    assert backend.quota_guard_fd is None


def test_docker_child_inherits_both_worker_and_quota_gate(monkeypatch, tmp_path):
    from awesome_deploy.runtime import DeploymentBackend
    import awesome_deploy.runtime as runtime
    captured = {}
    def run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(runtime, "run_command", run)
    backend = object.__new__(DeploymentBackend)
    backend.settings = SimpleNamespace(state_dir=tmp_path, docker_binary="docker", docker_host="unix:///test")
    backend.lock_fd, backend.quota_guard_fd = 41, 42
    backend._docker(["container", "start", "test"])
    assert set(captured["pass_fds"]) == {41, 42}


def test_quota_removal_retires_only_confirmed_absent_writers():
    helper = Helper()
    writer = {"container_id": "c" * 64, "container_name": "awesome-release-web",
              "app_id": "app", "release_id": "release", "service": "web"}
    original = helper.call
    def call(op, **kwargs):
        if op == "inventory":
            return {"records": [{**helper.record, "writers": [writer]}]}
        return original(op, **kwargs)
    helper.call = call
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        return ""
    managed = ManagedStorage(docker, SimpleNamespace(storage_backend="xfs-project"), quota=helper)
    managed.remove("app", "web", SPEC, reference())
    assert any(op == "retire" for op, kw in helper.calls)
    assert any(op == "delete" for op, kw in helper.calls)
    assert calls[0][:3] == ["container", "ls", "-a"]


def test_quota_removal_does_not_retire_running_writer():
    helper = Helper()
    writer = {"container_id": "c" * 64, "container_name": "awesome-release-web",
              "app_id": "app", "release_id": "release", "service": "web"}
    original = helper.call
    def call(op, **kwargs):
        if op == "inventory":
            return {"records": [{**helper.record, "writers": [writer]}]}
        return original(op, **kwargs)
    helper.call = call
    def docker(args, **kwargs):
        if args[:2] == ["container", "ls"]:
            return writer["container_id"]
        return json.dumps([{ "Id": writer["container_id"], "Name": "/awesome-release-web",
                            "Config": {"Labels": {"awesome.managed": "true", "awesome.app": "app", "awesome.job": "release"}},
                            "State": {"Running": True}}])
    managed = ManagedStorage(docker, SimpleNamespace(storage_backend="xfs-project"), quota=helper)
    with pytest.raises(DeployError) as exc:
        managed.remove("app", "web", SPEC, reference())
    assert exc.value.code == "STORAGE_IN_USE"
    assert not any(op in {"delete", "retire"} for op, kw in helper.calls)


def test_storage_contract_matches_real_helper_ledger(tmp_path):
    from test_quota_helper import FakeBackend
    from storage_quota.service import QuotaService
    config = {f"{scope}_budget_{unit}": 10000 for scope in ("global", "app", "service") for unit in ("mib", "inodes")}
    service = QuotaService(config, FakeBackend(), tmp_path)
    class DirectClient:
        def call(self, op, *, timeout=120, **values):
            return service.execute({"op": op, **values})
    managed = storage(DirectClient())
    ref = managed.ensure("app", "web", SPEC)
    assert ref["backend"] == "xfs-project"
    assert managed.inspect("app", "web", SPEC, ref)["size_mib"] == 64


def test_prepare_storage_uses_replayable_operation_id(monkeypatch, tmp_path):
    from awesome_deploy.runtime import DeploymentBackend
    import awesome_deploy.storage_status as status
    monkeypatch.setattr(status, "record_quota_status", lambda *a, **kw: None)
    observed = []
    monkeypatch.setattr(ManagedStorage, "ensure", lambda *args, **kwargs: observed.append(kwargs.get("operation_id")) or reference())
    from awesome_deploy.quota_client import QuotaClient
    monkeypatch.setattr(QuotaClient, "call", lambda *args, **kwargs: {"enforced": True})
    backend = object.__new__(DeploymentBackend)
    backend.settings = SimpleNamespace(storage_backend="xfs-project", state_dir=tmp_path)
    current = job()
    backend.prepare_storage(current, lambda c: None)
    backend.prepare_storage(current, lambda c: None)
    assert observed[0] and observed[0] == observed[1]


def test_quota_start_uses_registered_id_and_confirms_inside_gate(monkeypatch, tmp_path):
    from dataclasses import replace
    from test_runtime import backend as runtime_backend, job as runtime_job
    from awesome_deploy.quota_client import QuotaClient
    engine, current = runtime_backend(tmp_path), runtime_job(tmp_path)
    current["app_id"], current["id"] = "app", "release"
    current["snapshot"]["manifest"]["services"]["web"]["storage"] = SPEC
    current["execution"]["storage_volumes"] = {"web": reference()}
    engine.settings = replace(engine.settings, storage_backend="xfs-project")
    engine.preflight = lambda: {}
    engine.prepare_storage = lambda *a: None
    engine._ensure_network = lambda *a: None
    engine._image = lambda *a: {}
    engine._create_container = lambda *a: None
    engine._forward_identity = lambda *a, **kw: {}
    engine._reconcile_forward = lambda *a, **kw: None
    path = Helper().record["path"]
    row = {"Id": "d" * 64, "Name": "/awesome-release-web", "State": {"Running": True},
           "Config": {"Labels": {"awesome.managed": "true", "awesome.app": "app", "awesome.job": "release"}},
           "Mounts": [{"Type": "bind", "Source": path, "Destination": "/data", "RW": True}]}
    events = []
    def docker(args, **kw):
        events.append((args, engine.quota_guard_fd))
        return json.dumps([row]) if args[:2] == ["container", "inspect"] else ""
    engine._docker = docker
    monkeypatch.setattr(QuotaClient, "call", lambda *a, **kw: Helper().record)
    @contextmanager
    def session(*args, **kwargs):
        assert kwargs["items"][0]["writer"]["container_id"] == row["Id"]
        yield SimpleNamespace(fd=78, result={"records": [Helper().record]})
    monkeypatch.setattr(QuotaClient, "session", session)
    engine.start(current, lambda value: None)
    assert (["container", "start", row["Id"]], 78) in events
    assert (["container", "inspect", row["Id"]], 78) in events


def test_quota_client_rejects_duplicate_json_keys(tmp_path):
    from awesome_deploy.quota_client import QuotaClient
    script = tmp_path / "helper.py"
    script.write_text("print('{\"ok\":false,\"ok\":true,\"result\":{}}',flush=True)\n")
    with pytest.raises(DeployError) as exc:
        QuotaClient([sys.executable, str(script)]).call("check")
    assert exc.value.code == "QUOTA_PROTOCOL"


def test_guard_inherited_write_fd_keeps_admission_locked_after_worker_close(tmp_path):
    import fcntl
    import subprocess
    from awesome_deploy.quota_client import QuotaClient
    gate = tmp_path / "admission.lock"
    script = tmp_path / "helper.py"
    script.write_text("import sys,json,fcntl\nf=open(sys.argv[1],'w')\nfcntl.flock(f,fcntl.LOCK_EX)\nsys.stdin.readline()\nprint(json.dumps({'ok':True,'result':{}}),flush=True)\nsys.stdin.read()\n")
    child = None
    try:
        with pytest.raises(RuntimeError, match="worker lost"):
            with QuotaClient([sys.executable, str(script), str(gate)]).session("guard", items=[]) as session:
                child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(.25)"], pass_fds=(session.fd,))
                session.process.stdin.close()
                with gate.open("r") as probe:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise RuntimeError("worker lost")
    finally:
        if child is not None:
            child.wait(timeout=3)
    with gate.open("r") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_quota_client_accepts_large_bounded_inventory_and_rejects_overflow(tmp_path):
    from awesome_deploy.quota_client import QuotaClient
    script = tmp_path / "helper.py"
    script.write_text("import json\nprint(json.dumps({'ok':True,'result':{'rows':'x'*100000}}),flush=True)\n")
    assert len(QuotaClient([sys.executable, str(script)]).call("inventory")["rows"]) == 100000
    script.write_text("import json\nprint(json.dumps({'ok':True,'result':{'rows':'x'*(4*1024*1024)}}),flush=True)\n")
    with pytest.raises(DeployError) as exc:
        QuotaClient([sys.executable, str(script)]).call("inventory")
    assert exc.value.code == "QUOTA_PROTOCOL"


def test_quota_missing_ledger_is_not_successful_data_deletion():
    helper = Helper()
    helper.call = lambda *args, **kwargs: {"records": []}
    managed = storage(helper)
    with pytest.raises(DeployError) as exc:
        managed.remove("app", "web", SPEC, reference())
    assert exc.value.code == "QUOTA_REFERENCE_MISSING"


def test_offline_quota_deletion_is_replayable_against_real_ledger(tmp_path):
    from test_quota_helper import FakeBackend
    from storage_quota.service import QuotaService
    from awesome_deploy.offline_data import OfflineDataDeletion
    from awesome_deploy.config import Settings
    config = {f"{scope}_budget_{unit}": 10000 for scope in ("global", "app", "service") for unit in ("mib", "inodes")}
    service = QuotaService(config, FakeBackend(), tmp_path / "ledger")
    class DirectClient:
        def call(self, op, *, timeout=120, **values):
            return service.execute({"op": op, **values})
    managed = ManagedStorage(lambda *a, **kw: "", SimpleNamespace(storage_backend="xfs-project"), quota=DirectClient())
    ref = managed.ensure("app", "web", SPEC)
    deletion = OfflineDataDeletion(Settings(state_dir=tmp_path / "worker", storage_backend="xfs-project"), lambda *a, **kw: "")
    deletion.storage = managed
    metadata = {"app_id": "app", "job_ids": ["release"], "scopes": [],
                "volumes": [{"service": "web", "spec": SPEC, "expected": ref}]}
    plan = deletion.inventory(metadata)
    deletion.delete(plan, lambda change: None)
    deletion.delete(plan, lambda change: None)
    assert service.execute({"op": "inventory"})["totals"] == {"size_mib": 0, "max_inodes": 0}


def test_pending_allocation_without_directory_can_be_deleted_by_helper():
    helper = Helper()
    helper.record.update(path=None, state="allocating")
    managed = ManagedStorage(lambda *a, **kw: pytest.fail("No container can mount an uncreated allocation"),
                             SimpleNamespace(storage_backend="xfs-project"), quota=helper)
    managed.remove("app", "web", SPEC, reference())
    assert any(op == "delete" for op, _ in helper.calls)


def test_stateless_stop_does_not_require_quota_helper_when_backend_enabled(monkeypatch, tmp_path):
    from dataclasses import replace
    from test_runtime import recovery_backend
    from awesome_deploy.quota_client import QuotaClient
    engine, current = recovery_backend(tmp_path, {"web": False, "api": False})
    engine.settings = replace(engine.settings, storage_backend="xfs-project")
    original = engine._docker
    def docker(args, **kwargs):
        if args[:2] == ["container", "inspect"]:
            mapping = {hashlib.sha256(name.encode()).hexdigest(): f"awesome-abc123-{name}" for name in ("web", "api")}
            args = [*args[:2], *(mapping.get(value, value) for value in args[2:])]
        return original(args, **kwargs)
    engine._docker = docker
    monkeypatch.setattr(QuotaClient, "call", lambda *a, **kw: pytest.fail("Stateless stop must not call quota helper"))
    engine._stop_owned_containers(current, require_existing=True)


def test_enabling_quota_backend_preserves_explicit_legacy_data_deletion(monkeypatch, tmp_path):
    from dataclasses import replace
    from test_offline_data import environment
    from awesome_deploy.quota_client import QuotaClient
    backend, docker, secrets, metadata, other = environment.__wrapped__(tmp_path)
    backend.settings = replace(backend.settings, storage_backend="xfs-project")
    monkeypatch.setattr(QuotaClient, "call", lambda *a, **kw: {"records": []})
    plan = backend.inventory_offline_data(metadata)
    backend.delete_offline_data(plan, lambda change: None)
    assert docker.rows == {}
    backend.delete_offline_data(plan, lambda change: None)
