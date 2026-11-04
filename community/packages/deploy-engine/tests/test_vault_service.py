"""Authorization and durable revocation journal, independent of the encryption adapter."""
import fcntl
import json
import time

import pytest

from awesome_deploy.access import AccessStore
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store
from test_access import payload, user


class VaultAdapter:
    """Transaction-aware test adapter: no values are persisted or exposed."""
    def metadata(self, db, app_id):
        rows = db.execute("SELECT * FROM test_versions WHERE app_id=? ORDER BY name,version", (app_id,))
        items = {}
        for row in rows:
            item = items.setdefault(row["name"], {"name": row["name"], "kind": row["kind"],
                "data_generation": "generation", "current_version": None, "versions": []})
            if row["is_current"]:
                item["current_version"] = row["version"]
            item["versions"].append({"version": row["version"], "status": row["status"]})
        return list(items.values())

    def rotate(self, db, app_id, name, value, expected_current_version, *, actor_id):
        assert db.in_transaction
        current = db.execute("SELECT * FROM test_versions WHERE app_id=? AND name=? AND is_current=1",
                             (app_id, name)).fetchone()
        if not current or current["version"] != expected_current_version:
            raise DeployError("SECRET_VERSION_CONFLICT", "版本变化")
        db.execute("UPDATE test_versions SET is_current=0 WHERE app_id=? AND name=?", (app_id, name))
        version = "b" * 32
        db.execute("INSERT INTO test_versions VALUES(?,?,?,?,?,?)",
                   (app_id, name, version, "active", current["kind"], 1))
        return version

    def revoke(self, db, app_id, name, version, expected_current_version, *, actor_id):
        assert db.in_transaction
        current = db.execute("SELECT version FROM test_versions WHERE app_id=? AND name=? AND is_current=1",
                             (app_id, name)).fetchone()
        if not current or current["version"] != expected_current_version:
            raise DeployError("SECRET_VERSION_CONFLICT", "版本变化")
        result = db.execute("UPDATE test_versions SET status='revoked' WHERE app_id=? AND name=? AND version=?",
                            (app_id, name, version))
        if result.rowcount != 1:
            raise DeployError("SECRET_NOT_FOUND", "版本不存在")


@pytest.fixture
def service(tmp_path):
    from awesome_deploy.vault_service import VaultService
    tmp_path.chmod(0o700)
    settings = Settings(state_dir=tmp_path, auth_mode="feishu", feishu_app_id="cli_test",
                        feishu_tenant_key="tenant")
    store = Store(settings)
    owner = {**user(settings), "authenticated_at": time.time()}
    job = store.enqueue(payload(), "first", actor=owner)
    # This adapter deliberately avoids any crypto fixture/KEK or plaintext persistence.
    with store._connection(write=True) as db:
        object.__setattr__(settings, "secret_backend", "encrypted")
        from awesome_deploy.vault_format import ensure_schema, set_format
        ensure_schema(db)
        set_format(db, settings, phase="active", migration_id="fixture")
        db.execute("""CREATE TABLE test_versions(app_id TEXT, name TEXT, version TEXT, status TEXT,
                                               kind TEXT, is_current INTEGER)""")
        db.execute("INSERT INTO test_versions VALUES(?,?,?,?,?,?)",
                   (job["app_id"], "TOKEN", "a" * 32, "active", "runtime_secret", 1))
    instance = VaultService(settings, store, vault=VaultAdapter())
    return instance, settings, store, owner, job


def test_new_service_api_is_present():
    import importlib.util
    assert importlib.util.find_spec("awesome_deploy.vault_service"), "Missing app-authorized vault service"


@pytest.mark.parametrize("role,read,rotate,revoke", [
    ("owner", True, True, True), ("maintainer", True, True, False),
    ("viewer", False, False, False), ("stranger", False, False, False),
    ("admin", True, True, True),
])
def test_vault_permission_matrix(service, role, read, rotate, revoke):
    api, settings, store, owner, job = service
    actor = owner
    if role != "owner":
        actor = {**user(settings, admin=role == "admin"), "authenticated_at": time.time()}
        if role in {"viewer", "maintainer"}:
            AccessStore(settings).set_member(owner, job["app_id"], actor["id"], role)
    for operation, allowed in (("describe", read), ("rotate", rotate), ("revoke", revoke)):
        args = (job["app_id"],) if operation == "describe" else (
            (job["app_id"], "TOKEN", "new-value", "a" * 32) if operation == "rotate" else
            (job["app_id"], "TOKEN", "a" * 32, "b" * 32 if rotate else "a" * 32, "外部凭证泄漏"))
        if allowed:
            getattr(api, operation)(*args, actor=actor)
        else:
            with pytest.raises(DeployError) as error:
                getattr(api, operation)(*args, actor=actor)
            assert error.value.code == "ACCESS_DENIED"


