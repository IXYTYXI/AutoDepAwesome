"""Encrypted deletion uses frozen operation scope; all storage is synthetic."""

import copy
import json

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.secret_vault import SecretVault
from awesome_deploy.storage import ManagedStorage
from awesome_deploy.store import Store
from awesome_deploy.vault_migration import VaultMigration
from test_offline_data import InventoryDocker


@pytest.fixture
def environment(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", auth_mode="feishu", feishu_app_id="cli_test",
                        feishu_tenant_key="tenant", secret_backend="encrypted",
                        secret_kek_file=tmp_path / "credentials" / "root.json")
    migration = VaultMigration(settings)
    migration.provision(actor_id="operator")
    migration.initialize({"backup_reference": "state-copy", "kek_recovery_reference": "root-copy",
                         "responsible": "operator", "verified_at": "2026-10-21T10:00:00+08:00",
                         "retention_until": "2026-12-21T10:00:00+08:00"}, actor_id="operator")
    store, vault, docker = Store(settings), SecretVault(settings), InventoryDocker()
    spec = {"name": "data", "mountPath": "/data"}
    expected = ManagedStorage(docker).ensure("app", "web", spec)
    metadata = {"app_id": "app", "job_ids": ["job"], "scopes": [
        {"repository": "https://github.com/acme/demo.git", "subdomain": "demo"}],
        "volumes": [{"service": "web", "spec": spec, "expected": expected}]}
    with store._connection(write=True) as db:
        db.execute("INSERT INTO apps VALUES ('app','https://github.com/acme/demo.git','demo','job',1,1)")
        db.execute("INSERT INTO jobs VALUES ('job','job','{}','app','succeeded','complete','{}','{}',NULL,1,1)")
        for app in ("app", "other"):
            vault.register_app(db, app, "tenant", "generation", actor_id="operator")
            vault.create_missing(db, app, "MASTER_KEY", "synthetic-master", kind="platform_managed_master", actor_id="operator")
            vault.create_missing(db, app, "API_KEY", "synthetic-api", actor_id="operator")
        db.execute("INSERT INTO offline_operations VALUES ('op','op','app','job','running','offline',?,NULL,1,1)",
                   (json.dumps({"data_policy": "delete", "deletion_metadata": metadata}),))
        db.execute("INSERT INTO operation_auth VALUES ('offline','op','requester',1,'app',1,'delete','request')")
    backend = DeploymentBackend(settings)
    backend._docker = docker
    return settings, store, vault, docker, backend, metadata


def prepared(environment):
    _, store, _, _, backend, metadata = environment
    plan = backend.inventory_offline_data(metadata)
    with store._connection(write=True) as db:
        state = json.loads(db.execute("SELECT execution FROM offline_operations WHERE id='op'").fetchone()[0])
        state.update(deletion_plan=plan, authorization_started=True, cleanup_confirmed=True)
        db.execute("UPDATE offline_operations SET execution=? WHERE id='op'", (json.dumps(state),))
    return plan


def test_v2_deletes_only_exact_app_generation_and_keeps_plaintext_sentinel(environment):
    settings, store, vault, docker, backend, _ = environment
    before = (settings.state_dir / "secrets").read_bytes()
    plan = prepared(environment)
    assert plan["version"] == 2
    assert "synthetic" not in json.dumps(plan)
    assert "ciphertext" not in json.dumps(plan)
    backend.delete_offline_data(plan, lambda patch: store.checkpoint_offline("op", patch))
    assert docker.rows == {}
    assert (settings.state_dir / "secrets").read_bytes() == before
    with store._connection() as db:
        assert db.execute("SELECT state FROM vault_apps WHERE app_id='app'").fetchone()[0] == "destroyed"
        assert len(vault.metadata(db, "other")) == 2
        assert db.execute("SELECT actor_id FROM vault_audit WHERE action='secrets_destroyed'").fetchone()[0] == "requester"
    backend.delete_offline_data(plan, lambda patch: store.checkpoint_offline("op", patch))
    assert len([a for a in docker.calls if a[:2] == ["volume", "rm"]]) == 1


@pytest.mark.parametrize("change", ["new_version", "revoked", "new_generation", "wrong_operation", "unconfirmed", "journal", "legacy_plan"])
def test_changed_scope_blocks_before_any_volume_removal(environment, change):
    _, store, vault, docker, backend, _ = environment
    plan = prepared(environment)
    with store._connection(write=True) as db:
        if change == "new_version":
            current = vault.versions(db, "app", {"ENV": "API_KEY"})["API_KEY"]
            vault.rotate(db, "app", "API_KEY", "replacement", current, actor_id="operator")
        elif change == "revoked":
            current = vault.versions(db, "app", {"ENV": "API_KEY"})["API_KEY"]
            vault.revoke(db, "app", "API_KEY", current, current, actor_id="operator")
        elif change == "new_generation":
            vault.delete(db, plan["vault"], actor_id="operator")
            vault.register_app(db, "app", "tenant", "new-generation", actor_id="operator")
        elif change == "wrong_operation":
            db.execute("UPDATE offline_operations SET status='failed' WHERE id='op'")
        elif change == "unconfirmed":
            state = json.loads(db.execute("SELECT execution FROM offline_operations WHERE id='op'").fetchone()[0])
            state.pop("cleanup_confirmed")
            db.execute("UPDATE offline_operations SET execution=?", (json.dumps(state),))
        elif change == "journal":
            plan["metadata"]["job_ids"].append("foreign-job")
        else:
            plan = {"version": 1, "metadata": plan["metadata"], "secrets": []}
    with pytest.raises(DeployError):
        backend.delete_offline_data(plan, lambda _: None)
    assert docker.rows
    assert not any(a[:2] == ["volume", "rm"] for a in docker.calls)


@pytest.mark.parametrize("point", ["volumes_deleted", "data_deleted"])
def test_retry_after_each_durable_partial_delete_never_recreates_secrets(environment, point):
    _, store, vault, docker, backend, _ = environment
    plan = prepared(environment)
    def interrupted(patch):
        store.checkpoint_offline("op", patch)
        if patch.get(point):
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        backend.delete_offline_data(plan, interrupted)
    backend.delete_offline_data(plan, lambda patch: store.checkpoint_offline("op", patch))
    with store._connection() as db:
        assert db.execute("SELECT count(*) FROM vault_tombstones WHERE app_id='app'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM vault_app_keys WHERE app_id='app'").fetchone()[0] == 0
    assert not docker.rows


def test_volume_replacement_is_checked_before_vault_delete(environment):
    _, store, vault, docker, backend, metadata = environment
    plan = prepared(environment)
    volume = metadata["volumes"][0]
    storage = ManagedStorage(docker)
    storage.remove("app", **volume)
    storage.ensure("app", volume["service"], volume["spec"])
    with pytest.raises(DeployError):
        backend.delete_offline_data(plan, lambda _: None)
    with store._connection() as db:
        assert len(vault.metadata(db, "app")) == 2
    assert docker.rows


def test_inventory_requires_matching_authorized_deletion_intent(environment):
    _, store, _, _, backend, metadata = environment
    with store._connection(write=True) as db:
        db.execute("DELETE FROM operation_auth")
    with pytest.raises(DeployError):
        backend.inventory_offline_data(copy.deepcopy(metadata))
