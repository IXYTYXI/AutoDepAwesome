from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings
from awesome_deploy.storage_status import record_quota_status

AUTH = ("operator", "long-test-password")


def configured(tmp_path, backend):
    auth = tmp_path / "admin.json"
    initialize_credentials(auth, *AUTH)
    return Settings(state_dir=tmp_path, auth_file=auth, public_url="http://testserver",
                    storage_backend=backend)


def test_api_versions_schema_without_silently_changing_v1(tmp_path):
    with TestClient(create_app(configured(tmp_path, "local"))) as client:
        assert client.get("/api/v1/schema?version=2").status_code == 401
        assert client.get("/api/v1/schema", auth=AUTH).json()["properties"]["schemaVersion"]["const"] == 1
        assert client.get("/api/v1/schema?version=2", auth=AUTH).json()[
            "properties"]["schemaVersion"]["const"] == 2
        assert client.get("/api/v1/schema?version=3", auth=AUTH).status_code == 422
        result = client.get("/api/v1/capabilities", auth=AUTH).json()
        assert result["schemaVersions"] == [1, 2]
        assert result["storageHardQuota"]["supported"] is True
        assert result["storageHardQuota"]["configured"] is False
        assert result["storageHardQuota"]["enabled"] is False


def test_api_only_reads_a_fresh_worker_snapshot_and_never_invokes_sudo(tmp_path, monkeypatch):
    settings = configured(tmp_path, "xfs-project")
    from awesome_deploy.quota_client import QuotaClient
    monkeypatch.setattr(QuotaClient, "call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("API must not invoke privileged helper")))
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/v1/capabilities", auth=AUTH).json()
        assert result["storageHardQuota"]["status"] == "unknown"
        assert result["storage"]["driver"] == "xfs-project"
        from awesome_deploy.storage_status import begin_quota_observation
        record_quota_status(settings, {"enforced": True, "path": "/not-public"},
                            observation=begin_quota_observation(settings))
        result = client.get("/api/v1/capabilities", auth=AUTH).json()
        assert result["storageHardQuota"]["enabled"] is True
        assert result["storageHardQuota"]["checkedAt"] is not None
        assert "/not-public" not in str(result)