@pytest.mark.parametrize("mutation", ["rotate", "revoke"])
@pytest.mark.parametrize("age", [601, -1, None, float("nan")])
def test_mutations_require_recent_nonfuture_authentication(service, mutation, age):
    api, _, _, actor, job = service
    actor = {**actor, "authenticated_at": None if age is None else time.time() - age}
    args = (job["app_id"], "TOKEN", "secret", "a" * 32) if mutation == "rotate" else (
        job["app_id"], "TOKEN", "a" * 32, "a" * 32, "供应商凭证失效")
    with pytest.raises(DeployError) as error:
        getattr(api, mutation)(*args, actor=actor)
    assert error.value.code == "AUTH_REAUTH_REQUIRED"


@pytest.mark.parametrize("admin", [False, True])
def test_deployment_eligibility_required_even_for_admin(service, admin):
    api, settings, store, owner, job = service
    with store._connection(write=True) as db:
        db.execute("UPDATE auth_users SET can_deploy=0,is_admin=? WHERE id=?", (int(admin), owner["id"]))
    for operation, args in (("describe", (job["app_id"],)),
                            ("rotate", (job["app_id"], "TOKEN", "new-value", "a" * 32)),
                            ("revoke", (job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效"))):
        with pytest.raises(DeployError) as error:
            getattr(api, operation)(*args, actor=owner)
        assert error.value.code == "ACCESS_DENIED"


def test_old_actor_revision_cannot_mutate_after_revocation(service):
    api, _, store, actor, job = service
    with store._connection(write=True) as db:
        db.execute("UPDATE auth_users SET revision=revision+1 WHERE id=?", (actor["id"],))
    with pytest.raises(DeployError) as error:
        api.rotate(job["app_id"], "TOKEN", "new-value", "a" * 32, actor=actor)
    assert error.value.code == "ACCESS_DENIED"


@pytest.mark.parametrize("mutation", ["rotate", "revoke"])
def test_persistent_master_kind_cannot_be_mutated_generically(service, mutation):
    api, _, store, actor, job = service
    with store._connection(write=True) as db:
        db.execute("UPDATE test_versions SET kind='platform_managed_master'")
    args = (job["app_id"], "TOKEN", "new-value", "a" * 32) if mutation == "rotate" else (
        job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效")
    with pytest.raises(DeployError) as error:
        getattr(api, mutation)(*args, actor=actor)
    assert error.value.code == "SECRET_MASTER_PROTECTED"


def test_rotation_is_cas_and_holds_worker_lock(service):
    api, settings, _, actor, job = service
    with (settings.state_dir / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DeployError) as error:
            api.rotate(job["app_id"], "TOKEN", "new-value", "a" * 32, actor=actor)
        assert error.value.code == "SECRET_CONFIGURATION_BUSY"
    result = api.rotate(job["app_id"], "TOKEN", "new-value", "a" * 32, actor=actor)
    assert result == {"name": "TOKEN", "current_version": "b" * 32}
    with pytest.raises(DeployError) as error:
        api.rotate(job["app_id"], "TOKEN", "losing-value", "a" * 32, actor=actor)
    assert error.value.code == "SECRET_VERSION_CONFLICT"


def test_revocation_does_not_wait_for_worker_and_keeps_exact_job_scope(service):
    api, settings, store, actor, job = service
    store.checkpoint(job["id"], snapshot={"secret_versions": {"web": {"TOKEN": "a" * 32}}})
    with (settings.state_dir / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效", actor=actor)
    assert operation["status"] == "queued"
    assert operation["execution"]["job_ids"] == [job["id"]]
    with store._connection() as db:
        assert db.execute("SELECT status FROM test_versions").fetchone()[0] == "revoked"
    assert api.next_revocation()["id"] == operation["id"]
    assert api.claim_revocation(operation["id"])["status"] == "running"
    api.checkpoint_revocation(operation["id"], {"quarantined": [job["id"]]})
    api.fail_revocation(operation["id"], {"code": "GATEWAY_DEACTIVATE_FAILED", "message": "unsafe-output"})
    assert api.next_revocation() is None
    retried = api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效", actor=actor)
    assert retried["id"] == operation["id"] and retried["status"] == "queued"
    assert retried["execution"]["job_ids"] == [job["id"]]
    api.claim_revocation(operation["id"])
    api.checkpoint_revocation(operation["id"], {"confirmed_job_ids": [job["id"]]})
    assert api.finish_revocation(operation["id"])["status"] == "succeeded"
    assert api.next_revocation() is None


def test_revocation_rolls_back_state_if_journal_write_fails(service, monkeypatch):
    api, _, store, actor, job = service
    monkeypatch.setattr(api, "_affected_jobs", lambda *args: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(DeployError):
        api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效", actor=actor)
    with store._connection() as db:
        assert db.execute("SELECT status FROM test_versions").fetchone()[0] == "active"
        assert db.execute("SELECT count(*) FROM vault_revocations").fetchone()[0] == 0


def test_metadata_never_exposes_execution_reason_or_internal_error(service):
    api, _, _, actor, job = service
    operation = api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "reason-not-public", actor=actor)
    api.claim_revocation(operation["id"])
    api.fail_revocation(operation["id"], {"code": "REVOCATION_FAILED", "message": "sensitive-command-output"})
    result = api.describe(job["app_id"], actor=actor)
    assert result["can_rotate"] and result["can_revoke"]
    assert result["operations"][0]["status"] == "recovery_required"
    text = json.dumps(result)
    assert "reason-not-public" not in text and "sensitive-command-output" not in text
    assert "execution" not in result["operations"][0]


def test_checkpoint_cannot_expand_accepted_job_scope(service):
    api, _, _, actor, job = service
    operation = api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效", actor=actor)
    api.claim_revocation(operation["id"])
    with pytest.raises(DeployError):
        api.checkpoint_revocation(operation["id"], {"job_ids": ["another-release"]})


def test_encrypted_assignment_never_reclaims_legacy_file_namespace(service, monkeypatch):
    _, settings, store, owner, _ = service
    admin = user(settings, admin=True)
    with store._connection(write=True) as db:
        db.execute("INSERT INTO apps VALUES(?,?,?,?,?,?)",
                   ("legacy-app", "https://github.com/org/repo.git", "old", None, time.time(), time.time()))
    from awesome_deploy.secrets_store import SecretStore
    monkeypatch.setattr(SecretStore, "_scope_name", lambda *args: pytest.fail("Legacy scope must not be read"))
    AccessStore(settings).assign_owner(admin, "legacy-app", owner["id"])
    with store._connection() as db:
        assert db.execute("SELECT 1 FROM app_secret_scopes WHERE app_id='legacy-app'").fetchone() is None


def test_encrypted_operation_uses_app_identity_without_legacy_namespace_claim(service):
    _, settings, store, _, job = service
    with store._connection(write=True) as db:
        db.execute("DELETE FROM app_secret_scopes WHERE app_id=?", (job["app_id"],))
    assert AccessStore(settings).authorize_operation_local("job", job["id"])["id"]


def test_cannot_report_revocation_complete_before_every_accepted_job_is_confirmed(service):
    api, _, store, actor, job = service
    store.checkpoint(job["id"], snapshot={"secret_versions": {"web": {"TOKEN": "a" * 32}}})
    operation = api.revoke(job["app_id"], "TOKEN", "a" * 32, "a" * 32, "失效", actor=actor)
    api.claim_revocation(operation["id"])
    with pytest.raises(DeployError) as error:
        api.finish_revocation(operation["id"])
    assert error.value.code == "SECRET_REVOCATION_STATE"
    assert api.get_revocation(operation["id"])["status"] == "running"
    with pytest.raises(DeployError):
        api.checkpoint_revocation(operation["id"], {"confirmed_job_ids": ["not-in-accepted-scope"]})
    api.checkpoint_revocation(operation["id"], {"confirmed_job_ids": [job["id"]]})
    assert api.finish_revocation(operation["id"])["status"] == "succeeded"
