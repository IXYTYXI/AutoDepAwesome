import concurrent.futures
import copy
import sqlite3
from types import SimpleNamespace

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.store import Store
from awesome_deploy.worker import Worker
from test_worker import Backend, Crash, Source


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_available", staticmethod(lambda port: True))
    return Store(Settings(state_dir=tmp_path, port_start=42500, port_end=42900))


def payload(app_id=None, domain="demo"):
    value = {"repository": "https://github.com/acme/demo", "ref": "main", "subdomain": domain}
    if app_id:
        value["appId"] = app_id
    return value


def active(store, domain="demo"):
    job = store.enqueue(payload(domain=domain), "initial-" + domain)
    snapshot = {"manifest": {"services": {"web": {
        "resources": {"cpu": 0.5, "memoryMiB": 128},
        "storage": {"name": "images", "mountPath": "/data"},
    }}}, "commit": "a" * 40}
    store.claim(job["id"])
    store.reserve(job["id"], snapshot)
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": {
        "name": "preserved", "created_at": "original", "instance": "a" * 32,
    }}})
    return store.finish(job["id"])


def reservations(store):
    with sqlite3.connect(store.settings.database_path) as db:
        return db.execute("SELECT job_id,service,port FROM reservations ORDER BY job_id,service").fetchall()


class OfflineBackend(Backend):
    def offline(self, job, checkpoint):
        self.call("offline")
        assert self.lock_fd is not None
        checkpoint({"gateway_deactivated": True, "containers_stopped": True, "cleanup_confirmed": True})
        self.cleaned.append(job["id"])


def worker(store, backend=None):
    calls = []
    backend = backend or OfflineBackend(calls)
    return Worker(store.settings, store, Source(calls), backend), backend


def test_offline_request_is_durable_idempotent_and_checks_exact_release(store):
    job = active(store)
    with pytest.raises(DeployError, match="release"):
        store.enqueue_offline(job["app_id"], "stale", "wrong")
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    reopened = Store(store.settings)
    assert reopened.enqueue_offline(job["app_id"], job["id"], "offline") == operation
    assert reopened.get_offline(operation["id"])["target_release_id"] == job["id"]
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job["app_id"], "changed", "offline")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("status", ["queued", "running", "recovery_required"])
@pytest.mark.parametrize("identity", ["app", "domain"])
def test_offline_rejects_all_busy_deployments_including_unresolved_queue(store, status, identity):
    job = active(store)
    queued = store.enqueue(payload(job["app_id"] if identity == "app" else None), "next")
    if status == "running":
        store.claim(queued["id"])
    elif status == "recovery_required":
        store.fail(queued["id"], {"code": "CLEANUP_FAILED"}, recovery=True)
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job["app_id"], job["id"], "offline")
    assert error.value.code == "APP_BUSY"
    assert not store.list_offline_operations()


def test_offline_rejects_unretired_previous_release(store):
    job = active(store)
    store.checkpoint(job["id"], execution={"retirement_pending": ["old"]})
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job["app_id"], job["id"], "offline")
    assert error.value.code == "APP_BUSY"


def test_pending_offline_blocks_new_deploy_and_recovery_without_blocking_other_app(store):
    job = active(store)
    store.enqueue_offline(job["app_id"], job["id"], "offline")
    assert store.next_active_recovery("new-boot") is None
    with pytest.raises(DeployError):
        store.begin_active_recovery(job["id"], "new-boot")
    for data in (payload(job["app_id"]), payload()):
        with pytest.raises(DeployError) as error:
            store.enqueue(data, "new-" + str(bool(data.get("appId"))))
        assert error.value.code == "APP_OFFLINE_BUSY"
    assert store.enqueue(payload(domain="other"), "other")["status"] == "queued"


def test_deploy_and_offline_admission_are_serialized_in_one_transaction(store):
    job = active(store)

    def attempt(kind):
        try:
            if kind == "offline":
                store.enqueue_offline(job["app_id"], job["id"], "offline")
            else:
                store.enqueue(payload(job["app_id"]), "next")
            return "accepted"
        except DeployError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(attempt, ["offline", "deploy"]))
    assert results.count("accepted") == 1
    assert set(results) & {"APP_BUSY", "APP_OFFLINE_BUSY"}


