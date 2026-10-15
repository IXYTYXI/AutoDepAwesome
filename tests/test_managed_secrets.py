"""Platform-generated MASTER_KEY is never exposed or replaced by configuration retries."""

import copy
import fcntl
import re
import secrets
import sqlite3

import pytest

from awesome_deploy.secrets_store import SecretStore
from test_deployment_secrets import AUTH, REPO, VALUE, app_record, failed, post, url
from test_deployment_secrets import setup as setup


def managed_job(store, *, app_id=None, password=True):
    refs = {"MASTER_KEY": "MASTER_KEY"}
    if password:
        refs["BOOTSTRAP_PASSWORD"] = "BOOTSTRAP_PASSWORD"
    return failed(store, app_id=app_id, refs=refs)


def prohibit_generation(monkeypatch):
    def unexpected(*args):
        pytest.fail("This request must not generate a new secret")
    monkeypatch.setattr(secrets, "token_hex", unexpected)


@pytest.mark.parametrize("refs,expected", [
    ({"MASTER_KEY": "MASTER_KEY"}, {"MASTER_KEY": True}),
    ({"MASTER_KEY": "other-key"}, {"other-key": False}),
    ({"MASTER_KEY": "master_key"}, {"master_key": False}),
    ({"master_key": "MASTER_KEY"}, {"MASTER_KEY": False}),
    ({"OPENAI_API_KEY": "MASTER_KEY"}, {"MASTER_KEY": False}),
    ({"MASTER_KEY": "MASTER_KEY", "PROVIDER_KEY": "MASTER_KEY"}, {"MASTER_KEY": False}),
    ({"BOOTSTRAP_PASSWORD": "BOOTSTRAP_PASSWORD", "API_KEY": "provider"},
     {"BOOTSTRAP_PASSWORD": False, "provider": False}),
])
def test_managed_flag_requires_exact_reference_and_all_known_environment_uses(setup, monkeypatch, refs, expected):
    settings, store, client = setup
    job = failed(store, refs=refs)
    prohibit_generation(monkeypatch)
    response = client.get(url(job), auth=AUTH)
    assert response.status_code == 200
    assert {item["name"]: item["managed"] for item in response.json()["items"]} == expected
    assert not (settings.state_dir / "secrets").exists()


def test_managed_reference_may_be_shared_only_between_known_master_key_uses(setup):
    settings, store, client = setup
    job = managed_job(store, password=False)
    snapshot = copy.deepcopy(job["snapshot"])
    snapshot["manifest"]["services"]["api"] = copy.deepcopy(snapshot["manifest"]["services"]["web"])
    store.checkpoint(job["id"], snapshot=snapshot)
    item = client.get(url(job), auth=AUTH).json()["items"][0]
    assert item["managed"] is True and len(item["uses"]) == 2
    snapshot["manifest"]["services"]["api"]["secretRefs"] = {"API_KEY": "MASTER_KEY"}
    store.checkpoint(job["id"], snapshot=snapshot)
    assert client.get(url(job), auth=AUTH).json()["items"][0]["managed"] is False


