import copy
import hashlib
import json

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.storage import ManagedStorage
from test_runtime import backend, job, recovery_backend
from test_storage import Docker


def storage_runtime(tmp_path, running=False):
    states = {"web": running}
    engine, record = recovery_backend(tmp_path, states)
    spec = {"name": "images", "mountPath": "/data"}
    record["snapshot"]["manifest"]["services"]["web"]["storage"] = spec
    docker = Docker()
    volume = ManagedStorage(docker).ensure(record["app_id"], "web", spec)
    record["execution"]["storage_volumes"] = {"web": volume}
    mount = {"Type": "volume", "Name": volume["name"], "Destination": "/data", "RW": True, "Driver": "local"}
    original = engine._docker

    def call(args, **kwargs):
        if args[0] == "volume":
            engine.calls.append(args)
            return docker(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            normalized = args[:2] + [
                "awesome-abc123-web" if value == hashlib.sha256(b"web").hexdigest() else value
                for value in args[2:]
            ]
            rows = json.loads(original(normalized, **kwargs))
            for row in rows:
                row["Mounts"] = [copy.deepcopy(mount)]
            return json.dumps(rows)
        if args[:2] == ["container", "stop"]:
            engine.calls.append(args)
            states["web"] = False
            return ""
        return original(args, **kwargs)

    engine._docker = call
    return engine, record, mount, docker, states


def test_active_recovery_accepts_exact_persisted_volume_without_recreating(tmp_path):
    engine, record, _, _, states = storage_runtime(tmp_path)
    assert engine.recover_active(record, lambda delta: None) == {"restarted": ["web"]}
    assert states["web"]
    assert not any(args[:2] == ["volume", "create"] for args in engine.calls)


@pytest.mark.parametrize(
    "change",
    ["name", "destination", "readonly", "bind", "missing-volume", "no-identity", "extra", "tmpfs-shadow"],
)
def test_active_recovery_rejects_changed_storage_before_start(tmp_path, change):
    engine, record, mount, docker, states = storage_runtime(tmp_path)
    if change == "name":
        mount["Name"] = "foreign"
    elif change == "destination":
        mount["Destination"] = "/etc"
    elif change == "readonly":
        mount["RW"] = False
    elif change == "bind":
        mount["Type"] = "bind"
    elif change == "missing-volume":
        docker.rows.clear()
    elif change == "no-identity":
        record["execution"].pop("storage_volumes")
    else:
        original = engine._docker

        def extra(args, **kwargs):
            raw = original(args, **kwargs)
            if args[:2] == ["container", "inspect"]:
                rows = json.loads(raw)
                rows[0]["Mounts"].append(
                    {"Type": "tmpfs", "Destination": "/data/objects", "RW": True}
                    if change == "tmpfs-shadow"
                    else dict(mount, Name="foreign")
                )
                return json.dumps(rows)
            return raw

        engine._docker = extra
    with pytest.raises(DeployError):
        engine.recover_active(record, lambda delta: None)
    assert not states["web"]


def test_handoff_stops_exact_previous_id_and_confirms_stopped(tmp_path):
    engine, record, _, _, states = storage_runtime(tmp_path, running=True)
    engine.stop_for_storage_handoff(record)
    assert not states["web"]
    assert [
        "container",
        "stop",
        "--time",
        "30",
        record["execution"]["port_forwards"]["web"]["container_id"],
    ] in engine.calls
    assert engine.calls[-1][:2] == ["container", "inspect"]


@pytest.mark.parametrize("change", ["identity", "owner", "mount", "unstopped"])
def test_handoff_refuses_identity_change_or_unconfirmed_stop(tmp_path, change):
    engine, record, mount, _, states = storage_runtime(tmp_path, running=True)
    if change == "identity":
        record["execution"]["port_forwards"]["web"]["container_id"] = "f" * 64
    elif change == "mount":
        mount["Name"] = "foreign"
    original = engine._docker

    def docker(args, **kwargs):
        if change == "unstopped" and args[:2] == ["container", "stop"]:
            return ""
        raw = original(args, **kwargs)
        if change == "owner" and args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            rows[0]["Config"]["Labels"]["awesome.app"] = "foreign"
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.stop_for_storage_handoff(record)
    assert states["web"]


def test_create_mount_flag_uses_only_derived_volume(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    spec = {"name": "images", "mountPath": "/data"}
    record["snapshot"]["manifest"]["services"]["web"]["storage"] = spec
    name, _ = ManagedStorage.identity(record["app_id"], "web", spec)
    record["execution"]["storage_volumes"] = {"web": {"name": name, "created_at": "now"}}
    engine._create_container(
        record, "web", record["snapshot"]["manifest"]["services"]["web"], {}, ["network"]
    )
    create = next(args for args in engine.calls if args[:2] == ["container", "create"])
    assert create[create.index("--mount") + 1] == f"type=volume,source={name},destination=/data"
    assert "--read-only" in create and "--cap-drop=ALL" in create


def test_start_existing_candidate_checks_mount_before_start(tmp_path):
    engine, record, mount, _, states = storage_runtime(tmp_path)
    original = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["container", "ls"]:
            return record["execution"]["port_forwards"]["web"]["container_id"]
        return original(args, **kwargs)

    engine._docker = docker
    mount["Name"] = "foreign"
    with pytest.raises(DeployError, match="exact owned"):
        engine.start(record, lambda delta: None)
    assert not states["web"]


def test_update_refuses_missing_previous_volume_identity_before_create(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    spec = {"name": "images", "mountPath": "/data"}
    record["snapshot"]["manifest"]["services"]["web"]["storage"] = spec
    record["execution"]["previous_manifest"] = copy.deepcopy(record["snapshot"]["manifest"])
    with pytest.raises(DeployError, match="identity is missing"):
        engine.start(record, lambda delta: None)
    assert not any(args[:2] == ["volume", "create"] for args in engine.calls)


def test_cleanup_and_retirement_never_delete_volume(tmp_path):
    engine, record, _, docker, _ = storage_runtime(tmp_path, running=False)
    before = copy.deepcopy(docker.rows)
    engine.cleanup(record)
    assert docker.rows == before
    assert not any(args[:2] in [["volume", "rm"], ["volume", "prune"]] for args in engine.calls)


def test_candidate_quiescence_confirms_exit_and_rejects_foreign_container(tmp_path):
    engine, record, _, _, states = storage_runtime(tmp_path, running=True)
    original = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["container", "ls"]:
            return record["execution"]["port_forwards"]["web"]["container_id"]
        return original(args, **kwargs)

    engine._docker = docker
    engine.quiesce_storage_candidate(record)
    assert not states["web"]
    states["web"] = True
    record["execution"]["port_forwards"]["web"]["container_id"] = "f" * 64
    with pytest.raises(DeployError):
        engine.quiesce_storage_candidate(record)
    assert states["web"]


@pytest.mark.parametrize("change", ["missing", "different-mount"])
def test_new_candidate_is_reinspected_before_start(tmp_path, change):
    engine, record, mount, docker, states = storage_runtime(tmp_path)
    original_create = engine._create_container

    def create(*args):
        original_create(*args)
        if change == "missing":
            docker.rows.clear()
        else:
            mount["Name"] = "foreign"

    engine._create_container = create
    with pytest.raises(DeployError):
        engine.start(record, lambda delta: None)
    assert not states["web"]
    assert not any(args[:2] == ["container", "start"] for args in engine.calls)
