import copy

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.storage import ManagedStorage
from test_storage import Docker


class DeletionDocker(Docker):
    def __init__(self):
        super().__init__()
        self.references = []
        self.keep_after_remove = False
        self.replace_when_checking_references = False
        self.timeouts = []

    def __call__(self, args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        if args[0] == "ps":
            self.calls.append(args)
            assert "-a" in args
            assert args[args.index("--filter") + 1].startswith("volume=awesome-data-")
            if self.replace_when_checking_references:
                for row in self.rows.values():
                    row["Labels"]["awesome.storage-instance"] = "f" * 32
            return "\n".join(self.references)
        if args[:2] == ["volume", "rm"]:
            self.calls.append(args)
            assert len(args) == 3  # No force flag or unrelated volume.
            if not self.keep_after_remove:
                del self.rows[args[-1]]
            return args[-1]
        return super().__call__(args, **kwargs)


@pytest.fixture
def owned():
    docker = DeletionDocker()
    storage = ManagedStorage(docker)
    spec = {"name": "images", "mountPath": "/data"}
    expected = storage.ensure("app1", "web", spec)
    docker.calls.clear()
    docker.timeouts.clear()
    return docker, storage, spec, expected


def removals(docker):
    return [args for args in docker.calls if args[:2] == ["volume", "rm"]]


def test_explicit_removal_checks_identity_references_and_confirms_absence(owned):
    docker, storage, spec, expected = owned
    assert storage.remove("app1", "web", spec, expected, timeout=7) is None
    assert expected["name"] not in docker.rows
    assert removals(docker) == [["volume", "rm", expected["name"]]]
    assert docker.calls[-1][:2] == ["volume", "ls"]
    assert set(docker.timeouts) == {7}
    assert not any("create" in args or "prune" in args for args in docker.calls)


def test_verify_removal_is_read_only_and_can_be_followed_by_remove(owned):
    docker, storage, spec, expected = owned
    assert storage.verify_removal("app1", "web", spec, expected) is None
    assert expected["name"] in docker.rows
    assert not removals(docker)
    storage.remove("app1", "web", spec, expected)
    assert expected["name"] not in docker.rows


@pytest.mark.parametrize("method", ["remove", "verify_removal"])
def test_missing_original_is_an_idempotent_success_without_creation(owned, method):
    docker, storage, spec, expected = owned
    docker.rows.clear()
    assert getattr(storage, method)("app1", "web", spec, expected) is None
    assert not removals(docker)
    assert all(args[:2] == ["volume", "ls"] for args in docker.calls)


@pytest.mark.parametrize("method", ["remove", "verify_removal"])
@pytest.mark.parametrize(
    "invalid",
    [None, {}, {"name": "foreign"}, {"created_at": ""}, {"created_at": 1},
     {"instance": "f" * 31}, {"extra": "value"}],
)
def test_missing_volume_still_requires_strict_expected_identity(owned, method, invalid):
    docker, storage, spec, expected = owned
    docker.rows.clear()
    if invalid is not None and invalid:
        invalid = {**expected, **invalid}
    with pytest.raises(DeployError) as error:
        getattr(storage, method)("app1", "web", spec, invalid)
    assert error.value.code == "STORAGE_IDENTITY"
    assert not docker.calls


@pytest.mark.parametrize("method", ["remove", "verify_removal"])
@pytest.mark.parametrize("changed", ["owner", "driver", "scope", "options", "created_at", "instance"])
def test_foreign_or_replaced_volume_is_never_removed(owned, method, changed):
    docker, storage, spec, expected = owned
    row = docker.rows[expected["name"]]
    if changed == "owner":
        row["Labels"]["awesome.app"] = "foreign"
    elif changed == "driver":
        row["Driver"] = "nfs"
    elif changed == "scope":
        row["Scope"] = "global"
    elif changed == "options":
        row["Options"] = {"device": "/", "o": "bind", "type": "none"}
    elif changed == "created_at":
        row["CreatedAt"] = "2026-10-15T12:00:00Z"
    else:
        row["Labels"]["awesome.storage-instance"] = "f" * 32
    with pytest.raises(DeployError):
        getattr(storage, method)("app1", "web", spec, expected)
    assert not removals(docker)
    assert expected["name"] in docker.rows


@pytest.mark.parametrize("method", ["remove", "verify_removal"])
@pytest.mark.parametrize("container", ["running-container", "stopped-container"])
def test_any_container_reference_blocks_removal_including_stopped(owned, method, container):
    docker, storage, spec, expected = owned
    docker.references = [container]
    with pytest.raises(DeployError) as error:
        getattr(storage, method)("app1", "web", spec, expected)
    assert error.value.code == "STORAGE_IN_USE"
    assert not removals(docker)


def test_removal_rechecks_instance_after_reference_query(owned):
    docker, storage, spec, expected = owned
    docker.replace_when_checking_references = True
    with pytest.raises(DeployError) as error:
        storage.remove("app1", "web", spec, expected)
    assert error.value.code == "STORAGE_IDENTITY"
    assert not removals(docker)


def test_successful_rm_response_is_not_enough_if_volume_remains(owned):
    docker, storage, spec, expected = owned
    docker.keep_after_remove = True
    with pytest.raises(DeployError) as error:
        storage.remove("app1", "web", spec, expected)
    assert error.value.code == "STORAGE_DELETE_FAILED"
    assert expected["name"] in docker.rows


@pytest.mark.parametrize("method", ["remove", "verify_removal"])
def test_unmanaged_mount_path_is_rejected_before_docker(owned, method):
    docker, storage, spec, expected = owned
    altered = copy.deepcopy(spec)
    altered["mountPath"] = "/etc"
    with pytest.raises(DeployError) as error:
        getattr(storage, method)("app1", "web", altered, expected)
    assert error.value.code == "STORAGE_IDENTITY"
    assert not docker.calls