def test_server_generates_32_random_bytes_after_user_values_under_both_locks_without_leaks(setup, monkeypatch, caplog):
    settings, store, client = setup
    job = managed_job(store)
    before = store.get(job["id"])
    original = secrets.token_hex
    generated = []

    def generate(size):
        assert size == 32
        assert SecretStore(settings).inspect(REPO, "demo", "BOOTSTRAP_PASSWORD") == "configured"
        with (settings.state_dir / "worker.lock").open("a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(settings.database_path, timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        value = original(size)
        generated.append(value)
        return value

    monkeypatch.setattr(secrets, "token_hex", generate)
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 200, response.text
    assert len(generated) == 1 and re.fullmatch(r"[0-9a-f]{64}", generated[0])
    assert {item["state"] for item in response.json()["items"]} == {"configured"}
    assert SecretStore(settings).resolve(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"}) == {"MASTER_KEY": generated[0]}
    for value in (VALUE, generated[0]):
        assert value not in response.text and value not in caplog.text
        with store._connection() as db:
            assert value not in " ".join(db.iterdump())
    assert store.get(job["id"]) == before and store.events(job["id"]) == []
    assert len(store.list_jobs()) == 1


def test_empty_request_creates_only_missing_managed_key_and_is_idempotent_after_lost_response(setup, monkeypatch):
    settings, store, client = setup
    job = managed_job(store, password=False)
    first = post(client, job, {})
    assert first.status_code == 200
    secret_store = SecretStore(settings)
    version = secret_store.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"})
    prohibit_generation(monkeypatch)
    second = post(client, job, {})
    assert second.status_code == 200 and second.json() == first.json()
    assert secret_store.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"}) == version


def test_empty_request_can_retry_when_all_unmanaged_values_are_already_configured(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store)
    post(client, job, {"MODEL_KEY": VALUE, "APP_PASSWORD": VALUE})
    prohibit_generation(monkeypatch)
    response = post(client, job, {})
    assert response.status_code == 200
    assert {item["state"] for item in response.json()["items"]} == {"configured"}


@pytest.mark.parametrize("existing", [False, True])
def test_managed_client_value_is_rejected_before_any_other_write(setup, monkeypatch, existing):
    settings, store, client = setup
    job = managed_job(store)
    if existing:
        SecretStore(settings).set(REPO, "demo", "MASTER_KEY", "a" * 64)
    prohibit_generation(monkeypatch)
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE, "MASTER_KEY": "client-controlled"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "SECRET_MANAGED_INPUT"
    assert "client-controlled" not in response.text and VALUE not in response.text
    assert SecretStore(settings).inspect(REPO, "demo", "BOOTSTRAP_PASSWORD") == "missing"


@pytest.mark.parametrize("values", [{}, {"BOOTSTRAP_PASSWORD": ""}, {"BOOTSTRAP_PASSWORD": "bad\nvalue"},
                                    {"unknown": VALUE}])
def test_incomplete_or_invalid_user_fields_never_trigger_generation(setup, monkeypatch, values):
    settings, store, client = setup
    job = managed_job(store)
    prohibit_generation(monkeypatch)
    response = post(client, job, values)
    assert response.status_code == 422
    assert not (settings.state_dir / "secrets").exists()


@pytest.mark.parametrize("state", ["missing", "unavailable"])
def test_managed_key_in_existing_scope_requires_original_value_recovery(setup, monkeypatch, state):
    settings, store, client = setup
    owner = app_record(store)
    job = managed_job(store, app_id=owner)
    if state == "unavailable":
        secret_store = SecretStore(settings)
        secret_store.set(REPO, "demo", "MASTER_KEY", "original")
        (secret_store._directory(REPO, "demo", "MASTER_KEY") / "current.json").unlink()
    prohibit_generation(monkeypatch)
    metadata = client.get(url(job), auth=AUTH).json()
    assert metadata["canGenerate"] is False
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_UNAVAILABLE"
    assert SecretStore(settings).inspect(REPO, "demo", "BOOTSTRAP_PASSWORD") == "missing"


@pytest.mark.parametrize("owned", [False, True])
def test_already_configured_managed_key_is_reused_without_generation_or_reading_value(setup, monkeypatch, owned):
    settings, store, client = setup
    job = managed_job(store, app_id=app_record(store) if owned else None)
    secret_store = SecretStore(settings)
    version = secret_store.set(REPO, "demo", "MASTER_KEY", "b" * 64)
    prohibit_generation(monkeypatch)
    monkeypatch.setattr(SecretStore, "resolve", lambda *args, **kwargs: pytest.fail("Existing key must not be read"))
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 200
    assert secret_store.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"}) == {"MASTER_KEY": version}
    assert "b" * 64 not in response.text


def test_unavailable_managed_key_in_fresh_scope_is_never_replaced(setup, monkeypatch):
    settings, store, client = setup
    job = managed_job(store)
    secret_store = SecretStore(settings)
    secret_store.set(REPO, "demo", "MASTER_KEY", "original")
    (secret_store._directory(REPO, "demo", "MASTER_KEY") / "current.json").write_text("broken")
    prohibit_generation(monkeypatch)
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 409
    assert secret_store.inspect(REPO, "demo", "BOOTSTRAP_PASSWORD") == "missing"


def test_user_write_failure_occurs_before_random_key_generation(setup, monkeypatch):
    settings, store, client = setup
    job = managed_job(store)
    prohibit_generation(monkeypatch)
    monkeypatch.setattr(SecretStore, "create_missing", lambda *args: (_ for _ in ()).throw(OSError(VALUE)))
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 409 and VALUE not in response.text
    assert SecretStore(settings).inspect(REPO, "demo", "MASTER_KEY") == "missing"


def test_rng_failure_keeps_user_configuration_and_retry_generates_only_managed_key(setup, monkeypatch, caplog):
    settings, store, client = setup
    job = managed_job(store)
    original = secrets.token_hex
    monkeypatch.setattr(secrets, "token_hex", lambda *args: (_ for _ in ()).throw(RuntimeError(VALUE)))
    response = post(client, job, {"BOOTSTRAP_PASSWORD": VALUE})
    assert response.status_code == 409 and VALUE not in response.text and VALUE not in caplog.text
    secret_store = SecretStore(settings)
    password_version = secret_store.versions(REPO, "demo", {"PASSWORD": "BOOTSTRAP_PASSWORD"})
    assert secret_store.inspect(REPO, "demo", "MASTER_KEY") == "missing"
    monkeypatch.setattr(secrets, "token_hex", original)
    assert post(client, job, {}).status_code == 200
    assert secret_store.versions(REPO, "demo", {"PASSWORD": "BOOTSTRAP_PASSWORD"}) == password_version


def test_failure_after_managed_pointer_publication_never_rotates_key_on_retry(setup, monkeypatch):
    settings, store, client = setup
    job = managed_job(store)
    original = SecretStore.create_missing

    def publish_then_fail(secret_store, repository, subdomain, name, value):
        version = original(secret_store, repository, subdomain, name, value)
        if name == "MASTER_KEY":
            raise OSError("response failed")
        return version

    monkeypatch.setattr(SecretStore, "create_missing", publish_then_fail)
    assert post(client, job, {"BOOTSTRAP_PASSWORD": VALUE}).status_code == 409
    secret_store = SecretStore(settings)
    original_version = secret_store.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"})
    monkeypatch.setattr(SecretStore, "create_missing", original)
    prohibit_generation(monkeypatch)
    assert post(client, job, {}).status_code == 200
    assert secret_store.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"}) == original_version


def test_external_api_reference_named_master_key_remains_manual_and_never_generated(setup, monkeypatch):
    settings, store, client = setup
    job = failed(store, refs={"OPENAI_API_KEY": "MASTER_KEY"})
    prohibit_generation(monkeypatch)
    assert post(client, job, {}).status_code == 422
    response = post(client, job, {"MASTER_KEY": VALUE})
    assert response.status_code == 200
    assert response.json()["items"][0]["managed"] is False


def test_other_application_history_still_blocks_managed_generation(setup, monkeypatch):
    settings, store, client = setup
    owner = app_record(store, subdomain=None)
    managed_job(store, app_id=owner)
    job = managed_job(store, password=False)
    prohibit_generation(monkeypatch)
    response = post(client, job, {})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SECRET_SCOPE_CONFLICT"


def test_managed_input_error_is_localized_without_client_value():
    from awesome_deploy.diagnostics import ERROR_CODES, localize_error
    assert "SECRET_MANAGED_INPUT" in ERROR_CODES
    error = localize_error({"code": "SECRET_MANAGED_INPUT", "message": VALUE})
    assert VALUE not in str(error) and "平台" in error["message"]
