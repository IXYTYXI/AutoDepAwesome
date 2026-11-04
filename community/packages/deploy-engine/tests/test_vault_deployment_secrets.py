import copy
import re

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.deployment_secrets import DeploymentSecrets
from awesome_deploy.errors import DeployError
from awesome_deploy.manifest import load_manifest
from awesome_deploy.secret_vault import SecretVault
from awesome_deploy.store import Store
from awesome_deploy.vault_format import ensure_schema, set_format
from test_access import payload, user
from test_secret_vault import keyring
from test_source import manifest


@pytest.fixture
def configuration(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    keyring(credentials / "kek.json")
    settings = Settings(state_dir=state, auth_mode="feishu", feishu_app_id="cli_test", feishu_tenant_key="tenant")
    store = Store(settings)
    actor = user(settings)
    job = store.enqueue(payload(), "first", actor=actor)
    with store._connection(write=True) as db:
        object.__setattr__(settings, "secret_backend", "encrypted")
        object.__setattr__(settings, "secret_kek_file", credentials / "kek.json")
        vault = SecretVault(settings)
        ensure_schema(db)
        set_format(db, settings, phase="active", migration_id="fixture")
        vault.ensure_schema(db)
        vault.register_app(db, job["app_id"], "tenant", "generation", actor_id=actor["id"])
    data = load_manifest(manifest(), settings)
    data["services"]["web"]["secretRefs"] = {"PASSWORD": "PASSWORD", "MASTER_KEY": "encryption-key"}
    snapshot = {"repository": job["input"]["repository"], "commit": "a" * 40, "manifest": data}
    store.checkpoint(job["id"], phase="freeze", snapshot=snapshot, execution={"commit": "a" * 40})
    job = store.fail(job["id"], {"code": "SECRET_NOT_FOUND", "message": "missing"})
    return DeploymentSecrets(settings, store), settings, store, vault, actor, job


def test_encrypted_configuration_generates_aliased_master_and_keeps_plaintext_out_of_state(configuration):
    api, settings, store, vault, actor, job = configuration
    status = api.describe(job["id"], actor=actor)
    assert {item["name"]: item["managed"] for item in status["items"]} == {
        "PASSWORD": False, "encryption-key": True}
    saved = api.save(job["id"], {"PASSWORD": "synthetic-login-password"}, actor=actor)
    assert all(item["state"] == "configured" for item in saved["items"])
    with store._connection() as db:
        result = vault.resolve(db, job["app_id"], {"MASTER_KEY": "encryption-key"},
                               vault.versions(db, job["app_id"], {"MASTER_KEY": "encryption-key"}))
        assert re.fullmatch(r"[0-9a-f]{64}", result["MASTER_KEY"])
        values = ("synthetic-login-password", result["MASTER_KEY"])
        for value in values:
            assert value not in str(saved) and value not in " ".join(db.iterdump())
    assert not (settings.state_dir / "secrets").exists()
    assert api.save(job["id"], {}, actor=actor) == saved


def test_existing_managed_kind_survives_yaml_relabeling(configuration):
    api, _, store, vault, actor, job = configuration
    with store._connection(write=True) as db:
        version = vault.create_missing(db, job["app_id"], "encryption-key", "a" * 64,
                                       kind="platform_managed_master", actor_id=actor["id"])
    snapshot = copy.deepcopy(job["snapshot"])
    snapshot["manifest"]["services"]["web"]["secretRefs"] = {"TOKEN": "encryption-key"}
    store.checkpoint(job["id"], snapshot=snapshot)
    assert api.describe(job["id"], actor=actor)["items"][0]["managed"] is True
    with pytest.raises(DeployError) as error:
        api.save(job["id"], {"encryption-key": "replacement"}, actor=actor)
    assert error.value.code == "SECRET_MANAGED_INPUT"
    with store._connection() as db:
        assert vault.versions(db, job["app_id"], {"TOKEN": "encryption-key"}) == {"encryption-key": version}


def test_runtime_secret_cannot_be_reclassified_as_master_by_yaml(configuration):
    api, _, store, vault, actor, job = configuration
    with store._connection(write=True) as db:
        vault.create_missing(db, job["app_id"], "encryption-key", "old-key", actor_id=actor["id"])
    with pytest.raises(DeployError) as error:
        api.describe(job["id"], actor=actor)
    assert error.value.code == "SECRET_KIND_CONFLICT"


def test_generation_failure_rolls_back_entire_encrypted_batch(configuration, monkeypatch):
    api, _, store, vault, actor, job = configuration
    import secrets
    monkeypatch.setattr(secrets, "token_hex", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic-private")))
    with pytest.raises(DeployError) as error:
        api.save(job["id"], {"PASSWORD": "synthetic-private"}, actor=actor)
    assert "synthetic-private" not in str(error.value)
    with store._connection() as db:
        assert vault.metadata(db, job["app_id"]) == []


def test_missing_vault_registration_is_never_recreated_from_failed_job(configuration):
    api, _, store, _, actor, job = configuration
    with store._connection(write=True) as db:
        db.execute("DELETE FROM vault_apps WHERE app_id=?", (job["app_id"],))
    with pytest.raises(DeployError) as error:
        api.save(job["id"], {"PASSWORD": "new-pass"}, actor=actor)
    assert error.value.code == "VAULT_APP_UNREGISTERED"
    with store._connection() as db:
        assert db.execute("SELECT count(*) FROM vault_apps").fetchone()[0] == 0


def test_revoked_configured_value_is_not_treated_as_missing(configuration):
    api, _, store, vault, actor, job = configuration
    with store._connection(write=True) as db:
        version = vault.create_missing(db, job["app_id"], "PASSWORD", "password", actor_id=actor["id"])
        vault.revoke(db, job["app_id"], "PASSWORD", version, version, actor_id=actor["id"])
    state = {item["name"]: item["state"] for item in api.describe(job["id"], actor=actor)["items"]}
    assert state["PASSWORD"] == "unavailable"
    with pytest.raises(DeployError) as error:
        api.save(job["id"], {"PASSWORD": "replacement"}, actor=actor)
    assert error.value.code == "SECRET_UNAVAILABLE"
