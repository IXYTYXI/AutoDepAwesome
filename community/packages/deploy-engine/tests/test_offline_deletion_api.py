import json

import pytest

from awesome_deploy.diagnostics import PHASE_LABELS, localize_error
from awesome_deploy.errors import DeployError
from awesome_deploy.management import public_application, public_operation
from test_management_api import AUTH, active, client as _management_client

client = _management_client


def operation(**changes):
    return {"id": "operation", "app_id": "app", "target_release_id": "release", "status": "queued",
            "phase": "offline_queued", "created_at": 1, "updated_at": 2, "idempotency_key": "retry-key",
            "execution": {}, **changes}


@pytest.mark.parametrize("policy", [None, "retain", "delete"])
def test_offline_api_forwards_explicit_policy_and_confirmation_without_exposing_execution(client, monkeypatch, policy):
    calls = []
    def enqueue(app_id, release, key, **kwargs):
        calls.append((app_id, release, key, kwargs))
        return operation(app_id=app_id, target_release_id=release, execution={
            "data_policy": kwargs.get("data_policy", "retain"),
            "delete_plan": {"private-version": "never-expose-value"},
        })
    monkeypatch.setattr(client.app.state.store, "enqueue_offline", enqueue)
    body = {"expectedReleaseId": "release"}
    if policy is not None:
        body["dataPolicy"] = policy
    if policy == "delete":
        body["deleteConfirmation"] = "删除 demo"
    response = client.post("/api/v1/apps/app/offline", json=body, auth=AUTH,
                           headers={"Idempotency-Key": "request"})
    assert response.status_code == 202, response.text
    assert calls == [("app", "release", "request", {
        "data_policy": policy or "retain", "delete_confirmation": "删除 demo" if policy == "delete" else None,
    })]
    assert response.json()["dataPolicy"] == (policy or "retain")
    assert "private-version" not in response.text and "never-expose-value" not in response.text
    assert "delete_plan" not in response.text and "execution" not in response.text


@pytest.mark.parametrize("extra", [
    {"dataPolicy": "unknown-private-value"}, {"dataPolicy": True}, {"dataPolicy": None},
    {"deleteConfirmation": 123}, {"deleteConfirmation": ["private-value"]},
    {"unexpected": "private-value"},
])
def test_offline_api_rejects_invalid_fields_without_echoing_input(client, monkeypatch, extra):
    monkeypatch.setattr(client.app.state.store, "enqueue_offline", lambda *args, **kwargs: pytest.fail("invalid input enqueued"))
    response = client.post("/api/v1/apps/app/offline", json={"expectedReleaseId": "release", **extra},
                           auth=AUTH, headers={"Idempotency-Key": "request"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "private-value" not in response.text


def test_management_capabilities_describe_both_data_policies_and_stored_secret_deletion(client):
    capability = client.get("/api/v1/capabilities", auth=AUTH).json()["applicationManagement"]
    assert capability["dataPolicies"] == ["retain", "delete"]
    assert capability["deletesStoredSecrets"] is True
    assert capability["retainsData"] is True


@pytest.mark.parametrize("policy", [None, "retain", "delete"])
def test_public_offline_operation_exposes_only_policy_and_safe_retry_metadata(policy):
    execution = {"secret_delete_plan": {"value": "never-expose-value", "inode": 123},
                 "volume_delete_plan": {"name": "private-volume"}, "secret_versions": {"MASTER_KEY": "private-version"}}
    if policy is not None:
        execution["data_policy"] = policy
    result = public_operation(operation(status="recovery_required", execution=execution))
    assert result["dataPolicy"] == (policy or "retain")
    assert result["retryKey"] == "retry-key"
    assert set(result) == {"id", "appId", "releaseId", "status", "phase", "error", "createdAt", "updatedAt", "retryable", "retryKey", "dataPolicy"}
    assert not any(value in json.dumps(result) for value in ("never-expose-value", "private-volume", "private-version", "inode"))


@pytest.mark.parametrize("active_release,retained_flag,expected", [(False, True, True), (False, False, False), (False, "true", False), (True, True, False)])
def test_public_application_requires_verified_retained_deletion_and_no_active_release(active_release, retained_flag, expected):
    retained = {"id": "release", "input": {"subdomain": "demo", "ref": "main"}, "snapshot": {},
                "execution": {"data_deleted": retained_flag, "secret_delete_plan": "never-expose-value"}}
    app = {"id": "app", "repository": "https://github.com/org/repo", "subdomain": "demo" if active_release else None,
           "active_release_id": "new" if active_release else None, "updated_at": 2,
           "active_job": {**retained, "id": "new"} if active_release else None, "retained_job": retained}
    result = public_application(app, "example.test")
    assert result["dataDeleted"] is expected
    assert "never-expose-value" not in json.dumps(result)
    app["retained_job"] = None
    assert public_application(app, "example.test")["dataDeleted"] is False


@pytest.mark.parametrize("code,status", [
    ("OFFLINE_DATA_CONFLICT", 409), ("OFFLINE_DELETE_CONFIRMATION", 422), ("OFFLINE_DATA_POLICY", 422),
    ("SECRET_DELETE_CONFLICT", 409), ("STORAGE_IN_USE", 409), ("STORAGE_DELETE_FAILED", 422),
    ("OFFLINE_DATA_DELETE_FAILED", 422),
])
def test_offline_errors_are_localized_without_raw_metadata(client, monkeypatch, code, status):
    def reject(*args, **kwargs):
        raise DeployError(code, "never-expose-value", suggestion="private-plan")
    monkeypatch.setattr(client.app.state.store, "enqueue_offline", reject)
    response = client.post("/api/v1/apps/app/offline", json={"expectedReleaseId": "release"},
                           auth=AUTH, headers={"Idempotency-Key": "request"})
    assert response.status_code == status
    result = response.json()["error"]
    assert result["code"] == code
    assert result["title"] != "部署未完成" and result["steps"]
    assert "never-expose-value" not in response.text and "private-plan" not in response.text


def test_delete_diagnostic_phases_and_generic_failure_do_not_claim_data_is_retained():
    for phase in ("offline_delete_preflight", "offline_delete_data", "offline_delete_secrets"):
        assert phase in PHASE_LABELS and PHASE_LABELS[phase] != phase
    error = localize_error({"code": "OFFLINE_FAILED"}, "offline_delete_secrets")
    assert "数据和部署记录仍保留" not in error["message"]
    assert "部分" in json.dumps(error, ensure_ascii=False)


def test_real_store_preserves_default_retain_policy_in_management(client):
    release = active(client)
    response = client.post(f"/api/v1/apps/{release['app_id']}/offline", json={"expectedReleaseId": release["id"]},
                           auth=AUTH, headers={"Idempotency-Key": "retain-existing"})
    assert response.status_code == 202, response.text
    assert response.json()["dataPolicy"] == "retain"
    result = client.get("/api/v1/management", auth=AUTH).json()
    assert result["apps"][0]["dataDeleted"] is False
    assert result["offlineOperations"][0]["dataPolicy"] == "retain"


def test_unrecognized_stored_data_policy_is_not_copied_into_public_operation():
    result = public_operation(operation(execution={"data_policy": {"private-plan": "never-expose-value"}}))
    assert result["dataPolicy"] is None
    assert "private-plan" not in json.dumps(result)
