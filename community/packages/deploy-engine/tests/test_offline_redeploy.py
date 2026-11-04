import copy
import uuid

from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.secrets_store import SecretStore
from awesome_deploy.storage import ManagedStorage
from awesome_deploy.worker import Worker
from test_deployment_secrets import AUTH, REPO, post, url
from test_deployment_secrets import setup as setup
from test_managed_secrets import managed_job
from test_offline_data import InventoryDocker
from test_worker import Source


def test_api_delete_worker_cleanup_then_original_app_gets_new_key_and_empty_volume(setup, monkeypatch):
    settings, store, client = setup
    monkeypatch.setattr(store, "_available", lambda _: True)
    original_form = managed_job(store)
    assert post(client, original_form, {"BOOTSTRAP_PASSWORD": "first-disposable-password"}).status_code == 200
    secrets = SecretStore(settings)
    old_key = secrets.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"})
    snapshot = copy.deepcopy(original_form["snapshot"])
    spec = {"name": "data", "mountPath": "/data"}
    snapshot["manifest"]["services"]["web"]["storage"] = spec
    job = store.enqueue(original_form["input"], uuid.uuid4().hex)
    job = store.reserve(job["id"], snapshot)
    docker = InventoryDocker()
    storage = ManagedStorage(docker)
    volume = storage.ensure(job["app_id"], "web", spec)
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": volume}})
    store.finish(job["id"])
    response = client.post(f"/api/v1/apps/{job['app_id']}/offline", auth=AUTH,
        headers={"Idempotency-Key": "delete-data"}, json={
            "expectedReleaseId": job["id"], "dataPolicy": "delete", "deleteConfirmation": "删除 demo",
        })
    assert response.status_code == 202
    backend = DeploymentBackend(settings)
    backend._docker = docker
    backend.offline = lambda target, checkpoint: checkpoint({
        "gateway_deactivated": True, "containers_stopped": True, "cleanup_confirmed": True,
    })
    assert Worker(settings, store, Source([]), backend).run_once()
    assert store.get_offline(response.json()["id"])["status"] == "succeeded"
    assert not docker.rows
    assert secrets.inspect(REPO, "demo", "MASTER_KEY") == "missing"
    assert client.get(url(original_form), auth=AUTH).status_code == 409
    fresh_form = managed_job(store, app_id=job["app_id"])
    description = client.get(url(fresh_form), auth=AUTH)
    assert description.status_code == 200 and description.json()["canGenerate"] is True
    assert post(client, fresh_form, {"BOOTSTRAP_PASSWORD": "second-disposable-password"}).status_code == 200
    new_key = secrets.versions(REPO, "demo", {"MASTER_KEY": "MASTER_KEY"})
    assert old_key != new_key
    fresh = store.enqueue({**job["input"], "appId": job["app_id"]}, uuid.uuid4().hex)
    fresh = store.prepare_context(fresh["id"])
    assert "storage_volumes" not in fresh["execution"] and "previous_manifest" not in fresh["execution"]
    fresh = store.reserve(fresh["id"], snapshot)
    assert fresh["app_id"] == job["app_id"]
    replacement = storage.ensure(fresh["app_id"], "web", spec)
    assert replacement["name"] == volume["name"] and replacement["instance"] != volume["instance"]
