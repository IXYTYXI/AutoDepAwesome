"""Synthetic-only acceptance tests for the transactional encrypted vault."""

import base64
from dataclasses import replace
import importlib
import json
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


def keyring(path, *, active="first", keys=None):
    keys = keys or {"first": b"a" * 32}
    path.write_text(json.dumps({"format": 1, "active": active,
                               "keys": {name: base64.b64encode(value).decode() for name, value in keys.items()}}))
    path.chmod(0o600)


@pytest.fixture
def vault(tmp_path):
    module = importlib.import_module("awesome_deploy.secret_vault")
    secret_file = tmp_path / "credentials" / "kek.json"
    secret_file.parent.mkdir(mode=0o700)
    keyring(secret_file)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    settings = Settings(state_dir=state, secret_backend="encrypted", secret_kek_file=secret_file,
                        auth_mode="feishu", feishu_tenant_key="tenant")
    db = sqlite3.connect(state / "vault.sqlite3", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    module.SecretVault.ensure_schema(db)
    db.execute("BEGIN IMMEDIATE")
    value = module.SecretVault(settings)
    value.register_app(db, "app-a", "tenant", "generation-a", actor_id="operator")
    value.register_app(db, "app-b", "tenant", "generation-b", actor_id="operator")
    yield value, db, settings
    db.close()


def create(vault, name="KEY", value="synthetic-vault-secret-é", **kwargs):
    store, db, _ = vault
    return store.create_missing(db, "app-a", name, value, actor_id="actor", **kwargs)


def test_encrypted_write_safe_metadata_and_no_plaintext_in_db_or_wal(vault):
    store, db, settings = vault
    version = create(vault)
    assert store.versions(db, "app-a", {"ENV": "KEY"}) == {"KEY": version}
    assert store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": version}) == {"ENV": "synthetic-vault-secret-é"}
    metadata = store.metadata(db, "app-a")
    assert metadata == [{"name": "KEY", "kind": "runtime_secret", "current_version": version,
                         "data_generation": "generation-a", "versions": [{"version": version, "status": "active"}]}]
    db.commit()
    for path in settings.state_dir.iterdir():
        assert "synthetic-vault-secret-é".encode() not in path.read_bytes()
    assert "synthetic-vault-secret" not in " ".join(db.iterdump())


def test_caller_rollback_rolls_back_value_pointer_and_audit(vault):
    store, db, _ = vault
    db.commit()
    before = db.execute("SELECT count(*) FROM vault_audit").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    create(vault)
    db.rollback()
    assert store.metadata(db, "app-a") == []
    assert db.execute("SELECT count(*) FROM vault_audit").fetchone()[0] == before


def test_verify_deletion_is_readonly_and_checks_exact_live_or_destroyed_generation(vault):
    store, db, _ = vault
    current = create(vault)
    plan = store.inventory(db, "app-a")
    db.commit()
    before = " ".join(db.iterdump())
    store.verify_deletion(db, plan)
    assert " ".join(db.iterdump()) == before
    db.execute("BEGIN IMMEDIATE")
    store.rotate(db, "app-a", "KEY", "replacement", current, actor_id="actor")
    with pytest.raises(DeployError):
        store.verify_deletion(db, plan)
    db.rollback()
    db.execute("BEGIN IMMEDIATE")
    store.delete(db, plan, actor_id="actor")
    db.commit()
    store.verify_deletion(db, plan)


@pytest.mark.parametrize("residual", ["vault_app_keys", "vault_secrets", "vault_secret_versions", "changed_dek"])
def test_destroyed_tombstone_does_not_hide_residual_envelopes_or_changed_key_identity(vault, residual):
    store, db, _ = vault
    create(vault)
    plan = store.inventory(db, "app-a")
    if residual != "changed_dek":
        original = db.execute(f"SELECT * FROM {residual} WHERE app_id='app-a'").fetchone()
    store.delete(db, plan, actor_id="actor")
    if residual == "changed_dek":
        db.execute("UPDATE vault_apps SET dek_id=? WHERE app_id='app-a'", ("f" * 32,))
    else:
        db.execute(f"INSERT INTO {residual} VALUES ({','.join('?' for _ in original)})", tuple(original))
    with pytest.raises(DeployError):
        store.verify_deletion(db, plan)


def test_audit_failure_rolls_back_mutation_even_when_caller_catches(vault):
    store, db, _ = vault
    db.execute("CREATE TRIGGER broken_audit BEFORE INSERT ON vault_audit BEGIN SELECT RAISE(FAIL, 'audit unavailable'); END")
    with pytest.raises(DeployError):
        create(vault)
    assert store.metadata(db, "app-a") == []
    assert db.in_transaction


def test_rotation_preserves_frozen_versions_and_checks_expected_current(vault):
    store, db, _ = vault
    original = create(vault)
    current = store.rotate(db, "app-a", "KEY", "replacement", original, actor_id="actor")
    assert current != original
    assert store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": original})["ENV"] == "synthetic-vault-secret-é"
    with pytest.raises(DeployError) as error:
        store.rotate(db, "app-a", "KEY", "lost-update", original, actor_id="actor")
    assert error.value.code == "SECRET_VERSION_CONFLICT"
    assert store.versions(db, "app-a", {"ENV": "KEY"}) == {"KEY": current}


def test_revocation_blocks_fixed_versions_without_selecting_a_replacement(vault):
    store, db, _ = vault
    old = create(vault)
    new = store.rotate(db, "app-a", "KEY", "replacement", old, actor_id="actor")
    store.revoke(db, "app-a", "KEY", old, new, actor_id="actor")
    for operation in (lambda: store.validate_versions(db, "app-a", {"KEY": old}),
                      lambda: store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": old})):
        with pytest.raises(DeployError) as error:
            operation()
        assert error.value.code == "SECRET_REVOKED"
    assert store.versions(db, "app-a", {"ENV": "KEY"}) == {"KEY": new}
    store.revoke(db, "app-a", "KEY", new, new, actor_id="actor")
    with pytest.raises(DeployError, match="吊销"):
        store.versions(db, "app-a", {"ENV": "KEY"})


def test_persistent_master_kind_blocks_ordinary_rotation_and_revocation(vault):
    store, db, _ = vault
    version = create(vault, name="renamed-master", kind="platform_managed_master")
    for operation in (lambda: store.rotate(db, "app-a", "renamed-master", "other", version, actor_id="actor"),
                      lambda: store.revoke(db, "app-a", "renamed-master", version, version, actor_id="actor")):
        with pytest.raises(DeployError) as error:
            operation()
        assert error.value.code == "SECRET_MASTER_PROTECTED"
    assert store.resolve(db, "app-a", {"OTHER_ENV": "renamed-master"}, {"renamed-master": version})


@pytest.mark.parametrize("value", [None, True, 12, "", "a\nb", "a\rb", "a\0b", "é" * 8193, "\ud800"])
def test_byte_and_single_line_validation_precedes_all_writes(vault, value):
    store, db, _ = vault
    before = db.total_changes
    with pytest.raises(DeployError) as error:
        create(vault, value=value)
    assert error.value.code == "INVALID_SECRET"
    assert db.total_changes == before
    assert store.metadata(db, "app-a") == []


def test_utf8_exact_limit_and_preserved_import_version(vault):
    store, db, _ = vault
    version = create(vault, value="é" * 8192, version="1" * 32)
    assert version == "1" * 32
    assert store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": version})["ENV"] == "é" * 8192


@pytest.mark.parametrize("field,value", [("kind", "platform_managed_master"), ("data_generation", "other"),
                                         ("dek_id", "f" * 32), ("format", 9), ("ciphertext", b"tampered")])
def test_authenticated_metadata_and_ciphertext_tampering_fail_closed(vault, field, value):
    store, db, _ = vault
    version = create(vault)
    db.execute(f"UPDATE vault_secret_versions SET {field}=? WHERE app_id='app-a'", (value,))
    for operation in (lambda: store.metadata(db, "app-a"),
                      lambda: store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": version})):
        with pytest.raises(DeployError) as error:
            operation()
        assert error.value.code == "SECRET_UNAVAILABLE"


def test_ciphertext_swapping_across_apps_rejected(vault):
    store, db, _ = vault
    a = create(vault)
    b = store.create_missing(db, "app-b", "KEY", "other-value", actor_id="actor")
    row = db.execute("SELECT nonce,ciphertext FROM vault_secret_versions WHERE app_id='app-a'").fetchone()
    db.execute("UPDATE vault_secret_versions SET nonce=?,ciphertext=? WHERE app_id='app-b'", tuple(row))
    with pytest.raises(DeployError):
        store.resolve(db, "app-b", {"ENV": "KEY"}, {"KEY": b})
    assert store.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": a})


def test_rewrap_keeps_business_versions_and_old_key_not_required_afterwards(vault):
    store, db, settings = vault
    version = create(vault, kind="platform_managed_master")
    keyring(settings.secret_kek_file, active="second", keys={"first": b"a" * 32, "second": b"b" * 32})
    current = type(store)(settings)
    current.rewrap(db, actor_id="operator")
    keyring(settings.secret_kek_file, active="second", keys={"second": b"b" * 32})
    current = type(store)(settings)
    assert current.resolve(db, "app-a", {"ENV": "KEY"}, {"KEY": version})["ENV"] == "synthetic-vault-secret-é"
    assert current.versions(db, "app-a", {"ENV": "KEY"}) == {"KEY": version}


def test_exact_deletion_plan_rejects_new_versions_and_replays_with_tombstone(vault):
    store, db, _ = vault
    version = create(vault)
    plan = store.inventory(db, "app-a")
    assert "synthetic-vault-secret" not in json.dumps(plan)
    store.rotate(db, "app-a", "KEY", "new", version, actor_id="actor")
    with pytest.raises(DeployError) as error:
        store.delete(db, plan, actor_id="actor")
    assert error.value.code == "SECRET_DELETE_CONFLICT"
    plan = store.inventory(db, "app-a")
    store.delete(db, plan, actor_id="actor")
    store.delete(db, plan, actor_id="actor")
    assert db.execute("SELECT count(*) FROM vault_tombstones").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM vault_secret_versions WHERE app_id='app-a'").fetchone()[0] == 0
    with pytest.raises(DeployError):
        store.register_app(db, "app-a", "tenant", "generation-a", actor_id="actor")
    store.register_app(db, "app-a", "tenant", "new-generation", actor_id="actor")
    with pytest.raises(DeployError):
        store.delete(db, plan, actor_id="actor")


def test_keyring_missing_unsafe_or_in_state_never_generates_replacement(vault):
    store, db, settings = vault
    create(vault)
    for mode in (0o644, 0o400):
        settings.secret_kek_file.chmod(mode)
        with pytest.raises(DeployError):
            type(store)(settings)
    settings.secret_kek_file.chmod(0o600)
    inside = settings.state_dir / "key.json"
    keyring(inside)
    with pytest.raises((DeployError, ValueError)):
        type(store)(replace(settings, secret_kek_file=inside))
    settings.secret_kek_file.unlink()
    with pytest.raises(DeployError):
        type(store)(settings)
    assert not settings.secret_kek_file.exists()


def test_no_implicit_write_transaction_or_schema_commit(vault):
    store, db, _ = vault
    db.commit()
    with pytest.raises(DeployError):
        create(vault)
    db.execute("BEGIN IMMEDIATE")
    create(vault)
    store.ensure_schema(db)
    assert db.in_transaction
    db.rollback()
    assert store.metadata(db, "app-a") == []


def test_import_all_versions_preserves_ids_and_current_and_is_idempotent(vault):
    store, db, _ = vault
    a, b = "a" * 32, "b" * 32
    store.import_version(db, "app-a", "MASTER_KEY", "old-value", kind="platform_managed_master",
                         version=a, actor_id="operator")
    store.import_version(db, "app-a", "MASTER_KEY", "original-master", kind="platform_managed_master",
                         version=b, make_current=True, actor_id="operator")
    before = db.execute("SELECT count(*) FROM vault_audit").fetchone()[0]
    store.import_version(db, "app-a", "MASTER_KEY", "original-master", kind="platform_managed_master",
                         version=b, make_current=True, actor_id="operator")
    assert db.execute("SELECT count(*) FROM vault_audit").fetchone()[0] == before
    assert store.versions(db, "app-a", {"ENV": "MASTER_KEY"}) == {"MASTER_KEY": b}
    assert store.resolve(db, "app-a", {"ENV": "MASTER_KEY"}, {"MASTER_KEY": a}) == {"ENV": "old-value"}
    with pytest.raises(DeployError):
        store.import_version(db, "app-a", "MASTER_KEY", "wrong-value", kind="platform_managed_master",
                             version=b, actor_id="operator")
    with pytest.raises(DeployError):
        store.import_version(db, "app-a", "MASTER_KEY", "other", kind="runtime_secret",
                             version="c" * 32, actor_id="operator")


def test_destroyed_state_without_proof_cannot_create_a_new_generation(vault):
    store, db, _ = vault
    create(vault, kind="platform_managed_master")
    db.execute("UPDATE vault_apps SET state='destroyed' WHERE app_id='app-a'")
    with pytest.raises(DeployError):
        store.register_app(db, "app-a", "tenant", "new-generation", actor_id="actor")


def test_repeated_nonce_is_rejected_and_no_second_version_persists(vault, monkeypatch):
    store, db, _ = vault
    module = importlib.import_module("awesome_deploy.secret_vault")
    monkeypatch.setattr(module.os, "urandom", lambda length: b"x" * length)
    version = create(vault)
    with pytest.raises(DeployError):
        store.rotate(db, "app-a", "KEY", "replacement", version, actor_id="actor")
    assert len(store.metadata(db, "app-a")[0]["versions"]) == 1


def test_unknown_key_and_cross_app_dek_swap_fail(vault):
    store, db, settings = vault
    create(vault)
    keyring(settings.secret_kek_file, active="unknown", keys={"unknown": b"z" * 32})
    with pytest.raises(DeployError):
        type(store)(settings).metadata(db, "app-a")
    keyring(settings.secret_kek_file)
    second = db.execute("SELECT nonce,ciphertext FROM vault_app_keys WHERE app_id='app-b'").fetchone()
    db.execute("UPDATE vault_app_keys SET nonce=?,ciphertext=? WHERE app_id='app-a'", tuple(second))
    with pytest.raises(DeployError):
        store.metadata(db, "app-a")


def test_orphaned_application_rows_never_authorize_recreating_the_dek(vault):
    store, db, _ = vault
    create(vault, kind="platform_managed_master")
    db.execute("DELETE FROM vault_apps WHERE app_id='app-a'")
    before = db.execute("SELECT count(*) FROM vault_app_keys WHERE app_id='app-a'").fetchone()[0]
    with pytest.raises(DeployError):
        store.register_app(db, "app-a", "tenant", "generation-a", actor_id="operator")
    assert db.execute("SELECT count(*) FROM vault_app_keys WHERE app_id='app-a'").fetchone()[0] == before
