"""Migration uses synthetic values and a stopped-worker local database only."""

from dataclasses import replace
import base64
import importlib
import json
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secrets_store import SecretStore
from awesome_deploy.store import Store


@pytest.fixture
def legacy(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", auth_mode="feishu", feishu_app_id="cli_test",
                        feishu_tenant_key="tenant")
    settings.state_dir.mkdir(mode=0o700)
    Store(settings)
    root = SecretStore(settings)
    repo = "https://github.com/acme/demo.git"
    old = root.set(repo, "demo", "MASTER_KEY", "original-data-master")
    current = root.set(repo, "demo", "MASTER_KEY", "current-data-master")
    snapshot = {"repository": repo, "commit": "f" * 40,
                "manifest": {"app": {"subdomain": "demo"}, "services": {
                    "web": {"secretRefs": {"MASTER_KEY": "MASTER_KEY"}}}},
                "secret_versions": {"web": {"MASTER_KEY": old}}}
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO apps VALUES (?,?,?,?,?,?)", ("app", repo, "demo", "job", 1, 1))
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ("job", "job", json.dumps({"repository": repo, "subdomain": "demo", "ref": "main"}),
                    "app", "succeeded", "complete", json.dumps(snapshot), '{"frozen":true}', None, 1, 1))
        db.execute("INSERT INTO app_secret_scopes VALUES (?,?,?,?,?)", (repo, "demo", "app", "operator", 1))
    target = replace(settings, secret_backend="encrypted", secret_kek_file=tmp_path / "credentials" / "root.json")
    backup = {"backup_reference": "offline-backup-1", "kek_recovery_reference": "independent-safe-1",
              "responsible": "operator", "verified_at": "2026-10-21T10:00:00+08:00",
              "retention_until": "2026-12-21T10:00:00+08:00"}
    manifest = {"format": 1, "migration_id": "a" * 32, "tenant_id": "tenant", "feishu_app_id": "cli_test",
                "backup": backup, "applications": [{"app_id": "app", "data_generation": "generation",
                "job_ids": ["job"], "scopes": [{"repository": repo, "subdomain": "demo",
                "secrets": [{"name": "MASTER_KEY", "kind": "platform_managed_master"}]}]}]}
    return settings, target, manifest, old, current


def test_migration_preserves_original_bytes_all_versions_jobs_and_quarantines_plaintext(legacy):
    old_settings, target, manifest, old, current = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    result = module.VaultMigration(target).migrate(manifest, actor_id="operator", create_keyring=True)
    assert result["phase"] == "active"
    assert (target.state_dir / "secrets").is_file()
    assert (target.state_dir / ("secrets-quarantine-" + manifest["migration_id"])).is_dir()
    from awesome_deploy.secret_vault import SecretVault
    from awesome_deploy.vault_format import require_ready
    with sqlite3.connect(target.database_path) as db:
        db.row_factory = sqlite3.Row
        require_ready(target, db)
        with pytest.raises(DeployError) as error:
            require_ready(old_settings, db)
        assert error.value.code == "VAULT_FORMAT_DOWNGRADE"
        vault = SecretVault(target)
        assert vault.versions(db, "app", {"ENV": "MASTER_KEY"}) == {"MASTER_KEY": current}
        assert vault.resolve(db, "app", {"ENV": "MASTER_KEY"}, {"MASTER_KEY": old})["ENV"] == "original-data-master"
        assert dict(db.execute("SELECT * FROM vault_legacy_jobs").fetchone()) == {
            "job_id": "job", "app_id": "app", "data_generation": "generation"}
        assert "original-data-master" not in " ".join(db.iterdump())
        assert "current-data-master" not in " ".join(db.iterdump())
    assert "data-master" not in json.dumps(result)


def test_migration_accepts_owned_legacy_lock_and_normalizes_permissions(legacy):
    _, target, manifest, _, _ = legacy
    lock = target.state_dir / "worker.lock"
    lock.touch(mode=0o644)
    lock.chmod(0o644)
    identity = lock.stat().st_ino
    module = importlib.import_module("awesome_deploy.vault_migration")
    result = module.VaultMigration(target).migrate(manifest, actor_id="operator", create_keyring=True)
    assert result["phase"] == "active"
    assert lock.stat().st_ino == identity
    assert lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["wrong_bytes", "missing_original"])
def test_completed_migration_retry_authenticates_existing_envelopes(legacy, failure):
    _, target, manifest, _, _ = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    migration = module.VaultMigration(target)
    migration.migrate(manifest, actor_id="operator", create_keyring=True)
    ring = json.loads(target.secret_kek_file.read_text())
    if failure == "wrong_bytes":
        ring["keys"][ring["active"]] = base64.b64encode(b"x" * 32).decode()
    else:
        ring["active"] = "replacement"
        ring["keys"] = {"replacement": base64.b64encode(b"x" * 32).decode()}
    target.secret_kek_file.write_text(json.dumps(ring))
    with pytest.raises(DeployError):
        migration.migrate(manifest, actor_id="operator")


