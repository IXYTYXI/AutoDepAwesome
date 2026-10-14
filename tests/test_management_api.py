import copy

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings

AUTH = ("operator", "long-test-password")
BODY = {"repository": "https://github.com/org/repo.git", "ref": "main", "subdomain": "demo"}


@pytest.fixture
def client(tmp_path):
    auth = tmp_path / "admin.json"
    initialize_credentials(auth, *AUTH)
    settings = Settings(state_dir=tmp_path, auth_file=auth, public_url="http://testserver")
    with TestClient(create_app(settings)) as client:
        yield client


def active(client):
    store = client.app.state.store
    store._available = lambda port: True
    job = store.enqueue(BODY, "original")
    snapshot = {"repository": BODY["repository"], "commit": "a" * 40, "manifest": {
        "app": {"subdomain": "demo"},
        "services": {"web": {"image": "example/web@sha256:" + "b" * 64,
            "env": {"BUSINESS_VALUE": "DO_NOT_EXPOSE"}, "resources": {"cpu": 1, "memoryMiB": 128}}},
        "routes": [{"path": "/", "match": "prefix", "service": "web", "stripPrefix": False}],
    }}
    job = store.reserve(job["id"], snapshot)
    store.finish(job["id"])
    return store.get(job["id"])


def test_management_authentication_and_write_protection(client):
    for path in ("/manage", "/api/v1/management", "/assets/manage.js", "/assets/manage.css"):
        assert client.get(path).status_code == 401
    endpoint = "/api/v1/apps/anything/offline"
    body = {"expectedReleaseId": "release"}
    assert client.post(endpoint, json=body).status_code == 401
    assert client.post(endpoint, json=body, auth=AUTH, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post(endpoint, json=body, auth=AUTH, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(endpoint, json=body, auth=AUTH).status_code == 400


def test_management_shows_all_jobs_and_online_app_without_private_data(client):
    job = active(client)
    store = client.app.state.store
    for index in range(61):
        store.enqueue({**BODY, "subdomain": "q" + str(index)}, "q" + str(index))
    response = client.get("/api/v1/management", auth=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert len(data["deployments"]) == 61
    app = data["apps"][0]
    assert app["id"] == job["app_id"] and app["activeReleaseId"] == job["id"]
    assert app["status"] == "online" and app["canOffline"] is True
    assert app["url"] == "https://demo.awesome.guanghexinzhi.cn"
    assert app["commit"] == "a" * 40
    assert "DO_NOT_EXPOSE" not in response.text and "snapshot" not in response.text
    assert "execution" not in response.text and "idempotency_key" not in response.text


def test_offline_submission_is_durable_and_does_not_touch_runtime(client):
    job = active(client)
    store = client.app.state.store
    endpoint = f"/api/v1/apps/{job['app_id']}/offline"
    body = {"expectedReleaseId": job["id"]}
    headers = {"Idempotency-Key": "offline-1", "Origin": "http://testserver"}
    before = copy.deepcopy(store.get(job["id"]))
    response = client.post(endpoint, json=body, headers=headers, auth=AUTH)
    assert response.status_code == 202, response.text
    operation = response.json()
    assert operation["appId"] == job["app_id"] and operation["releaseId"] == job["id"]
    assert operation["status"] == "queued"
    assert client.post(endpoint, json=body, headers=headers, auth=AUTH).json()["id"] == operation["id"]
    assert store.get(job["id"]) == before
    assert store.active_release(job["app_id"])["id"] == job["id"]
    data = client.get("/api/v1/management", auth=AUTH).json()
    assert data["apps"][0]["status"] == "offlining"
    assert data["apps"][0]["canOffline"] is False
    assert data["offlineOperations"][0]["id"] == operation["id"]


def test_stale_release_and_same_app_queued_update_block_offline(client):
    job = active(client)
    endpoint = f"/api/v1/apps/{job['app_id']}/offline"
    headers = {"Idempotency-Key": "offline-1"}
    response = client.post(endpoint, json={"expectedReleaseId": "b" * 32}, auth=AUTH, headers=headers)
    assert response.status_code == 409
    client.app.state.store.enqueue({**BODY, "appId": job["app_id"]}, "update")
    response = client.post(endpoint, json={"expectedReleaseId": job["id"]}, auth=AUTH, headers=headers)
    assert response.status_code == 409
    data = client.get("/api/v1/management", auth=AUTH).json()
    assert data["apps"][0]["canOffline"] is False
    assert data["apps"][0]["offlineBlockReason"]


def test_management_all_statuses_and_immutable_failure_rendering(client):
    store = client.app.state.store
    jobs = [store.enqueue({**BODY, "subdomain": "q"+str(i)}, str(i)) for i in range(3)]
    store.claim(jobs[1]["id"])
    store.fail(jobs[2]["id"], {"code": "OFFLINE_FAILED", "message": "secret-raw-output"}, recovery=True)
    data = client.get("/api/v1/management", auth=AUTH)
    assert data.status_code == 200
    assert {j["status"] for j in data.json()["deployments"]} == {"queued", "running", "recovery_required"}
    assert "secret-raw-output" not in data.text


def test_success_history_does_not_link_to_now_offline_or_replaced_release(client):
    job = active(client)
    assert client.get(f"/api/v1/deployments/{job['id']}", auth=AUTH).json()["url"]
    store = client.app.state.store
    with store._connection(write=True) as db:
        db.execute("UPDATE apps SET active_release_id=NULL WHERE id=?", (job["app_id"],))
    historical = client.get(f"/api/v1/deployments/{job['id']}", auth=AUTH).json()
    assert historical["status"] == "succeeded"
    assert historical["url"] is None and historical["active"] is False