def test_worker_offline_releases_only_target_and_never_resurrects_it(store):
    target = active(store)
    other = active(store, "other")
    prior = reservations(store)
    operation = store.enqueue_offline(target["app_id"], target["id"], "offline")
    runner, backend = worker(store)
    assert runner.run_once()
    assert backend.cleaned == [target["id"]]
    assert not backend.recovered
    assert store.get_offline(operation["id"])["status"] == "succeeded"
    assert store.active_release(target["app_id"]) is None
    assert store.active_release(other["app_id"])["id"] == other["id"]
    assert reservations(store) == [row for row in prior if row[0] == other["id"]]
    retained = store.get(target["id"])
    assert retained["status"] == "succeeded"
    assert retained["execution"]["resources_released"] is True
    assert retained["execution"]["storage_volumes"] == target["execution"]["storage_volumes"]
    assert len(store.apps()) == 2
    runner.run_once()
    assert target["id"] not in backend.recovered


def test_worker_replays_interrupted_offline_after_restart(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    runner, backend = worker(store)
    backend.crash_at = "offline"
    with pytest.raises(Crash):
        runner.run_once()
    assert store.get_offline(operation["id"])["status"] == "running"
    assert reservations(store)
    reopened = Store(store.settings)
    resumed, healthy = worker(reopened)
    assert resumed.run_once()
    assert reopened.get_offline(operation["id"])["status"] == "succeeded"
    assert healthy.cleaned == [job["id"]]


def test_crash_after_cleanup_before_completion_rechecks_backend_on_replay(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    runner, backend = worker(store)
    original = backend.offline

    def crash_after_cleanup(target, checkpoint):
        original(target, checkpoint)
        raise Crash("after cleanup")

    backend.offline = crash_after_cleanup
    with pytest.raises(Crash):
        runner.run_once()
    assert store.get_offline(operation["id"])["execution"]["cleanup_confirmed"] is True
    assert reservations(store)
    resumed, replacement = worker(Store(store.settings))
    assert resumed.run_once()
    assert replacement.cleaned == [job["id"]]
    assert not reservations(store)


def test_worker_handles_offline_request_racing_active_recovery_selection(store, monkeypatch):
    job = active(store)
    runner, backend = worker(store)
    original = store.next_active_recovery

    def select_then_request(boot_id):
        selected = original(boot_id)
        store.enqueue_offline(job["app_id"], job["id"], "offline")
        return selected

    monkeypatch.setattr(store, "next_active_recovery", select_then_request)
    assert runner.run_once()
    assert not backend.recovered
    assert runner.run_once()
    assert backend.cleaned == [job["id"]]


def test_recovery_can_finish_bookkeeping_when_offline_is_requested_during_it(store):
    job = active(store)
    runner, backend = worker(store)
    original = backend.recover_active

    def recover_then_request(target, checkpoint):
        result = original(target, checkpoint)
        store.enqueue_offline(job["app_id"], job["id"], "offline")
        return result

    backend.recover_active = recover_then_request
    assert runner.run_once()
    assert runner.run_once()
    assert backend.cleaned == [job["id"]]


def test_completion_refuses_unconfirmed_cleanup_or_changed_release_without_releasing_reservations(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    store.claim_offline(operation["id"])
    store.checkpoint_offline(operation["id"], {"gateway_deactivated": True, "containers_stopped": True})
    with pytest.raises(DeployError) as error:
        store.finish_offline(operation["id"])
    assert error.value.code == "OFFLINE_STATE"
    assert reservations(store)
    store.checkpoint_offline(operation["id"], {"cleanup_confirmed": True})
    with sqlite3.connect(store.settings.database_path) as db:
        db.execute("UPDATE apps SET active_release_id='foreign' WHERE id=?", (job["app_id"],))
    with pytest.raises(DeployError) as error:
        store.finish_offline(operation["id"])
    assert error.value.code == "OFFLINE_RELEASE_CHANGED"
    assert reservations(store)
    assert not store.get(job["id"])["execution"].get("resources_released")


def test_offline_failure_is_durable_blocks_recovery_and_explicit_same_key_retries(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    runner, backend = worker(store)
    backend.fail_at = "offline"
    assert runner.run_once()
    failed = store.get_offline(operation["id"])
    assert failed["status"] == "recovery_required"
    assert "SECRET_TOKEN" not in str(failed)
    assert reservations(store)
    assert runner.run_once() is False
    assert store.next_active_recovery("new-boot") is None
    assert store.enqueue_offline(job["app_id"], job["id"], "offline")["status"] == "queued"
    backend.fail_at = None
    assert runner.run_once()
    assert store.get_offline(operation["id"])["status"] == "succeeded"


def test_offline_redeploy_retains_manifest_and_exact_volume_without_active_rollback(store):
    old = active(store)
    store.enqueue_offline(old["app_id"], old["id"], "offline")
    runner, _ = worker(store)
    runner.run_once()
    candidate = store.enqueue(payload(old["app_id"]), "redeploy")
    prepared = store.prepare_context(candidate["id"])
    assert prepared["execution"]["previous_release"] is None
    assert prepared["execution"]["previous_manifest"] == old["snapshot"]["manifest"]
    assert prepared["execution"]["storage_volumes"] == old["execution"]["storage_volumes"]
    reserved = store.reserve(candidate["id"], old["snapshot"])
    assert reserved["execution"]["previous_release"] is None
    assert reserved["execution"]["storage_volumes"] == old["execution"]["storage_volumes"]
    runner._recover_previous_for_rollback(reserved)
    assert store.active_release(old["app_id"]) is None


def test_management_includes_every_nonterminal_job_and_retained_app_metadata(store):
    old = active(store)
    for index in range(205):
        store.enqueue(payload(domain=f"q{index}"), f"queued-{index}")
    operation = store.enqueue_offline(old["app_id"], old["id"], "offline")
    snapshot = store.management_snapshot()
    assert len(snapshot["deployments"]) == 205
    assert snapshot["apps"][0]["active_job"]["id"] == old["id"]
    assert snapshot["apps"][0]["operation"]["id"] == operation["id"]
    assert snapshot["offline_operations"] == [operation]
    runner, _ = worker(store)
    runner.run_once()
    app = store.management_snapshot()["apps"][0]
    assert app["active_job"] is None
    assert app["retained_job"]["id"] == old["id"]


@pytest.mark.parametrize("response", [{}, {"deactivated": False}, {"deactivated": True, "release": "foreign"}])
def test_runtime_requires_exact_gateway_confirmation_before_stopping_or_cleanup(tmp_path, response):
    backend = DeploymentBackend(Settings(state_dir=tmp_path))
    calls = []
    backend._runtime_preflight = lambda: None
    backend.gateway = SimpleNamespace(call=lambda op, job: response, lock_fd=None)
    backend._stop_owned_containers = lambda *args, **kwargs: calls.append("stop")
    backend.cleanup = lambda job: calls.append("cleanup")
    with pytest.raises(DeployError):
        backend.offline({"id": "target", "status": "succeeded", "execution": {}}, lambda delta: None)
    assert calls == []


def test_runtime_offline_order_and_retry_never_mutate_release_record(tmp_path):
    backend = DeploymentBackend(Settings(state_dir=tmp_path))
    calls, checkpoints = [], []
    backend._runtime_preflight = lambda: None

    def gateway(op, job):
        calls.append(op)
        return {"deactivated": True, "release": job["id"]}

    backend.gateway = SimpleNamespace(call=gateway, lock_fd=None)
    backend._stop_owned_containers = lambda *args, **kwargs: calls.append("stop")
    backend.cleanup = lambda job: calls.append("cleanup")
    job = {"id": "target", "status": "succeeded", "execution": {"storage_volumes": {"web": "retain"}}}
    original = copy.deepcopy(job)
    backend.offline(job, checkpoints.append)
    backend.offline(job, checkpoints.append)
    assert calls == ["deactivate", "stop", "cleanup"] * 2
    assert checkpoints[-1]["cleanup_confirmed"] is True
    assert job == original


def test_runtime_persists_precise_intents_before_mutations(tmp_path):
    backend = DeploymentBackend(Settings(state_dir=tmp_path))
    backend._runtime_preflight = lambda: None
    checkpoints = []

    def gateway(op, job):
        assert checkpoints[-1] == {"phase": "offline_deactivate", "deactivate_intent": True}
        return {"deactivated": True, "release": job["id"]}

    def stop(*args, **kwargs):
        assert checkpoints[-1] == {"phase": "offline_stop", "stop_intent": True}

    def cleanup(job):
        assert checkpoints[-1] == {"phase": "offline_cleanup", "cleanup_intent": True}

    backend.gateway = SimpleNamespace(call=gateway, lock_fd=None)
    backend._stop_owned_containers = stop
    backend.cleanup = cleanup
    backend.offline({"id": "target", "status": "succeeded", "execution": {}}, checkpoints.append)


def test_offline_error_preserves_last_attempted_phase(store):
    job = active(store)
    operation = store.enqueue_offline(job["app_id"], job["id"], "offline")
    runner, backend = worker(store)

    def fail_stop(target, checkpoint):
        checkpoint({"phase": "offline_stop", "stop_intent": True})
        raise RuntimeError("cannot stop")

    backend.offline = fail_stop
    runner.run_once()
    failed = store.get_offline(operation["id"])
    assert failed["phase"] == "offline_stop"
    assert failed["execution"]["stop_intent"] is True
