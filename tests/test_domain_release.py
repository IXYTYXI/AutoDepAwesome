import concurrent.futures
import copy
import json
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_available", staticmethod(lambda port: True))
    return Store(Settings(state_dir=tmp_path, port_start=42500, port_end=42900))


def payload(domain="demo", app_id=None, repository="https://github.com/acme/demo.git"):
    data = {"repository": repository, "subdomain": domain, "ref": "main"}
    if app_id:
        data["appId"] = app_id
    return data


def snapshot(data=None, version=None):
    data = data or payload()
    return {"repository": data["repository"], "commit": "a" * 40, "manifest": {
        "app": {"subdomain": data["subdomain"]}, "services": {"web": {
            "resources": {"cpu": 0.5, "memoryMiB": 128},
            "storage": {"name": "images", "mountPath": "/data"},
            "secretRefs": {"KEY": "upload-token"} if version else {},
        }}}, "secret_versions": {"web": {"upload-token": version}} if version else {}}


def deploy(store, key="first", domain="demo", app_id=None, version=None, repository="https://github.com/acme/demo.git"):
    data = payload(domain, app_id, repository)
    job = store.enqueue(data, key)
    store.claim(job["id"])
    store.prepare_context(job["id"])
    store.reserve(job["id"], snapshot(data, version))
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": {
        "name": "kept-volume", "created_at": "original", "instance": "a" * 32,
    }}})
    return store.finish(job["id"])


def offline(store, job, key="offline"):
    operation = store.enqueue_offline(job["app_id"], job["id"], key)
    store.claim_offline(operation["id"])
    store.checkpoint_offline(operation["id"], {
        "gateway_deactivated": True, "containers_stopped": True, "cleanup_confirmed": True,
    })
    return store.finish_offline(operation["id"])


def app(store, app_id):
    return next(row for row in store.apps() if row["id"] == app_id)


