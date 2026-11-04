import json
import time

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.vault_service import VaultService
from test_vault_deployment_secrets import configuration as configuration


def seed(configuration):
    _, settings, store, vault, actor, job = configuration
    actor = {**actor, "authenticated_at": time.time()}
    with store._connection(write=True) as db:
        version = vault.create_missing(db, job["app_id"], "TOKEN", "synthetic-original-credential", actor_id=actor["id"])
    service = VaultService(settings, store)
    return service, settings, store, vault, actor, job, version


def test_real_vault_rotation_and_revoke_share_pointer_audit_and_operation_transactions(configuration):
    service, settings, store, vault, actor, job, original = seed(configuration)
    updated = service.rotate(job["app_id"], "TOKEN", "synthetic-replacement-credential", original, actor=actor)
    with pytest.raises(DeployError) as error:
        service.rotate(job["app_id"], "TOKEN", "stale-key-must-not-write", original, actor=actor)
    assert error.value.code == "SECRET_VERSION_CONFLICT"
    snapshot = {**job["snapshot"], "secret_versions": {"web": {"TOKEN": original}}}
    store.checkpoint(job["id"], snapshot=snapshot)
    operation = service.revoke(job["app_id"], "TOKEN", original, updated["current_version"],
                               "已通知供应商吊销", actor=actor)
    assert operation["status"] == "queued" and operation["execution"]["job_ids"] == [job["id"]]
    metadata = service.describe(job["app_id"], actor=actor)
    assert metadata["items"][0]["current_version"] == updated["current_version"]
    with store._connection() as db:
        with pytest.raises(DeployError) as error:
            vault.resolve(db, job["app_id"], {"TOKEN": "TOKEN"}, {"TOKEN": original})
        assert error.value.code == "SECRET_REVOKED"
        assert vault.resolve(db, job["app_id"], {"TOKEN": "TOKEN"},
                             {"TOKEN": updated["current_version"]})["TOKEN"] == "synthetic-replacement-credential"
        assert db.execute("SELECT count(*) FROM vault_audit WHERE action='secret_rotated'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM vault_audit WHERE action='secret_revoked'").fetchone()[0] == 1
        assert "synthetic-original-credential" not in " ".join(db.iterdump())
    assert "synthetic-replacement-credential" not in json.dumps(metadata)
    for path in settings.state_dir.glob("platform.sqlite3*"):
        assert b"synthetic-replacement-credential" not in path.read_bytes()


def test_real_revocation_journal_failure_rolls_back_cryptographic_state_and_audit(configuration):
    service, _, store, vault, actor, job, version = seed(configuration)
    with store._connection(write=True) as db:
        db.execute("""CREATE TRIGGER unavailable_journal BEFORE INSERT ON vault_revocations
                      BEGIN SELECT RAISE(FAIL, 'journal unavailable'); END""")
    with pytest.raises(DeployError) as error:
        service.revoke(job["app_id"], "TOKEN", version, version, "失效", actor=actor)
    assert error.value.code == "SECRET_UNAVAILABLE"
    with store._connection() as db:
        assert vault.metadata(db, job["app_id"])[0]["versions"] == [{"version": version, "status": "active"}]
        assert db.execute("SELECT count(*) FROM vault_audit WHERE action='secret_revoked'").fetchone()[0] == 0


def test_revocation_retry_rechecks_current_version_after_subsequent_rotation(configuration):
    service, _, _, _, actor, job, version = seed(configuration)
    operation = service.revoke(job["app_id"], "TOKEN", version, version, "失效", actor=actor)
    service.claim_revocation(operation["id"])
    service.fail_revocation(operation["id"], {"code": "GATEWAY_DEACTIVATE_FAILED"})
    new = service.rotate(job["app_id"], "TOKEN", "new-live-key", version, actor=actor)
    with pytest.raises(DeployError) as error:
        service.revoke(job["app_id"], "TOKEN", version, version, "失效", actor=actor)
    assert error.value.code == "SECRET_VERSION_CONFLICT"
    retry = service.revoke(job["app_id"], "TOKEN", version, new["current_version"], "失效", actor=actor)
    assert retry["id"] == operation["id"] and retry["status"] == "queued"
