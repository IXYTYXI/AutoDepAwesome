import copy

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.secrets_store import SecretStore
from awesome_deploy.storage import ManagedStorage
from test_storage_deletion import DeletionDocker


class InventoryDocker(DeletionDocker):
    def __call__(self, args, **kwargs):
        if args[:2] == ["volume", "ls"] and args[3].startswith("label=awesome.app="):
            self.calls.append(args)
            app_id = args[3].split("=", 2)[2]
            return "\n".join(name for name, row in self.rows.items() if row["Labels"]["awesome.app"] == app_id)
        return super().__call__(args, **kwargs)


@pytest.fixture
def environment(tmp_path):
    settings = Settings(state_dir=tmp_path)
    docker = InventoryDocker()
    storage = ManagedStorage(docker)
    spec = {"name": "data", "mountPath": "/data"}
    expected = storage.ensure("app1", "web", spec)
    secrets = SecretStore(settings)
    scope = {"repository": "https://github.com/acme/repo.git", "subdomain": "demo"}
    secrets.set(**scope, name="MASTER_KEY", value="private-value-not-in-plan")
    secrets.set(**scope, name="MASTER_KEY", value="rotated-private-value")
    other = {**scope, "subdomain": "other"}
    secrets.set(**other, name="MASTER_KEY", value="other-private-value")
    backend = DeploymentBackend(settings)
    backend._docker = docker
    metadata = {"app_id": "app1", "job_ids": ["old"], "scopes": [scope], "volumes": [
        {"service": "web", "spec": spec, "expected": expected},
    ]}
    return backend, docker, secrets, metadata, other


def test_inventory_is_readonly_and_delete_clears_only_frozen_targets(environment):
    backend, docker, secrets, metadata, other = environment
    before = copy.deepcopy(docker.rows)
    plan = backend.inventory_offline_data(metadata)
    assert docker.rows == before
    assert "private-value" not in str(plan)
    changes = []
    backend.delete_offline_data(plan, changes.append)
    assert docker.rows == {}
    assert secrets.inspect(**metadata["scopes"][0], name="MASTER_KEY") == "missing"
    assert secrets.inspect(**other, name="MASTER_KEY") == "configured"
    assert changes[-1]["data_deleted"] is True
    backend.delete_offline_data(plan, changes.append)
    assert len([a for a in docker.calls if a[:2] == ["volume", "rm"]]) == 1


def test_new_secret_after_inventory_blocks_before_any_volume_removal(environment):
    backend, docker, secrets, metadata, _ = environment
    plan = backend.inventory_offline_data(metadata)
    secrets.set(**metadata["scopes"][0], name="NEW_KEY", value="new-private-value")
    with pytest.raises(DeployError):
        backend.delete_offline_data(plan, lambda _: None)
    assert docker.rows
    assert not any(a[:2] == ["volume", "rm"] for a in docker.calls)


def test_foreign_container_reference_blocks_all_secrets_and_volume_deletion(environment):
    backend, docker, secrets, metadata, _ = environment
    plan = backend.inventory_offline_data(metadata)
    docker.references = ["stopped-foreign"]
    with pytest.raises(DeployError) as error:
        backend.delete_offline_data(plan, lambda _: None)
    assert error.value.code == "STORAGE_IN_USE"
    assert docker.rows
    assert secrets.inspect(**metadata["scopes"][0], name="MASTER_KEY") == "configured"


@pytest.mark.parametrize("when", ["inventory", "delete"])
def test_unrecorded_app_volume_blocks_instead_of_silently_leaving_data(environment, when):
    backend, docker, _, metadata, _ = environment
    plan = backend.inventory_offline_data(metadata) if when == "delete" else None
    ManagedStorage(docker).ensure("app1", "oldservice", {"name": "old", "mountPath": "/data"})
    with pytest.raises(DeployError) as error:
        if plan is None:
            backend.inventory_offline_data(metadata)
        else:
            backend.delete_offline_data(plan, lambda _: None)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"
    assert len(docker.rows) == 2


def test_missing_original_volume_is_allowed_but_never_recreated(environment):
    backend, docker, _, metadata, _ = environment
    docker.rows.clear()
    docker.calls.clear()
    plan = backend.inventory_offline_data(metadata)
    backend.delete_offline_data(plan, lambda _: None)
    assert not any(a[:2] == ["volume", "create"] for a in docker.calls)


def test_retry_after_volume_removed_never_accepts_replacement(environment):
    backend, docker, _, metadata, _ = environment
    plan = backend.inventory_offline_data(metadata)
    volume = metadata["volumes"][0]
    ManagedStorage(docker).remove("app1", **volume)
    ManagedStorage(docker).ensure("app1", volume["service"], volume["spec"])
    with pytest.raises(DeployError) as error:
        backend.delete_offline_data(plan, lambda _: None)
    assert error.value.code == "STORAGE_IDENTITY"
    assert docker.rows


@pytest.mark.parametrize("corruption", ["missing", "extra", "duplicate", "scope", "version", "metadata"])
def test_incomplete_or_expanded_journal_is_rejected_before_any_deletion(environment, corruption):
    backend, docker, secrets, metadata, other = environment
    plan = backend.inventory_offline_data(metadata)
    if corruption == "missing":
        plan["secrets"] = []
    elif corruption == "extra":
        plan["secrets"].append(secrets.inventory_scope(**other))
    elif corruption == "duplicate":
        plan["secrets"] *= 2
    elif corruption == "scope":
        plan["metadata"]["scopes"].append(metadata["scopes"][0])
    elif corruption == "version":
        plan["version"] = True
    else:
        plan["metadata"]["scopes"] = None
    with pytest.raises(DeployError) as error:
        backend.delete_offline_data(plan, lambda _: None)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"
    assert docker.rows
    assert secrets.inspect(**metadata["scopes"][0], name="MASTER_KEY") == "configured"
