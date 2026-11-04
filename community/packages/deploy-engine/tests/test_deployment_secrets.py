import copy
import fcntl
import json
import os
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings
from awesome_deploy.manifest import load_manifest
from awesome_deploy.secrets_store import SecretStore
from awesome_deploy.store import Store
from test_source import manifest


AUTH = ("operator", "long-test-password")
REPO = "https://github.com/example/demo.git"
SHA = "a" * 40
VALUE = "PRIVATE-VALUE-must-never-appear-in-records"


@pytest.fixture
def setup(tmp_path):
    state = tmp_path.resolve()
    state.chmod(0o700)
    settings = Settings(state_dir=state, auth_file=state / "admin.json", public_url="http://testserver")
    initialize_credentials(settings.auth_file, *AUTH)
    store = Store(settings)
    with TestClient(create_app(settings, store)) as client:
        yield settings, store, client


def failed(store, *, app_id=None, code="SECRET_NOT_FOUND", name="demo", refs=None):
    payload = {"repository": REPO, "ref": "main", "subdomain": name}
    if app_id:
        payload["appId"] = app_id
    job = store.enqueue(payload, uuid.uuid4().hex)
    data = load_manifest(manifest(), store.settings)
    data["app"]["subdomain"] = name
    data["services"]["web"]["secretRefs"] = refs or {"TOKEN": "MODEL_KEY", "PASSWORD": "APP_PASSWORD"}
    snapshot = {"repository": REPO, "commit": SHA, "source_dir": str(store.settings.state_dir / "sources" / job["id"]),
                "manifest": data}
    store.checkpoint(job["id"], phase="freeze", snapshot=snapshot, execution={"commit": SHA})
    if app_id:
        with store._connection(write=True) as db:
            db.execute("UPDATE jobs SET app_id=? WHERE id=?", (app_id, job["id"]))
    return store.fail(job["id"], {"code": code, "message": "old raw diagnostic"})


def app_record(store, *, app_id="existing-app", subdomain="demo", repository=REPO):
    with store._connection(write=True) as db:
        db.execute("INSERT INTO apps VALUES (?,?,?,?,?,?)", (app_id, repository, subdomain, None, time.time(), time.time()))
    return app_id


def url(job):
    return f"/api/v1/deployments/{job['id']}/secrets"


def post(client, job, values, **kwargs):
    return client.post(url(job), json={"values": values}, auth=AUTH,
                       headers={"Origin": "http://testserver"}, **kwargs)


def test_metadata_lists_deduplicated_references_and_never_resolves_values(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store, refs={"TOKEN": "MODEL_KEY", "OTHER_TOKEN": "MODEL_KEY", "PASSWORD": "APP_PASSWORD"})
    SecretStore(settings).set(REPO, "demo", "MODEL_KEY", VALUE)
    monkeypatch.setattr(SecretStore, "resolve", lambda *args, **kwargs: pytest.fail("GET must not read values"))
    monkeypatch.setattr(SecretStore, "versions", lambda *args, **kwargs: pytest.fail("GET uses safe inspection"))
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "jobId": job["id"], "repository": REPO, "subdomain": "demo", "canGenerate": True,
        "items": [
            {"name": "APP_PASSWORD", "uses": [{"service": "web", "environmentVariable": "PASSWORD"}], "state": "missing", "managed": False},
            {"name": "MODEL_KEY", "uses": [{"service": "web", "environmentVariable": "OTHER_TOKEN"},
                                             {"service": "web", "environmentVariable": "TOKEN"}], "state": "configured", "managed": False},
        ],
    }
    assert VALUE not in response.text and str(settings.state_dir) not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_save_only_creates_missing_values_and_does_not_enqueue_or_change_job_events(setup):
    settings, store, client = setup
    job = failed(store)
    before = store.get(job["id"])
    events = store.events(job["id"])
    response = post(client, job, {"APP_PASSWORD": VALUE, "MODEL_KEY": "existing-provider-value"})
    assert response.status_code == 200, response.text
    assert {item["state"] for item in response.json()["items"]} == {"configured"}
    assert VALUE not in response.text
    assert SecretStore(settings).resolve(REPO, "demo", {"PASSWORD": "APP_PASSWORD"}) == {"PASSWORD": VALUE}
    assert store.get(job["id"]) == before
    assert store.events(job["id"]) == events
    assert len(store.list_jobs()) == 1
    with store._connection() as db:
        assert VALUE not in " ".join(db.iterdump())


