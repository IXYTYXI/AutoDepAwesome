from dataclasses import replace
import json
import sqlite3

import pytest

from awesome_deploy import cli
from awesome_deploy.config import Settings
from awesome_deploy.secret_vault import SecretVault
from awesome_deploy.vault_migration import VaultMigration


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path / "state", auth_mode="feishu", feishu_tenant_key="tenant",
                        feishu_app_id="cli_test", secret_backend="encrypted", secret_kek_file=tmp_path / "credentials" / "root.json")
    backup = {"backup_reference": "offline-copy", "kek_recovery_reference": "independent-copy",
              "responsible": "operator", "verified_at": "2026-10-21T10:00:00+08:00",
              "retention_until": "2026-12-21T10:00:00+08:00"}
    record = tmp_path / "recovery.json"
    record.write_text(json.dumps(backup))
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-cli-secret")
    return settings, record


def test_explicit_key_provision_then_empty_platform_initialize_and_check(setup, capsys):
    settings, record = setup
    assert cli.main(["vault-keyring-init"]) == 0
    assert settings.secret_kek_file.exists()
    assert cli.main(["vault-init", "--recovery-record", str(record)]) == 0
    assert cli.main(["vault-check"]) == 0
    output = capsys.readouterr()
    assert "synthetic-cli-secret" not in output.out + output.err
    assert "active" in output.out


def test_encrypted_secret_set_requires_explicit_app_and_cas_for_replacement(setup, capsys):
    settings, record = setup
    migration = VaultMigration(settings)
    migration.provision(actor_id="operator")
    migration.initialize(json.loads(record.read_text()), actor_id="operator")
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO apps VALUES ('app','https://github.com/acme/demo.git',NULL,NULL,1,1)")
        SecretVault(settings).register_app(db, "app", "tenant", "generation", actor_id="operator")
    assert cli.main(["secret-set", "--app-id", "app", "--name", "API_KEY"]) == 0
    assert cli.main(["secret-set", "--app-id", "app", "--name", "API_KEY"]) == 1
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        version = SecretVault(settings).versions(db, "app", {"ENV": "API_KEY"})["API_KEY"]
    assert cli.main(["secret-set", "--app-id", "app", "--name", "API_KEY", "--expected-current-version", version,
                     "--reason", "供应商凭证轮换"]) == 0
    output = capsys.readouterr()
    assert "synthetic-cli-secret" not in output.out + output.err


def test_missing_key_on_existing_encrypted_platform_never_recreated(setup, capsys):
    settings, record = setup
    migration = VaultMigration(settings)
    migration.provision(actor_id="operator")
    migration.initialize(json.loads(record.read_text()), actor_id="operator")
    settings.secret_kek_file.unlink()
    assert cli.main(["vault-keyring-init"]) == 1
    assert not settings.secret_kek_file.exists()
    assert "VAULT_KEY_UNAVAILABLE" in capsys.readouterr().err


def test_migration_cli_accepts_legacy_config_only_with_explicit_external_key_path(setup, monkeypatch, capsys):
    settings, record = setup
    legacy = replace(settings, secret_backend="legacy", secret_kek_file=None)
    monkeypatch.setattr(cli, "load_settings", lambda path: legacy)
    assert cli.main(["vault-keyring-init", "--kek-file", str(settings.secret_kek_file)]) == 0
    assert cli.main(["vault-init", "--kek-file", str(settings.secret_kek_file), "--recovery-record", str(record)]) == 0
    assert "active" in capsys.readouterr().out


def test_check_accepts_authenticated_revoked_history_but_reports_unfinished_cleanup(setup, capsys):
    settings, record = setup
    migration = VaultMigration(settings)
    migration.provision(actor_id="operator")
    migration.initialize(json.loads(record.read_text()), actor_id="operator")
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO apps VALUES ('app','https://github.com/acme/demo.git',NULL,NULL,1,1)")
        vault = SecretVault(settings)
        vault.register_app(db, "app", "tenant", "generation", actor_id="operator")
        version = vault.create_missing(db, "app", "KEY", "synthetic-value", actor_id="operator")
        vault.revoke(db, "app", "KEY", version, version, actor_id="operator")
        snapshot = {"manifest": {"services": {"web": {"secretRefs": {"ENV": "KEY"}}}},
                    "secret_versions": {"web": {"KEY": version}},
                    "vault_identity": {"app_id": "app", "tenant_id": "tenant", "data_generation": "generation"}}
        db.execute("INSERT INTO jobs VALUES ('job','job','{}','app','succeeded','complete',?,'{\"resources_released\":true}',NULL,1,1)",
                   (json.dumps(snapshot),))
    assert cli.main(["vault-check"]) == 0
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO vault_revocations VALUES ('revocation','app','KEY',?,'queued','{}',NULL,1,1)", (version,))
    assert cli.main(["vault-check"]) == 1
    assert cli.main(["secret-set", "--app-id", "app", "--name", "OTHER"]) == 1
    # Rotation is independent of fixed-scope cleanup of the old revoked version.
    assert cli.main(["secret-set", "--app-id", "app", "--name", "KEY",
                     "--expected-current-version", version, "--reason", "replacement"]) == 0
    output = capsys.readouterr()
    assert "SECRET_REVOCATION_STATE" in output.err
    assert "synthetic" not in output.out + output.err


def test_legacy_cli_provisions_pending_manifest_then_requires_completed_recovery(setup, monkeypatch, capsys):
    from awesome_deploy.store import Store
    settings, record = setup
    legacy_settings = replace(settings, secret_backend="legacy", secret_kek_file=None)
    settings.state_dir.mkdir(mode=0o700)
    Store(legacy_settings)
    repository = "https://github.com/acme/demo.git"
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO apps VALUES ('app',?,'demo',NULL,1,1)", (repository,))
        db.execute("INSERT INTO app_secret_scopes VALUES (?,'demo','app','operator',1)", (repository,))
    manifest = {"format": 1, "migration_id": "a" * 32, "tenant_id": "tenant", "feishu_app_id": "cli_test",
                "backup": None, "applications": [{"app_id": "app", "data_generation": "generation",
                "job_ids": [], "scopes": [{"repository": repository, "subdomain": "demo", "secrets": []}]}]}
    manifest_path = record.with_name("migration.json")
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(cli, "load_settings", lambda path: legacy_settings)
    arguments = ["--kek-file", str(settings.secret_kek_file), "--manifest", str(manifest_path)]

    assert cli.main(["vault-keyring-init", *arguments]) == 0
    provision_output = json.loads(capsys.readouterr().out)
    assert provision_output["created"] is True
    assert "phase" not in provision_output
    assert cli.main(["vault-migrate", *arguments]) == 1
    assert "VAULT_RECOVERY_RECORD_REQUIRED" in capsys.readouterr().err
    manifest["backup"] = json.loads(record.read_text())
    manifest_path.write_text(json.dumps(manifest))
    assert cli.main(["vault-migrate", *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "active"
