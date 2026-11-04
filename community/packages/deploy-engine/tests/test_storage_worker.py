import pytest

from awesome_deploy.store import Store
from awesome_deploy.worker import Worker
import test_worker
from test_worker import Crash, payload, worker


@pytest.fixture
def setup(tmp_path):
    return test_worker.setup.__wrapped__(tmp_path)


def enable_storage(setup):
    _, store, calls, source, backend = setup
    prepare = source.prepare

    def storage_source(job, checkpoint):
        result = prepare(job, checkpoint)
        result["manifest"]["services"]["web"]["storage"] = {"name": "images", "mountPath": "/data"}
        return result

    source.prepare = storage_source

    def stop(previous):
        candidates = [job for job in store.list_jobs() if job["status"] == "running"]
        assert candidates[0]["execution"]["storage_handoff_intent"]
        backend.call("stop_previous")

    backend.stop_for_storage_handoff = stop
    backend.quiesce_storage_candidate = lambda job: backend.call("stop_candidate")
    return backend


def releases(setup):
    backend = enable_storage(setup)
    _, store, calls, _, _ = setup
    first = store.enqueue(payload(), "storage-first")
    worker(setup).run_once()
    first = store.get(first["id"])
    assert first["status"] == "succeeded"
    second = store.enqueue(payload(first["app_id"]), "storage-second")
    calls.clear()
    return first, second, backend


def test_storage_update_stops_previous_only_after_build_and_before_candidate(setup):
    first, second, backend = releases(setup)
    _, store, calls, _, _ = setup
    worker(setup).run_once()
    assert store.get(second["id"])["status"] == "succeeded"
    assert calls.index("build") < calls.index("stop_previous") < calls.index("start")
    assert backend.cleaned == [first["id"]]


@pytest.mark.parametrize("failure", ["start", "health", "https", "verify"])
def test_storage_failure_quiesces_and_cleans_before_previous_recovery(setup, failure):
    first, second, backend = releases(setup)
    _, store, calls, _, _ = setup
    backend.fail_at = failure
    worker(setup).run_once()
    assert store.get(second["id"])["status"] == "failed"
    assert calls.index("stop_candidate") < calls.index("cleanup") < calls.index("recover_active")
    assert backend.public == first["id"]
    if failure == "verify":
        assert calls.index("recover_active") < calls.index("rollback")


@pytest.mark.parametrize("crash", ["stop_previous", "start", "health", "publish", "verify"])
def test_storage_interruption_rolls_back_before_any_active_maintenance(setup, crash):
    first, second, backend = releases(setup)
    settings, store, calls, source, _ = setup
    backend.crash_at = crash
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at = None
    backend.boot_id = "reboot"
    calls.clear()
    assert Worker(settings, Store(settings), source, backend).run_once()
    assert calls[:3] == ["stop_candidate", "cleanup", "recover_active"]
    assert "start" not in calls
    assert store.get(second["id"])["status"] == "failed"
    assert backend.public == first["id"]


@pytest.mark.parametrize("failure", ["stop_candidate", "cleanup", "recover_active", "rollback"])
def test_pending_storage_recovery_blocks_maintenance_and_new_jobs(setup, failure):
    first, second, backend = releases(setup)
    settings, store, calls, source, _ = setup
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at = None
    backend.fail_at = failure
    worker(setup).run_once()
    assert store.get(second["id"])["status"] == "recovery_required"
    assert store.next_active_recovery("another-boot", now=10**12) is None
    calls.clear()
    backend.boot_id = "another-boot"
    assert not Worker(settings, Store(settings), source, backend).run_once()
    assert calls == []
    backend.fail_at = None
    candidate = store.enqueue(payload(first["app_id"]), "blocked")
    worker(setup).run_once()
    assert store.get(candidate["id"])["error"]["code"] == "RECOVERY_REQUIRED"
    assert "recover_active" not in calls


def test_crash_after_storage_intent_before_stop_recovers_conservatively(setup, monkeypatch):
    first, second, backend = releases(setup)
    settings, store, calls, source, _ = setup
    original = store.checkpoint

    def checkpoint(job_id, **kwargs):
        result = original(job_id, **kwargs)
        if kwargs.get("execution", {}).get("storage_handoff_intent"):
            raise Crash("intent persisted")
        return result

    monkeypatch.setattr(store, "checkpoint", checkpoint)
    with pytest.raises(Crash):
        worker(setup).run_once()
    assert "stop_previous" not in calls
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["stop_candidate", "cleanup", "recover_active"]
    assert backend.public == first["id"]


@pytest.mark.parametrize(
    "boundary", ["storage_previous_stopped", "storage_candidate_cleaned", "rollback_confirmed"]
)
def test_handoff_checkpoint_crashes_never_restart_candidate_or_overlap_writers(setup, monkeypatch, boundary):
    first, second, backend = releases(setup)
    settings, store, calls, source, _ = setup
    if boundary != "storage_previous_stopped":
        backend.fail_at = "verify"
    checkpoint = store.checkpoint

    def persisted_then_crash(job_id, **kwargs):
        result = checkpoint(job_id, **kwargs)
        if kwargs.get("execution", {}).get(boundary):
            raise Crash(boundary)
        return result

    monkeypatch.setattr(store, "checkpoint", persisted_then_crash)
    with pytest.raises(Crash):
        worker(setup).run_once()
    calls.clear()
    backend.fail_at = None
    backend.boot_id = "new-boot"
    reopened = Store(settings)
    assert Worker(settings, reopened, source, backend).run_once()
    assert "start" not in calls and "build" not in calls
    assert reopened.get(second["id"])["status"] == "failed"
    assert backend.public == first["id"]
    if boundary == "storage_previous_stopped":
        assert calls.index("stop_candidate") < calls.index("cleanup") < calls.index("recover_active")
    else:
        assert calls[0] == "recover_active"


def test_crash_after_actual_cleanup_before_journal_can_repeat_cleanup_safely(setup):
    first, second, backend = releases(setup)
    settings, store, calls, source, _ = setup
    cleanup = backend.cleanup

    def clean_then_crash(job):
        cleanup(job)
        raise Crash("cleanup completed but not checkpointed")

    backend.cleanup = clean_then_crash
    backend.fail_at = "health"
    with pytest.raises(Crash):
        worker(setup).run_once()
    assert "recover_active" not in calls
    backend.cleanup = cleanup
    backend.fail_at = None
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["stop_candidate", "cleanup", "recover_active"]
    assert backend.public == first["id"]


def test_failed_build_keeps_old_storage_service_running(setup):
    first, second, backend = releases(setup)
    _, store, calls, _, _ = setup
    backend.fail_at = "build"
    worker(setup).run_once()
    assert "stop_previous" not in calls and "stop_candidate" not in calls
    assert not store.get(second["id"])["execution"].get("storage_handoff_intent")
    assert backend.public == first["id"]


def test_quota_prepare_failure_does_not_stop_previous(setup):
    from awesome_deploy.errors import DeployError

    first, second, backend = releases(setup)
    _, store, calls, _, _ = setup

    def prepare_storage(job, checkpoint):
        backend.call("prepare_storage")
        raise DeployError("QUOTA_BUDGET", "No quota capacity")

    backend.prepare_storage = prepare_storage
    worker(setup).run_once()
    assert "prepare_storage" in calls
    assert "stop_previous" not in calls
    assert store.get(second["id"])["status"] == "failed"
    assert store.active_release(first["app_id"])["id"] == first["id"]