def test_authentication_and_origin_protect_secret_metadata_and_writes(setup):
    settings, store, client = setup
    job = failed(store)
    assert client.get(url(job)).status_code == 401
    assert client.post(url(job), json={"values": {"MODEL_KEY": VALUE}}).status_code == 401
    response = client.post(url(job), json={"values": {"MODEL_KEY": VALUE}}, auth=AUTH,
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post(url(job), json={"values": {"MODEL_KEY": VALUE}}, auth=AUTH,
                           headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("body", [
    {}, {"values": {}}, {"values": []}, {"values": {"MODEL_KEY": 123}},
    {"values": {"MODEL_KEY": True}}, {"values": {"MODEL_KEY": None}},
    {"values": {"MODEL_KEY": VALUE}, "repository": "https://github.com/other/repo"},
    {"values": {"MODEL_KEY": VALUE}, "subdomain": "other"},
    {"values": {"UNDECLARED": VALUE}}, {"values": {"MODEL_KEY": ""}},
    {"values": {"APP_PASSWORD": VALUE, "MODEL_KEY": "invalid\nvalue"}},
    {"values": {"APP_PASSWORD": VALUE, "MODEL_KEY": "invalid\rvalue"}},
    {"values": {"APP_PASSWORD": VALUE, "MODEL_KEY": "invalid\x00value"}},
    {"values": {"APP_PASSWORD": VALUE, "MODEL_KEY": "字" * 5462}},
    {"values": {"APP_PASSWORD": VALUE, "MODEL_KEY": "\ud800"}},
])
def test_invalid_requests_are_redacted_and_validated_before_any_write(setup, body):
    settings, store, client = setup
    job = failed(store)
    response = client.post(url(job), content=json.dumps(body), auth=AUTH,
                           headers={"Origin": "http://testserver", "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert VALUE not in response.text and "invalid" not in response.text
    assert not (settings.state_dir / "secrets").exists()


def test_existing_value_is_not_overwritten_and_entire_batch_rejected(setup):
    settings, store, client = setup
    job = failed(store)
    secret_store = SecretStore(settings)
    original = secret_store.set(REPO, "demo", "MODEL_KEY", "original")
    response = post(client, job, {"APP_PASSWORD": VALUE, "MODEL_KEY": "replacement"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SECRET_ALREADY_CONFIGURED"
    assert secret_store.versions(REPO, "demo", {"TOKEN": "MODEL_KEY"}) == {"MODEL_KEY": original}
    assert secret_store.inspect(REPO, "demo", "APP_PASSWORD") == "missing"


def test_damaged_secret_reference_is_unavailable_and_cannot_be_repaired_by_post(setup):
    settings, store, client = setup
    job = failed(store)
    secret_store = SecretStore(settings)
    secret_store.set(REPO, "demo", "MODEL_KEY", "old")
    path = secret_store._directory(REPO, "demo", "MODEL_KEY") / "current.json"
    path.write_text("broken")
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 200
    assert next(item for item in response.json()["items"] if item["name"] == "MODEL_KEY")["state"] == "unavailable"
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_UNAVAILABLE"
    assert path.read_text() == "broken"


@pytest.mark.parametrize("patch", [
    {"status": "queued"}, {"status": "running"}, {"status": "recovery_required"}, {"status": "succeeded"},
    {"error": {"code": "GIT_FAILED"}}, {"error": None}, {"snapshot": None}, {"snapshot": {}},
    {"execution": {}}, {"execution": {"commit": "b" * 40}},
])
def test_only_failed_secret_not_found_with_pinned_snapshot_is_eligible(setup, patch):
    settings, store, client = setup
    job = failed(store)
    with store._connection(write=True) as db:
        for key, value in patch.items():
            db.execute(f"UPDATE jobs SET {key}=? WHERE id=?", (json.dumps(value) if key != "status" else value, job["id"]))
    for response in (client.get(url(job), auth=AUTH), post(client, job, {"MODEL_KEY": VALUE})):
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SECRET_CONFIGURATION_NOT_AVAILABLE"
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("change", ["repository", "subdomain", "commit", "manifest", "refs", "no-refs"])
def test_snapshot_validation_rejects_identity_and_manifest_tampering(setup, change):
    settings, store, client = setup
    job = failed(store)
    snap = copy.deepcopy(job["snapshot"])
    if change == "repository":
        snap["repository"] = "https://github.com/other/repo.git"
    elif change == "subdomain":
        snap["manifest"]["app"]["subdomain"] = "other"
    elif change == "commit":
        snap["commit"] = "main"
    elif change == "manifest":
        snap["manifest"]["services"]["web"]["kind"] = "worker"
    elif change == "refs":
        snap["manifest"]["services"]["web"]["secretRefs"] = {"TOKEN": "../private"}
    else:
        snap["manifest"]["services"]["web"]["secretRefs"] = {}
    store.checkpoint(job["id"], snapshot=snap)
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_CONFIGURATION_NOT_AVAILABLE"
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("historical_status,deleted", [("failed", False), ("succeeded", False), ("failed", True)])
def test_other_application_history_forbids_new_writer_even_without_secret_versions(setup, historical_status, deleted):
    settings, store, client = setup
    app_id = app_record(store, subdomain=None)
    previous = failed(store, app_id=app_id)
    with store._connection(write=True) as db:
        db.execute("UPDATE jobs SET status=? WHERE id=?", (historical_status, previous["id"]))
        if deleted:
            db.execute("DELETE FROM apps WHERE id=?", (app_id,))
    job = failed(store)
    for response in (client.get(url(job), auth=AUTH), post(client, job, {"MODEL_KEY": VALUE})):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SECRET_SCOPE_CONFLICT"
    assert not (settings.state_dir / "secrets").exists()


def test_rejected_app_selection_without_confirmed_owner_does_not_claim_secret_scope(setup):
    settings, store, client = setup
    wrong = app_record(store, subdomain="other", repository="https://github.com/other/repo.git")
    previous = failed(store, code="APP_IDENTITY_CONFLICT")
    with store._connection(write=True) as db:
        db.execute("UPDATE jobs SET input=? WHERE id=?", (json.dumps({**previous["input"], "appId": wrong}), previous["id"]))
    job = failed(store)
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 200 and response.json()["canGenerate"] is True
    assert post(client, job, {"MODEL_KEY": VALUE}).status_code == 200


def test_existing_online_owner_can_add_new_reference_but_cannot_generate(setup):
    settings, store, client = setup
    app_id = app_record(store)
    previous = failed(store, app_id=app_id)
    with store._connection(write=True) as db:
        db.execute("UPDATE jobs SET status='succeeded',error=NULL WHERE id=?", (previous["id"],))
        db.execute("UPDATE apps SET active_release_id=? WHERE id=?", (previous["id"], app_id))
    job = failed(store, app_id=app_id)
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 200 and response.json()["canGenerate"] is False
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 200
    assert store.active_release(app_id)["id"] == previous["id"]


@pytest.mark.parametrize("mismatch", ["input-app", "missing-app", "repository", "bound-domain"])
def test_selected_and_persisted_application_identity_must_match(setup, mismatch):
    settings, store, client = setup
    app_id = app_record(store)
    job = failed(store, app_id=app_id)
    with store._connection(write=True) as db:
        if mismatch == "input-app":
            payload = {**job["input"], "appId": "another-app"}
            db.execute("UPDATE jobs SET input=? WHERE id=?", (json.dumps(payload), job["id"]))
        elif mismatch == "missing-app":
            db.execute("DELETE FROM apps WHERE id=?", (app_id,))
        elif mismatch == "repository":
            db.execute("UPDATE apps SET repository='https://github.com/other/repo.git' WHERE id=?", (app_id,))
        else:
            db.execute("UPDATE apps SET subdomain='other' WHERE id=?", (app_id,))
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_SCOPE_CONFLICT"


@pytest.mark.parametrize("state", ["queued", "running", "recovery_required"])
def test_related_active_deployment_blocks_saving(setup, state):
    settings, store, client = setup
    job = failed(store)
    competing = store.enqueue(job["input"], "competing")
    with store._connection(write=True) as db:
        db.execute("UPDATE jobs SET status=? WHERE id=?", (state, competing["id"]))
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_CONFIGURATION_BUSY"
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("state", ["queued", "running", "recovery_required"])
def test_unfinished_offline_operation_blocks_saving(setup, state):
    settings, store, client = setup
    app_id = app_record(store)
    job = failed(store, app_id=app_id)
    with store._connection(write=True) as db:
        db.execute("""INSERT INTO offline_operations
            (id,idempotency_key,app_id,target_release_id,status,phase,created_at,updated_at)
            VALUES ('op','key',?,?,?,'offline',?,?)""", (app_id, job["id"], state, time.time(), time.time()))
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_CONFIGURATION_BUSY"


def test_orphan_resource_reservation_prevents_fresh_scope_generation(setup):
    settings, store, client = setup
    job = failed(store)
    with store._connection(write=True) as db:
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (job["id"], "web", 20000, .5, 512))
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 409


@pytest.mark.parametrize("execution", [
    {"ports": {"web": 20000}}, {"storage_volumes": {"web": "old-volume"}},
    {"images": {"web": "old-image"}}, {"publish_intent": True}, {"storage_handoff_intent": True},
])
def test_fresh_scope_with_orphan_runtime_identity_cannot_generate_or_save(setup, execution):
    settings, store, client = setup
    job = failed(store)
    store.checkpoint(job["id"], execution=execution)
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 409
    assert post(client, job, {"MODEL_KEY": VALUE}).status_code == 409


@pytest.mark.parametrize("active", [True, False])
def test_only_current_release_recovery_blocks_same_owner_configuration(setup, active):
    settings, store, client = setup
    app_id = app_record(store)
    previous = failed(store, app_id=app_id)
    with store._connection(write=True) as db:
        db.execute("UPDATE jobs SET status='succeeded',error=NULL WHERE id=?", (previous["id"],))
        if active:
            db.execute("UPDATE apps SET active_release_id=? WHERE id=?", (previous["id"], app_id))
    store.checkpoint(previous["id"], execution={"active_recovery": {"outcome": "failed"}})
    job = failed(store, app_id=app_id)
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == (409 if active else 200)


def test_worker_lock_is_nonblocking_and_acquired_before_database_transaction(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store)
    with (settings.state_dir / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(store, "_connection", lambda *args, **kwargs: pytest.fail("DB before worker lock"))
        response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_CONFIGURATION_BUSY"


def test_post_holds_worker_lock_and_sqlite_immediate_transaction_through_secret_creation(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store)
    original = SecretStore.create_missing

    def guarded(secret_store, *args):
        with (settings.state_dir / "worker.lock").open("a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(settings.database_path, timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        return original(secret_store, *args)

    monkeypatch.setattr(SecretStore, "create_missing", guarded)
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 200


def test_partial_filesystem_failure_preserves_created_values_and_allows_remaining_values(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store)
    original = SecretStore.create_missing

    def fail_second(secret_store, repository, subdomain, name, value):
        if name == "MODEL_KEY":
            raise OSError("private diagnostic " + VALUE)
        return original(secret_store, repository, subdomain, name, value)

    monkeypatch.setattr(SecretStore, "create_missing", fail_second)
    response = post(client, job, {"APP_PASSWORD": VALUE, "MODEL_KEY": "provider-value"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SECRET_UNAVAILABLE"
    assert VALUE not in response.text
    monkeypatch.setattr(SecretStore, "create_missing", original)
    response = client.get(url(job), auth=AUTH)
    states = {item["name"]: item["state"] for item in response.json()["items"]}
    assert states == {"APP_PASSWORD": "configured", "MODEL_KEY": "missing"}
    assert post(client, job, {"MODEL_KEY": "provider-value"}).status_code == 200


@pytest.mark.parametrize("target,method", [("inspect", "get"), ("create_missing", "post"),
                                           ("validate_value", "post"), ("database", "get")])
def test_unexpected_dependency_errors_are_redacted_without_server_tracebacks(setup, monkeypatch, caplog, target, method):
    settings, store, client = setup
    job = failed(store)

    def unexpected(*args, **kwargs):
        if target == "database":
            raise sqlite3.OperationalError(VALUE)
        raise RuntimeError(VALUE)

    if target == "database":
        monkeypatch.setattr(store, "secret_configuration_scope", unexpected)
    else:
        monkeypatch.setattr(SecretStore, target, unexpected)
    response = client.get(url(job), auth=AUTH) if method == "get" else post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_UNAVAILABLE"
    assert VALUE not in response.text and VALUE not in caplog.text


@pytest.mark.parametrize("kind", ["symlink", "directory", "hardlink"])
def test_unsafe_worker_lock_never_writes_secrets(setup, kind):
    settings, store, client = setup
    job = failed(store)
    lock = settings.state_dir / "worker.lock"
    if kind == "symlink":
        lock.symlink_to(settings.auth_file)
    elif kind == "directory":
        lock.mkdir()
    else:
        os.link(settings.auth_file, lock)
    response = post(client, job, {"MODEL_KEY": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_UNAVAILABLE"
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("code", ["SECRET_CONFIGURATION_NOT_AVAILABLE", "SECRET_CONFIGURATION_BUSY",
                                  "SECRET_ALREADY_CONFIGURED", "SECRET_UNAVAILABLE"])
def test_new_error_diagnostics_are_chinese_and_never_echo_input(code):
    from awesome_deploy.diagnostics import ERROR_CODES, localize_error
    assert code in ERROR_CODES
    response = localize_error({"code": code, "message": VALUE, "suggestion": VALUE, "path": VALUE})
    assert VALUE not in str(response)
    assert any("\u4e00" <= character <= "\u9fff" for character in response["message"])
