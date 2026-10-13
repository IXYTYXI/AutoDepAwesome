import pytest
from fastapi.testclient import TestClient

from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings


@pytest.fixture
def client(tmp_path):
    from awesome_deploy.api import create_app

    auth = tmp_path / "admin.json"
    initialize_credentials(auth, "operator", "long-test-password")
    settings = Settings(state_dir=tmp_path, auth_file=auth, public_url="http://testserver")
    with TestClient(create_app(settings)) as c:
        yield c


def headers(key="request-1"):
    return {"Idempotency-Key": key, "Origin": "http://testserver"}


AUTH = ("operator", "long-test-password")
BODY = {"repository": "https://github.com/org/repo.git", "ref": "main", "subdomain": "demo"}


def test_management_endpoints_are_protected(client):
    for url in ["/", "/api/v1/deployments", "/api/v1/apps", "/api/v1/schema"]:
        assert client.get(url).status_code == 401
    assert client.post("/api/v1/deployments", json=BODY, headers=headers()).status_code == 401


def test_submit_idempotent_and_status_available(client):
    r = client.post("/api/v1/deployments", json=BODY, headers=headers(), auth=AUTH)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued"
    assert "approved" not in r.text
    again = client.post("/api/v1/deployments", json=BODY, headers=headers(), auth=AUTH)
    assert again.json()["id"] == job["id"]
    changed = client.post("/api/v1/deployments", json={**BODY, "ref": "dev"}, headers=headers(), auth=AUTH)
    assert changed.status_code == 409
    fetched = client.get("/api/v1/deployments/" + job["id"], auth=AUTH)
    assert fetched.status_code == 200
    assert fetched.json()["url"] is None
    assert "snapshot" not in fetched.json()


def test_csrf_and_request_limits(client):
    r = client.post(
        "/api/v1/deployments",
        json=BODY,
        headers={"Origin": "https://evil.awesome.guanghexinzhi.cn", "Idempotency-Key": "x"},
        auth=AUTH,
    )
    assert r.status_code == 403
    assert client.post("/api/v1/deployments", json=BODY, auth=AUTH).status_code == 400
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "repository": "file:///etc"}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "subdomain": "fuck"}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/deployments", json={**BODY, "approved": True}, headers=headers(), auth=AUTH
        ).status_code
        == 422
    )


def test_schema_and_readiness_do_not_fake_live_deployment(client):
    schema = client.get("/api/v1/schema", auth=AUTH)
    assert schema.status_code == 200
    assert "schemaVersion" in schema.json()["properties"]
    capabilities = client.get("/api/v1/capabilities", auth=AUTH).json()
    assert capabilities["persistentStorage"] is True
    assert capabilities["storage"] == {"driver": "local", "mountPath": "/data", "perService": 1, "updates": "recreate"}
    assert capabilities["approval"] is False
    assert capabilities["serviceKinds"] == ["web"]
    assert client.get("/", auth=AUTH).status_code == 200