def test_completed_migration_retry_accepts_valid_new_versions_and_revoked_history(legacy):
    _, target, manifest, _, _ = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    migration = module.VaultMigration(target)
    migration.migrate(manifest, actor_id="operator", create_keyring=True)
    from awesome_deploy.secret_vault import SecretVault
    with sqlite3.connect(target.database_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        vault = SecretVault(target)
        original = vault.create_missing(db, "app", "API_KEY", "initial", actor_id="operator")
        current = vault.rotate(db, "app", "API_KEY", "rotated", original, actor_id="operator")
        vault.revoke(db, "app", "API_KEY", original, current, actor_id="operator")
    assert migration.migrate(manifest, actor_id="operator")["phase"] == "active"


@pytest.mark.parametrize("interruption", ["maintenance", "imported", "sealing", "quarantined", "sealed"])
def test_interrupted_migration_replays_without_new_keys_or_logical_versions(legacy, interruption):
    _, target, manifest, old, current = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    def checkpoint(phase):
        if phase == interruption:
            raise RuntimeError("simulated interruption")
    with pytest.raises(RuntimeError):
        module.VaultMigration(target).migrate(manifest, actor_id="operator", create_keyring=True, checkpoint=checkpoint)
    before = target.secret_kek_file.read_bytes()
    result = module.VaultMigration(target).migrate(manifest, actor_id="operator")
    assert result["phase"] == "active"
    assert target.secret_kek_file.read_bytes() == before
    with sqlite3.connect(target.database_path) as db:
        assert set(row[0] for row in db.execute("SELECT version FROM vault_secret_versions")) == {old, current}


@pytest.mark.parametrize("problem", ["unknown_scope", "missing_kind", "missing_app", "missing_job", "changed_owner", "queued"])
def test_ambiguous_or_incomplete_migration_never_creates_root_or_moves_originals(legacy, problem):
    _, target, manifest, _, _ = legacy
    if problem == "unknown_scope":
        (target.state_dir / "secrets" / ("f" * 64)).mkdir(mode=0o700)
    elif problem == "missing_kind":
        del manifest["applications"][0]["scopes"][0]["secrets"][0]["kind"]
    elif problem == "missing_app":
        manifest["applications"] = []
    elif problem == "missing_job":
        manifest["applications"][0]["job_ids"] = []
    else:
        with sqlite3.connect(target.database_path) as db:
            if problem == "changed_owner":
                db.execute("UPDATE app_secret_scopes SET app_id='other'")
            else:
                db.execute("UPDATE jobs SET status='queued'")
    module = importlib.import_module("awesome_deploy.vault_migration")
    with pytest.raises(DeployError):
        module.VaultMigration(target).migrate(manifest, actor_id="operator", create_keyring=True)
    assert not target.secret_kek_file.exists()
    assert (target.state_dir / "secrets").is_dir()


def test_initialization_refuses_existing_apps_and_does_not_generate_root(legacy):
    _, target, manifest, _, _ = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    with pytest.raises(DeployError):
        module.VaultMigration(target).initialize(manifest["backup"], actor_id="operator", create_keyring=True)
    assert not target.secret_kek_file.exists()


def test_empty_initialization_resumes_after_seal_before_activation(tmp_path, monkeypatch):
    module = importlib.import_module("awesome_deploy.vault_migration")
    settings = Settings(state_dir=tmp_path / "state", auth_mode="feishu", feishu_app_id="cli_test",
                        feishu_tenant_key="tenant", secret_backend="encrypted",
                        secret_kek_file=tmp_path / "credentials" / "root.json")
    backup = {"backup_reference": "empty-state-copy", "kek_recovery_reference": "root-copy",
              "responsible": "operator", "verified_at": "2026-10-21T10:00:00+08:00",
              "retention_until": "2026-12-21T10:00:00+08:00"}
    migration = module.VaultMigration(settings)
    migration.provision(actor_id="operator")
    original = module.vault_format.set_format
    def interrupted(*args, **kwargs):
        if kwargs["phase"] == "active":
            raise RuntimeError("simulated crash")
        return original(*args, **kwargs)
    monkeypatch.setattr(module.vault_format, "set_format", interrupted)
    with pytest.raises(RuntimeError):
        migration.initialize(backup, actor_id="operator")
    monkeypatch.setattr(module.vault_format, "set_format", original)
    assert migration.initialize(backup, actor_id="operator")["phase"] == "active"


def test_changed_manifest_and_missing_key_cannot_resume_by_recreating_root(legacy):
    _, settings, manifest, _, _ = legacy
    module = importlib.import_module("awesome_deploy.vault_migration")
    migration = module.VaultMigration(settings)
    def pause(phase):
        if phase == "imported":
            raise RuntimeError("pause")
    with pytest.raises(RuntimeError):
        migration.migrate(manifest, actor_id="operator", create_keyring=True, checkpoint=pause)
    settings.secret_kek_file.unlink()
    with pytest.raises(DeployError):
        migration.migrate(manifest, actor_id="operator", create_keyring=True)
    assert not settings.secret_kek_file.exists()
    manifest["applications"][0]["data_generation"] = "replacement"
    with pytest.raises(DeployError):
        migration.migrate(manifest, actor_id="operator")
