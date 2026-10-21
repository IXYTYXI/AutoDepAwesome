import time

import pytest

from awesome_deploy.vault_service import VaultService
from test_feishu_api import login, switch_user, write_headers
from test_feishu_api import web as web
from test_vault_service import VaultAdapter


@pytest.fixture
def encrypted_web(web):
    client, app, provider = web
    alice = login(client)
    result = client.post("/api/v1/deployments", json={"repository": "https://github.com/org/app.git",
                         "ref": "main", "subdomain": "demo"}, headers=write_headers(alice))
    assert result.status_code == 202
    job = result.json()
    settings, store = app.state.store.settings, app.state.store
    settings.state_dir.chmod(0o700)
    with store._connection(write=True) as db:
        object.__setattr__(settings, "secret_backend", "encrypted")
        from awesome_deploy.vault_format import ensure_schema, set_format
        ensure_schema(db)
        set_format(db, settings, phase="active", migration_id="fixture")
        db.execute("""CREATE TABLE test_versions(app_id TEXT, name TEXT, version TEXT, status TEXT,
                                               kind TEXT, is_current INTEGER)""")
        db.execute("INSERT INTO test_versions VALUES(?,?,?,?,?,?)",
                   (job["appId"], "TOKEN", "a" * 32, "active", "runtime_secret", 1))
    app.state.vault_service = VaultService(settings, store, vault=VaultAdapter())
    return client, app, provider, alice, job


def test_vault_endpoints_expose_metadata_and_cas_without_plaintext(encrypted_web):
    client, app, _, alice, job = encrypted_web
    url = f"/api/v1/apps/{job['appId']}/secrets"
    result = client.get(url)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["current_version"] == "a" * 32
    assert client.get("/auth/session").json()["capabilities"]["vault_enabled"]
    body = {"value": "sensitive-new-key", "expected_current_version": "a" * 32}
    first = client.post(url + "/TOKEN/rotate", json=body, headers=write_headers(alice))
    assert first.status_code == 200, first.text
    assert "sensitive-new-key" not in first.text
    conflict = client.post(url + "/TOKEN/rotate", json=body, headers=write_headers(alice))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "SECRET_VERSION_CONFLICT"
    operation = client.post(url + "/TOKEN/revoke", json={"version": "a" * 32,
                            "expected_current_version": "b" * 32, "reason": "凭证已泄漏"},
                            headers=write_headers(alice))
    assert operation.status_code == 202 and operation.json()["status"] == "queued"
    assert "execution" not in operation.json()
    assert client.get(url).json()["operations"][0]["id"] == operation.json()["id"]


def test_vault_api_rechecks_recency_and_csrf(encrypted_web):
    client, app, _, alice, job = encrypted_web
    path = f"/api/v1/apps/{job['appId']}/secrets/TOKEN/rotate"
    body = {"value": "sensitive-new-key", "expected_current_version": "a" * 32}
    assert client.post(path, json=body).status_code == 403
    with app.state.identity.connection(write=True) as db:
        db.execute("UPDATE auth_sessions SET created_at=?", (time.time() - 601,))
    response = client.post(path, json=body, headers=write_headers(alice))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_REAUTH_REQUIRED"
    assert "sensitive-new-key" not in response.text


def test_other_user_cannot_read_vault_or_enumerate_operations(encrypted_web):
    client, _, provider, _, job = encrypted_web
    switch_user(client, provider, "ou_stranger")
    assert client.get(f"/api/v1/apps/{job['appId']}/secrets").status_code == 403


def test_legacy_mode_does_not_expose_version_management(web):
    client, _, _ = web
    alice = login(client)
    assert client.get("/api/v1/apps/unknown/secrets").status_code == 404
    assert client.post("/api/v1/apps/unknown/secrets/TOKEN/rotate", json={"value": "secret",
                       "expected_current_version": "a" * 32}, headers=write_headers(alice)).status_code == 404
    assert not client.get("/auth/session").json()["capabilities"]["vault_enabled"]


def test_last_admin_web_error_is_explicit(web):
    client, app, _ = web
    alice = login(client)
    app.state.identity.set_user(alice["userId"], is_admin=True)
    alice = login(client)
    response = client.patch(f"/api/v1/users/{alice['userId']}", json={"enabled": False},
                            headers=write_headers(alice))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_ADMIN_REQUIRED"
