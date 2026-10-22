import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


class FakeProvider:
    person = "ou_alice"
    tenant = "tenant"
    name = "张三"
    allowed = True
    codes = []

    def authorize_url(self, state, challenge):
        from urllib.parse import urlencode
        return "https://accounts.feishu.cn/authorize?" + urlencode({"state": state, "code_challenge": challenge})

    def exchange(self, code, verifier):
        self.codes.append((code, verifier))
        return {"open_id": self.person, "tenant_key": self.tenant, "name": self.name}

    def check_eligible(self, open_id):
        return self.allowed

    def close(self):
        pass


@pytest.fixture(params=["continuous", "login"])
def web(tmp_path, request):
    from awesome_deploy.api import create_app
    settings = Settings(state_dir=tmp_path, public_url="https://testserver", auth_mode="feishu",
                        feishu_app_id="cli_test", feishu_tenant_key="tenant", feishu_app_secret_file=tmp_path / "key",
                        feishu_eligibility_mode=request.param)
    provider = FakeProvider()
    if request.param == "login":
        provider.check_eligible = lambda open_id: pytest.fail("login mode must not poll Feishu")
    app = create_app(settings, identity_provider=provider)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client, app, provider


def login(client):
    start = client.get("/auth/feishu/start")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    result = client.get("/auth/feishu/callback", params={"state": query["state"][0], "code": "test-code"})
    assert result.status_code == 303 and result.headers["location"] == "/"
    return client.get("/auth/session").json()


def test_api_access_login_returns_to_requested_page(web):
    client, _, _ = web
    start = client.get("/auth/feishu/start", params={"return": "/api-access"})
    query = parse_qs(urlsplit(start.headers["location"]).query)
    result = client.get("/auth/feishu/callback", params={"state": query["state"][0], "code": "test-code"})
    assert result.status_code == 303
    assert result.headers["location"] == "/api-access"
    assert client.get("/api-access").status_code == 200


def test_document_login_returns_to_selected_document(web):
    client, _, _ = web
    start = client.get("/auth/feishu/start", params={"return": "/document?doc=api"})
    query = parse_qs(urlsplit(start.headers["location"]).query)
    result = client.get("/auth/feishu/callback", params={"state": query["state"][0], "code": "test-code"})
    assert result.status_code == 303
    assert result.headers["location"] == "/document?doc=api"


