import copy
import json

import pytest

from awesome_deploy.errors import DeployError
from test_manifest import load, valid


def stateful():
    data = valid()
    data["services"]["web"]["storage"] = {"name": "images", "mountPath": "/data"}
    return data


def test_storage_schema_accepts_only_owned_fixed_mount():
    assert load(stateful())["services"]["web"]["storage"]["mountPath"] == "/data"


@pytest.mark.parametrize(
    "change",
    [
        {"name": "../images"},
        {"name": "images\n"},
        {"mountPath": "/tmp"},
        {"mountPath": "/data/../etc"},
        {"driver": "local"},
        {"options": {"device": "/"}},
        {"hostPath": "/tmp"},
    ],
)
def test_storage_rejects_arbitrary_path_and_options(change):
    data = stateful()
    data["services"]["web"]["storage"].update(change)
    with pytest.raises(DeployError):
        load(data)


def test_storage_names_cannot_be_shared_by_services():
    data = stateful()
    data["services"]["other"] = copy.deepcopy(data["services"]["web"])
    with pytest.raises(DeployError, match="shared"):
        load(data)


@pytest.mark.parametrize("change", ["remove", "rename", "service"])
def test_storage_cannot_be_silently_abandoned(change):
    previous = load(stateful())
    data = stateful()
    if change == "remove":
        data["services"]["web"].pop("storage")
    elif change == "rename":
        data["services"]["web"]["storage"]["name"] = "new"
    else:
        data["services"]["other"] = data["services"].pop("web")
        data["routes"][0]["service"] = "other"
    with pytest.raises(DeployError, match="Existing storage"):
        load(data, previous)


class Docker:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["volume", "ls"]:
            name = args[args.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
            return name if name in self.rows else ""
        if args[:2] == ["volume", "create"]:
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
            self.rows[args[-1]] = {
                "Name": args[-1],
                "Driver": "local",
                "Scope": "local",
                "Options": None,
                "Labels": labels,
                "CreatedAt": "2026-10-13T12:00:00Z",
            }
            return args[-1]
        if args[:2] == ["volume", "inspect"]:
            return json.dumps([self.rows[args[-1]]])
        raise AssertionError(args)


def test_volume_creation_reuse_and_app_isolation():
    from awesome_deploy.storage import ManagedStorage

    docker = Docker()
    storage = ManagedStorage(docker)
    first = storage.ensure("app1", "web", {"name": "images", "mountPath": "/data"})
    assert storage.ensure("app1", "web", {"name": "images", "mountPath": "/data"}, first) == first
    other = storage.ensure("app2", "web", {"name": "images", "mountPath": "/data"})
    assert first["name"] != other["name"]
    assert len([args for args in docker.calls if args[:2] == ["volume", "create"]]) == 2
    assert not any("rm" in args or "prune" in args for args in docker.calls)


@pytest.mark.parametrize(
    "change", ["owner", "driver", "options", "missing", "recreated", "same-second-recreated"]
)
def test_volume_conflict_and_missing_persisted_volume_fail_closed(change):
    from awesome_deploy.storage import ManagedStorage

    docker = Docker()
    storage = ManagedStorage(docker)
    spec = {"name": "images", "mountPath": "/data"}
    record = storage.ensure("app1", "web", spec)
    row = docker.rows[record["name"]]
    if change == "owner":
        row["Labels"]["awesome.app"] = "foreign"
    elif change == "driver":
        row["Driver"] = "nfs"
    elif change == "options":
        row["Options"] = {"type": "none", "device": "/", "o": "bind"}
    elif change == "missing":
        docker.rows.clear()
    elif change == "same-second-recreated":
        row["Labels"]["awesome.storage-instance"] = "f" * 32
    else:
        row["CreatedAt"] = "2026-10-14T12:00:00Z"
    with pytest.raises(DeployError):
        storage.ensure("app1", "web", spec, record)
    assert len([args for args in docker.calls if args[:2] == ["volume", "create"]]) == 1