def legacy_schema(store):
    """Recreate the exact pre-release domain constraint without modifying child tables."""
    with sqlite3.connect(store.settings.database_path, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        for app_id, subdomain in db.execute("SELECT id,subdomain FROM apps").fetchall():
            if subdomain is None:
                raw = db.execute("SELECT input FROM jobs WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (app_id,)).fetchone()[0]
                db.execute("UPDATE apps SET subdomain=? WHERE id=?", (json.loads(raw)["subdomain"], app_id))
        db.execute("""CREATE TABLE legacy_apps (
            id TEXT PRIMARY KEY, repository TEXT NOT NULL, subdomain TEXT NOT NULL UNIQUE,
            active_release_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("INSERT INTO legacy_apps SELECT * FROM apps")
        db.execute("DROP TABLE apps")
        db.execute("ALTER TABLE legacy_apps RENAME TO apps")
        db.commit()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_completed_offline_frees_domain_and_ports_but_keeps_original_identity(store):
    original = deploy(store)
    operation = offline(store, original)
    old = app(store, original["app_id"])
    assert old["subdomain"] is None and old["domainBound"] is False
    assert old["lastSubdomain"] == "demo"
    replacement = deploy(store, "replacement")
    assert replacement["app_id"] != original["app_id"]
    assert replacement["execution"]["ports"] == original["execution"]["ports"]
    assert store.get(original["id"])["execution"]["storage_volumes"] == original["execution"]["storage_volumes"]
    assert store.finish_offline(operation["id"])["status"] == "succeeded"
    assert app(store, replacement["app_id"])["subdomain"] == "demo"


def test_offline_app_can_claim_another_free_prefix_without_changing_storage_or_original_record(store):
    original = deploy(store)
    offline(store, original)
    data = payload("fresh", original["app_id"])
    candidate = store.enqueue(data, "again")
    prepared = store.prepare_context(candidate["id"])
    assert app(store, original["app_id"])["subdomain"] is None
    assert prepared["execution"]["previous_release"] is None
    assert prepared["execution"]["storage_volumes"] == original["execution"]["storage_volumes"]
    reserved = store.reserve(candidate["id"], snapshot(data))
    assert reserved["app_id"] == original["app_id"]
    assert app(store, original["app_id"])["subdomain"] == "fresh"
    assert store.get(original["id"])["input"]["subdomain"] == "demo"


def test_old_offline_app_cannot_take_prefix_claimed_by_another_app(store):
    original = deploy(store)
    offline(store, original)
    replacement = deploy(store, "replacement")
    candidate = store.enqueue(payload(app_id=original["app_id"]), "again")
    with pytest.raises(DeployError) as error:
        store.prepare_context(candidate["id"])
    assert error.value.code == "DOMAIN_CONFLICT"
    assert app(store, replacement["app_id"])["subdomain"] == "demo"


def test_prepare_does_not_reserve_and_transactional_reserve_has_one_winner(store):
    old = deploy(store)
    offline(store, old)
    candidates = [store.enqueue(payload(app_id=old["app_id"]), "existing"), store.enqueue(payload(), "new")]
    for candidate in candidates:
        store.prepare_context(candidate["id"])
    assert app(store, old["app_id"])["subdomain"] is None

    def reserve(candidate):
        try:
            return store.reserve(candidate["id"], snapshot())["app_id"]
        except DeployError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor() as executor:
        outcomes = list(executor.map(reserve, candidates))
    assert outcomes.count("DOMAIN_CONFLICT") == 1
    assert len([row for row in store.apps() if row["subdomain"] == "demo"]) == 1


@pytest.mark.parametrize("repository,domain", [("https://github.com/acme/other.git", "demo"),
                                                 ("https://github.com/acme/demo.git", "other")])
def test_online_app_cannot_change_repository_or_domain(store, repository, domain):
    original = deploy(store)
    candidate = store.enqueue(payload(domain, original["app_id"], repository), "changed")
    with pytest.raises(DeployError) as error:
        store.prepare_context(candidate["id"])
    assert error.value.code == "APP_IDENTITY_CONFLICT"


def test_failure_only_frees_binding_after_confirmed_cleanup_and_preserves_active_binding(store):
    first = store.enqueue(payload(), "first")
    reserved = store.reserve(first["id"], snapshot())
    store.fail(first["id"], {"code": "CLEANUP_FAILED"}, recovery=True)
    assert app(store, reserved["app_id"])["domainBound"] is True
    store.release(first["id"])
    assert app(store, reserved["app_id"])["subdomain"] is None
    current = deploy(store, "current")
    next_job = store.enqueue(payload(app_id=current["app_id"]), "candidate")
    store.reserve(next_job["id"], snapshot())
    store.release(next_job["id"])
    assert app(store, current["app_id"])["subdomain"] == "demo"


def test_stale_cleanup_cannot_clear_later_candidate_domain(store):
    first = store.enqueue(payload(), "first")
    old = store.reserve(first["id"], snapshot())
    store.release(first["id"])
    data = payload(app_id=old["app_id"])
    candidate = store.enqueue(data, "next")
    store.reserve(candidate["id"], snapshot(data))
    store.release(first["id"])
    assert app(store, old["app_id"])["subdomain"] == "demo"


def test_management_exposes_real_binding_and_history_preference(store):
    old = deploy(store)
    offline(store, old)
    row = store.management_snapshot()["apps"][0]
    assert row["subdomain"] is None and row["lastSubdomain"] == "demo" and row["domainBound"] is False


def test_migration_preserves_all_other_rows_schema_and_foreign_keys(store):
    live = deploy(store)
    old = deploy(store, "other", "other")
    offline(store, old)
    store.event(old["id"], "complete", "kept history")
    legacy_schema(store)
    with sqlite3.connect(store.settings.database_path) as db:
        db.execute("CREATE TABLE external_reference (app_id TEXT REFERENCES apps(id), note TEXT)")
        db.execute("INSERT INTO external_reference VALUES (?, 'kept')", (old["app_id"],))
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='apps'")]
        before = {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name!='apps' ORDER BY type,name").fetchall()
    migrated = Store(store.settings, verified_offline_domains={old["app_id"]: old["id"]})
    assert app(migrated, live["app_id"])["subdomain"] == "demo"
    assert app(migrated, old["app_id"])["subdomain"] is None
    with sqlite3.connect(store.settings.database_path) as db:
        assert next(row for row in db.execute("PRAGMA table_info(apps)") if row[1] == "subdomain")[3] == 0
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables} == before
        assert db.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name!='apps' ORDER BY type,name").fetchall() == schema
        assert db.execute("PRAGMA foreign_key_list(offline_operations)").fetchall()[1][2] == "apps"
    assert Store(store.settings).apps() == migrated.apps()


@pytest.mark.parametrize("blocker", ["queued", "running", "recovery_required", "reservation", "offline"])
def test_legacy_completed_offline_is_not_unbound_with_unresolved_work(store, blocker):
    old = deploy(store)
    operation = offline(store, old)
    legacy_schema(store)
    with sqlite3.connect(store.settings.database_path) as db:
        if blocker == "reservation":
            db.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (old["id"], "web", 42700, 0.5, 128))
        elif blocker == "offline":
            db.execute("UPDATE offline_operations SET status='recovery_required' WHERE id=?", (operation["id"],))
        else:
            waiting = store.enqueue(payload(app_id=old["app_id"]), "waiting")
            db.execute("UPDATE jobs SET status=? WHERE id=?", (blocker, waiting["id"]))
    migrated = Store(store.settings)
    assert app(migrated, old["app_id"])["subdomain"] == "demo"
    with pytest.raises(DeployError) as error:
        Store(store.settings, verified_offline_domains={old["app_id"]: old["id"]})
    assert error.value.code == "STATE_MIGRATION_FAILED"
    assert app(migrated, old["app_id"])["subdomain"] == "demo"


def test_default_upgrade_keeps_legacy_binding_until_explicitly_verified(store):
    old = deploy(store)
    offline(store, old)
    legacy_schema(store)
    migrated = Store(store.settings)
    assert app(migrated, old["app_id"])["subdomain"] == "demo"
    verified = {old["app_id"]: old["id"]}
    released = Store(store.settings, verified_offline_domains=verified)
    assert app(released, old["app_id"])["subdomain"] is None
    # The explicit journal can replay after schema migration and domain release.
    assert Store(store.settings, verified_offline_domains=verified).apps() == released.apps()


@pytest.mark.parametrize("mismatch", ["app", "release", "active", "domain", "confirmations"])
def test_explicit_legacy_verification_mismatch_fails_closed_and_rolls_back_schema(store, mismatch):
    old = deploy(store)
    operation = offline(store, old)
    legacy_schema(store)
    verified = {old["app_id"]: old["id"]}
    with sqlite3.connect(store.settings.database_path) as db:
        if mismatch == "app":
            verified = {"f" * 32: old["id"]}
        elif mismatch == "release":
            verified = {old["app_id"]: "f" * 32}
        elif mismatch == "active":
            db.execute("UPDATE apps SET active_release_id=? WHERE id=?", (old["id"], old["app_id"]))
        elif mismatch == "domain":
            db.execute("UPDATE apps SET subdomain='new-binding' WHERE id=?", (old["app_id"],))
        elif mismatch == "confirmations":
            db.execute("UPDATE offline_operations SET execution='{}' WHERE id=?", (operation["id"],))
        original_rows = db.execute("SELECT * FROM apps ORDER BY id").fetchall()
    with pytest.raises(DeployError) as error:
        Store(store.settings, verified_offline_domains=verified)
    assert error.value.code == "STATE_MIGRATION_FAILED"
    with sqlite3.connect(store.settings.database_path) as db:
        assert next(row for row in db.execute("PRAGMA table_info(apps)") if row[1] == "subdomain")[3] == 1
        assert db.execute("SELECT * FROM apps ORDER BY id").fetchall() == original_rows


def test_explicit_verified_domain_batch_is_atomic_on_nullable_schema(store):
    first = deploy(store)
    offline(store, first)
    second = deploy(store, "second", "second")
    offline(store, second, "second-offline")
    legacy_schema(store)
    migrated = Store(store.settings)
    with pytest.raises(DeployError) as error:
        Store(store.settings, verified_offline_domains={first["app_id"]: first["id"], second["app_id"]: "f" * 32})
    assert error.value.code == "STATE_MIGRATION_FAILED"
    assert app(migrated, first["app_id"])["subdomain"] == "demo"
    assert app(migrated, second["app_id"])["subdomain"] == "second"


def test_verified_legacy_release_replay_does_not_clear_later_claim(store):
    old = deploy(store)
    offline(store, old)
    legacy_schema(store)
    verified = {old["app_id"]: old["id"]}
    migrated = Store(store.settings, verified_offline_domains=verified)
    data = payload("fresh", old["app_id"])
    next_job = migrated.enqueue(data, "next")
    migrated.reserve(next_job["id"], snapshot(data))
    with pytest.raises(DeployError) as error:
        Store(store.settings, verified_offline_domains=verified)
    assert error.value.code == "STATE_MIGRATION_FAILED"
    assert app(migrated, old["app_id"])["subdomain"] == "fresh"


def test_migration_does_not_release_inactive_first_deployment_or_repeat_unbinding(store):
    first = store.enqueue(payload(), "first")
    reserved = store.reserve(first["id"], snapshot())
    legacy_schema(store)
    migrated = Store(store.settings)
    assert app(migrated, reserved["app_id"])["subdomain"] == "demo"
    # A normal startup must not apply the historical migration cleanup again.
    migrated.release(first["id"])
    data = payload(app_id=reserved["app_id"])
    candidate = migrated.enqueue(data, "next")
    migrated.reserve(candidate["id"], snapshot(data))
    assert app(Store(store.settings), reserved["app_id"])["subdomain"] == "demo"


def test_migration_refuses_broken_foreign_keys_and_keeps_legacy_table(store):
    deploy(store)
    legacy_schema(store)
    with sqlite3.connect(store.settings.database_path) as db:
        db.execute("CREATE TABLE external_reference (app_id TEXT REFERENCES apps(id))")
        db.execute("INSERT INTO external_reference VALUES ('missing')")
    with pytest.raises(DeployError) as error:
        Store(store.settings)
    assert error.value.code == "STATE_MIGRATION_FAILED"
    with sqlite3.connect(store.settings.database_path) as db:
        assert next(row for row in db.execute("PRAGMA table_info(apps)") if row[1] == "subdomain")[3] == 1


def test_new_app_cannot_inherit_original_or_unconsumed_rotated_secret_versions(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    attempt = store.enqueue(payload(), "new-app")
    with pytest.raises(DeployError) as error:
        store.reserve(attempt["id"], snapshot(version="a" * 32))
    assert error.value.code == "SECRET_SCOPE_CONFLICT"
    assert len(store.apps()) == 1 and app(store, old["app_id"])["subdomain"] is None
    # A rotated version that no deployment has consumed still belongs to the
    # old repository/domain scope and must not be inherited by a new app.
    with pytest.raises(DeployError) as rotated:
        store.reserve(attempt["id"], snapshot(version="b" * 32))
    assert rotated.value.code == "SECRET_SCOPE_CONFLICT"
    assert len(store.apps()) == 1 and app(store, old["app_id"])["subdomain"] is None


def test_new_app_cannot_reference_a_secret_not_used_by_the_old_manifest(store):
    old = deploy(store)
    assert not old["snapshot"]["secret_versions"]
    offline(store, old)
    attempt = store.enqueue(payload(), "new-app")
    with pytest.raises(DeployError) as error:
        store.reserve(attempt["id"], snapshot(version="a" * 32))
    assert error.value.code == "SECRET_SCOPE_CONFLICT"
    assert len(store.apps()) == 1


def test_new_app_without_secret_references_can_reuse_the_domain(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    attempt = store.enqueue(payload(), "new-app")
    frozen = snapshot()
    frozen["secret_versions"] = {"web": {}}
    assert store.reserve(attempt["id"], frozen)["app_id"] != old["app_id"]


def test_new_app_can_use_a_separately_configured_free_domain(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    newer = deploy(store, "new-app", "fresh", version="b" * 32)
    assert newer["app_id"] != old["app_id"]


def test_original_app_can_reuse_its_own_secret_version_and_data_identity(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    data = payload(app_id=old["app_id"])
    attempt = store.enqueue(data, "same-app")
    store.prepare_context(attempt["id"])
    reserved = store.reserve(attempt["id"], snapshot(data, "a" * 32))
    assert reserved["execution"]["storage_volumes"] == old["execution"]["storage_volumes"]


def test_secret_guard_also_checks_frozen_resumed_reservations(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    attempt = store.enqueue(payload(), "new-app")
    reserved = store.reserve(attempt["id"], snapshot())
    stale_snapshot = copy.deepcopy(reserved["snapshot"])
    stale_snapshot["secret_versions"] = {"web": {"upload-token": "b" * 32}}
    store.checkpoint(attempt["id"], snapshot=stale_snapshot)
    with pytest.raises(DeployError) as error:
        store.reserve(attempt["id"], stale_snapshot)
    assert error.value.code == "SECRET_SCOPE_CONFLICT"


def test_old_offline_completion_replay_cannot_clear_same_app_new_binding(store):
    old = deploy(store)
    operation = offline(store, old)
    newer = deploy(store, "newer", "fresh", old["app_id"])
    assert store.finish_offline(operation["id"])["status"] == "succeeded"
    assert app(store, old["app_id"])["subdomain"] == "fresh"
    assert app(store, old["app_id"])["active_release_id"] == newer["id"]


def test_secret_version_guard_does_not_cross_repository_scope(store):
    old = deploy(store, version="a" * 32)
    offline(store, old)
    newer = deploy(store, "different-repo", version="a" * 32, repository="https://github.com/acme/another.git")
    assert newer["app_id"] != old["app_id"]


def test_migration_refuses_unreviewed_constraints_without_altering_them(store):
    deploy(store)
    legacy_schema(store)
    with sqlite3.connect(store.settings.database_path, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE custom_apps (
            id TEXT PRIMARY KEY, repository TEXT NOT NULL, subdomain TEXT NOT NULL UNIQUE,
            active_release_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            CHECK(length(subdomain) > 0))""")
        db.execute("INSERT INTO custom_apps SELECT * FROM apps")
        db.execute("DROP TABLE apps")
        db.execute("ALTER TABLE custom_apps RENAME TO apps")
        db.commit()
        original = db.execute("SELECT sql FROM sqlite_master WHERE name='apps'").fetchone()[0]
    with pytest.raises(DeployError) as error:
        Store(store.settings)
    assert error.value.code == "STATE_MIGRATION_FAILED"
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='apps'").fetchone()[0] == original


def test_finish_requires_candidate_still_owns_its_domain(store):
    candidate = store.enqueue(payload(), "candidate")
    reserved = store.reserve(candidate["id"], snapshot())
    with sqlite3.connect(store.settings.database_path) as db:
        db.execute("UPDATE apps SET subdomain=NULL WHERE id=?", (reserved["app_id"],))
    with pytest.raises(DeployError) as error:
        store.finish(candidate["id"])
    assert error.value.code == "DOMAIN_CONFLICT"
    assert store.active_release(reserved["app_id"]) is None
