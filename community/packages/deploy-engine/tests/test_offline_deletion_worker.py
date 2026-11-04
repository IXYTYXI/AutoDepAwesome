import pytest

from awesome_deploy.config import Settings
from awesome_deploy.store import Store
from awesome_deploy.storage import ManagedStorage
from test_offline_lifecycle import active, reservations, worker, OfflineBackend
from test_worker import Crash


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_available", staticmethod(lambda port: True))
    return Store(Settings(state_dir=tmp_path, port_start=42500, port_end=42900))


class DeleteBackend(OfflineBackend):
    def inventory_offline_data(self, metadata):
        self.call("inventory")
        assert self.lock_fd is not None
        return {"metadata": metadata, "frozen_identity": "original-only"}

    def delete_offline_data(self, plan, checkpoint):
        assert plan["frozen_identity"] == "original-only"
        checkpoint({"phase": "offline_delete_data", "data_delete_intent": True})
        self.call("delete_data")
        checkpoint({"volumes_deleted": True})
        self.call("delete_secrets")
        checkpoint({"secrets_deleted": True, "data_deleted": True})


def pending_delete(store):
    job = active(store)
    spec = job["snapshot"]["manifest"]["services"]["web"]["storage"]
    name, _ = ManagedStorage.identity(job["app_id"], "web", spec)
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": {
        "name": name, "created_at": "original", "instance": "a" * 32,
    }}})
    operation = store.enqueue_offline(job["app_id"], job["id"], "delete", data_policy="delete",
                                      delete_confirmation="删除 demo")
    return job, operation


def test_delete_preflights_then_stops_then_purges_before_releasing(store):
    job, operation = pending_delete(store)
    backend = DeleteBackend([])
    runner, _ = worker(store, backend)
    runner.run_once()
    assert backend.calls == ["inventory", "offline", "delete_data", "delete_secrets"]
    result = store.get_offline(operation["id"])
    assert result["status"] == "succeeded"
    assert result["execution"]["data_deleted"] is True
    assert reservations(store) == []
    assert store.active_release(job["app_id"]) is None


def test_preflight_failure_does_not_stop_app_or_delete_data(store):
    job, operation = pending_delete(store)
    backend = DeleteBackend([])
    backend.fail_at = "inventory"
    worker(store, backend)[0].run_once()
    assert backend.calls == ["inventory"]
    assert store.get_offline(operation["id"])["status"] == "recovery_required"
    assert store.active_release(job["app_id"])["id"] == job["id"]
    assert reservations(store)


@pytest.mark.parametrize("crash_at", ["offline", "delete_data", "delete_secrets"])
def test_restart_resumes_same_frozen_plan_and_does_not_restart_traffic(store, crash_at):
    _, operation = pending_delete(store)
    backend = DeleteBackend([])
    backend.crash_at = crash_at
    runner, _ = worker(store, backend)
    with pytest.raises(Crash):
        runner.run_once()
    assert reservations(store)
    assert store.get_offline(operation["id"])["execution"]["deletion_plan"]["frozen_identity"] == "original-only"
    backend.crash_at = None
    backend.calls.clear()
    runner.run_once()
    assert "inventory" not in backend.calls
    if crash_at != "offline":
        assert "offline" not in backend.calls
    assert "recover_active" not in backend.calls
    assert store.get_offline(operation["id"])["status"] == "succeeded"


def test_partial_failure_preserves_gates_and_sanitizes_unknown_error(store):
    _, operation = pending_delete(store)
    backend = DeleteBackend([])
    backend.fail_at = "delete_secrets"
    worker(store, backend)[0].run_once()
    result = store.get_offline(operation["id"])
    assert result["status"] == "recovery_required"
    assert result["execution"]["volumes_deleted"] is True
    assert not result["execution"].get("data_deleted")
    assert "raw-value" not in str(result["error"])
    assert reservations(store)


def test_retain_never_enumerates_or_deletes_saved_data(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "retain")
    backend = DeleteBackend([])
    worker(store, backend)[0].run_once()
    assert backend.calls == ["offline"]
    assert store.get_offline(operation["id"])["status"] == "succeeded"


def test_journal_cannot_expand_the_accepted_app_scopes(store):
    _, operation = pending_delete(store)
    store.claim_offline(operation["id"])
    store.checkpoint_offline(operation["id"], {"deletion_plan": {
        "metadata": {"app_id": "other"}, "frozen_identity": "original-only",
    }})
    backend = DeleteBackend([])
    worker(store, backend)[0].run_once()
    result = store.get_offline(operation["id"])
    assert result["status"] == "recovery_required"
    assert result["error"]["code"] == "OFFLINE_DATA_CONFLICT"
    assert backend.calls == []