def test_login_session_headers_and_basic_cannot_bypass(web):
    client, app, provider = web
    assert client.get("/").status_code == 303
    assert client.get("/document").headers["location"] == "/login?error=session&return=%2Fdocument"
    assert client.get("/document?doc=api").headers["location"] == (
        "/login?error=session&return=%2Fdocument%3Fdoc%3Dapi"
    )
    assert client.get("/api-access").headers["location"] == "/login?error=session&return=%2Fapi-access"
    assert client.get("/document/deployment-guide.txt").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/api/v1/apps", auth=("operator", "password")).status_code == 401
    session = login(client)
    assert session["role"] == "member" and session["canDeploy"] is True
    assert session["userId"] and session["csrfToken"]
    assert client.get("/document").status_code == 200
    assert client.get("/document?doc=api").status_code == 200
    assert client.get("/document/deployment-guide.txt").status_code == 200
    assert "open_id" not in session and "access_token" not in session
    cookie = client.cookies.get("__Host-awesome_session")
    assert cookie and len(cookie) >= 43
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_state_browser_binding_replay_and_cookie_flags(web):
    client, app, provider = web
    start = client.get("/auth/feishu/start")
    cookie_header = start.headers["set-cookie"]
    assert all(x in cookie_header for x in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
    assert "Domain=" not in cookie_header
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    cookie = client.cookies.get("__Host-awesome_oauth")
    client.cookies.clear()
    assert "error=expired" in client.get("/auth/feishu/callback", params={"state": state, "code": "x"}).headers["location"]
    client.cookies.set("__Host-awesome_oauth", cookie, domain="testserver.local", path="/")
    success = client.get("/auth/feishu/callback", params={"state": state, "code": "x"})
    assert success.headers["location"] == "/"
    code, verifier = provider.codes[-1]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert expected == parse_qs(urlsplit(start.headers["location"]).query)["code_challenge"][0]
    assert "error=expired" in client.get("/auth/feishu/callback", params={"state": state, "code": "x"}).headers["location"]


def test_csrf_origin_exact_and_logout(web):
    client, app, provider = web
    session = login(client)
    body = {"repository": "https://github.com/org/repo.git", "ref": "main", "subdomain": "demo"}
    headers = {"Idempotency-Key": "one", "Origin": "https://testserver"}
    assert client.post("/api/v1/deployments", json=body, headers=headers).status_code == 403
    headers["X-CSRF-Token"] = session["csrfToken"]
    assert client.post("/api/v1/deployments", json=body, headers={**headers, "Origin": "https://app.testserver"}).status_code == 403
    assert client.post("/api/v1/deployments", json=body, headers={k: v for k, v in headers.items() if k != "Origin"}).status_code == 403
    assert client.post("/api/v1/deployments", json=body, headers=headers).status_code == 202
    assert client.post("/auth/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/v1/apps").status_code == 401


def test_cross_tenant_callback_denied(web):
    client, app, provider = web
    provider.tenant = "other"
    start = client.get("/auth/feishu/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    result = client.get("/auth/feishu/callback", params={"state": state, "code": "x"})
    assert result.headers["location"] == "/login?error=denied"
    assert client.get("/auth/session").status_code == 401


def test_worker_forced_checks_keep_local_membership_and_user_revocation(web):
    client, app, provider = web
    alice = login(client)
    body = {"repository": "https://github.com/org/repo.git", "ref": "main", "subdomain": "demo"}
    job = client.post("/api/v1/deployments", json=body, headers=write_headers(alice)).json()
    with app.state.identity.connection(write=True) as db:
        db.execute("UPDATE auth_users SET checked_at=checked_at-301")
    assert app.state.access.authorize_operation("job", job["id"], force=True)["id"] == alice["userId"]
    assert client.get("/auth/session").status_code == 200
    with app.state.identity.connection(write=True) as db:
        db.execute("UPDATE app_members SET enabled=0 WHERE app_id=?", (job["appId"],))
    with pytest.raises(DeployError) as error:
        app.state.access.authorize_operation("job", job["id"], force=True)
    assert error.value.code == "ACCESS_DENIED"
    app.state.identity.set_user(alice["userId"], enabled=False)
    assert client.get("/auth/session").status_code == 401


def write_headers(session, key="deploy-key"):
    return {"Origin": "https://testserver", "X-CSRF-Token": session["csrfToken"], "Idempotency-Key": key}


def switch_user(client, provider, person):
    client.cookies.clear()
    provider.person = person
    return login(client)


def test_two_members_cannot_read_mutate_or_replay_each_others_app(web):
    client, app, provider = web
    alice = login(client)
    body = {"repository": "https://github.com/org/private.git", "ref": "main", "subdomain": "alice"}
    result = client.post("/api/v1/deployments", json=body, headers=write_headers(alice))
    assert result.status_code == 202, result.text
    job = result.json()
    bob = switch_user(client, provider, "ou_bob")
    assert client.get("/api/v1/apps").json() == {"items": []}
    assert client.get("/api/v1/deployments").json() == {"items": []}
    assert client.get("/api/v1/management").json() == {"apps": [], "deployments": [], "offlineOperations": []}
    for path in (f"/api/v1/deployments/{job['id']}", f"/api/v1/deployments/{job['id']}/events",
                 f"/api/v1/deployments/{job['id']}/secrets", f"/api/v1/apps/{job['appId']}/members"):
        assert client.get(path).status_code == 403
    for payload in (body, {**body, "appId": job["appId"]}):
        response = client.post("/api/v1/deployments", json=payload, headers=write_headers(bob))
        assert response.status_code in {403, 409}
    response = client.post("/api/v1/deployments", json={**body, "subdomain": "bob"}, headers=write_headers(bob))
    assert response.status_code == 202 and response.json()["id"] != job["id"]


def test_member_roles_safe_projection_revocation_and_transfer(web):
    client, app, provider = web
    bob = switch_user(client, provider, "ou_bob")
    alice = switch_user(client, provider, "ou_alice")
    body = {"repository": "https://github.com/org/secret-repo.git", "ref": "feature-private", "subdomain": "alice"}
    job = client.post("/api/v1/deployments", json=body, headers=write_headers(alice)).json()
    path = f"/api/v1/apps/{job['appId']}/members/{bob['userId']}"
    assert client.put(path, json={"role": "viewer"}, headers=write_headers(alice)).status_code == 200
    bob = switch_user(client, provider, "ou_bob")
    apps = client.get("/api/v1/apps").json()["items"]
    assert len(apps) == 1 and apps[0]["permissions"]["deploy"] is False
    assert "repository" not in apps[0]
    management = client.get("/api/v1/management").json()
    assert body["repository"] not in str(management) and body["ref"] not in str(management)
    assert management["deployments"] == []
    assert client.get(f"/api/v1/deployments/{job['id']}/events").status_code == 403
    assert client.post("/api/v1/deployments", json={**body, "appId": job["appId"]}, headers=write_headers(bob)).status_code == 403
    alice = switch_user(client, provider, "ou_alice")
    assert client.put(path, json={"role": "maintainer"}, headers=write_headers(alice)).status_code == 200
    bob = switch_user(client, provider, "ou_bob")
    assert client.get(f"/api/v1/deployments/{job['id']}/events").status_code == 200
    assert client.get(f"/api/v1/apps/{job['appId']}/members").status_code == 403
    assert client.post(f"/api/v1/apps/{job['appId']}/offline", json={"expectedReleaseId": job["id"]}, headers=write_headers(bob)).status_code == 403
    alice = switch_user(client, provider, "ou_alice")
    assert client.post(f"/api/v1/apps/{job['appId']}/transfer", json={"userId": bob["userId"]}, headers=write_headers(alice)).status_code == 200
    assert client.get(f"/api/v1/apps/{job['appId']}/members").status_code == 403


def test_global_permission_revocation_and_recent_login_gate(web):
    client, app, provider = web
    alice = login(client)
    app.state.identity.set_user(alice["userId"], is_admin=True)
    assert client.get("/auth/session").status_code == 401
    alice = login(client)
    bob = switch_user(client, provider, "ou_bob")
    assert client.get("/api/v1/users").status_code == 403
    alice = switch_user(client, provider, "ou_alice")
    with app.state.identity.connection(write=True) as db:
        db.execute("UPDATE auth_sessions SET created_at=created_at-601")
    response = client.patch(f"/api/v1/users/{bob['userId']}", json={"enabled": False}, headers=write_headers(alice))
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH_REAUTH_REQUIRED"
    alice = login(client)
    assert client.patch(f"/api/v1/users/{bob['userId']}", json={"enabled": False}, headers=write_headers(alice)).status_code == 200
    assert app.state.identity.get_user(bob["userId"])["enabled"] == 0


def test_anonymous_callback_flood_does_not_grow_audit_and_real_failures_are_bounded(web):
    client, app, provider = web
    for _ in range(60):
        assert client.get("/auth/feishu/callback").status_code == 303
    with app.state.identity.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_audit").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM auth_login_failures").fetchone()[0] == 0
    for _ in range(3):
        start = client.get("/auth/feishu/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        client.get("/auth/feishu/callback", params={"state": state, "error": "access_denied"})
    with app.state.identity.connection() as db:
        assert db.execute("SELECT SUM(count) FROM auth_login_failures").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM auth_login_failures").fetchone()[0] <= 2


def test_repository_credentials_require_explicit_admin_authorization(web):
    client, app, provider = web
    alice = login(client)
    body = {"repository": "https://github.com/org/private.git", "ref": "main", "subdomain": "demo"}
    job = client.post("/api/v1/deployments", json=body, headers=write_headers(alice)).json()
    path = f"/api/v1/apps/{job['appId']}/repository-credentials"
    assert client.put(path, json={"approved": False}, headers=write_headers(alice)).status_code == 403
    app.state.identity.set_user(alice["userId"], is_admin=True)
    alice = login(client)
    assert client.put(path, json={"approved": True}, headers=write_headers(alice)).status_code == 409
    assert client.put(path, json={"approved": False}, headers=write_headers(alice)).status_code == 200
