import fcntl
import threading

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store
from awesome_deploy.worker import Worker


def payload(app_id=None):
    value = {"repository": "https://github.com/acme/demo", "ref": "main", "subdomain": "demo"}
    if app_id:
        value["appId"] = app_id
    return value


class Crash(BaseException):
    pass


class Source:
    def __init__(self, calls):
        self.calls = calls
        self.fail = False

    def prepare(self, job, checkpoint):
        self.calls.append("prepare")
        checkpoint({"commit": job["execution"].get("commit", "a" * 40)})
        if self.fail:
            raise DeployError("INVALID_MANIFEST", "Manifest is invalid.")
        return {
            "commit": job["execution"].get("commit", "a" * 40),
            "source_dir": "/fake/source",
            "manifest": {"services": {"web": {"resources": {"cpu": 0.5, "memoryMiB": 128}}}},
        }


class Backend:
    def __init__(self, calls):
        self.calls = calls
        self.fail_at = None
        self.crash_at = None
        self.cleaned = []
        self.public = None
        self.previous = None
        self.observer = None
        self.boot_id = "test-boot"
        self.recovered = []
        self.restarted = []

    def call(self, name):
        self.calls.append(name)
        if name == self.crash_at:
            raise Crash(name)
        if name == self.fail_at:
            raise RuntimeError("SECRET_TOKEN=raw-value")

    def recovery_boot_id(self):
        return self.boot_id

    def recover_active(self, job, checkpoint):
        assert job["status"] == "succeeded"
        self.call("recover_active")
        self.recovered.append(job["id"])
        return {"restarted": list(self.restarted)}

    def freeze(self, snapshot, job, checkpoint):
        self.call("freeze")
        return {**snapshot, "frozen_value": "durable"}

    def build(self, job, checkpoint):
        self.call("build")
        checkpoint({"images": {"web": "sha256:abc"}})
        return {"web": "sha256:abc"}

    def start(self, job, checkpoint):
        assert job["snapshot"]["frozen_value"] == "durable"
        assert job["execution"]["images"] == {"web": "sha256:abc"}
        self.call("start")

    def health(self, job):
        self.call("health")

    def ensure_https(self, job):
        self.call("https")

    def publish(self, job):
        assert job["execution"]["publish_intent"] is True
        self.previous = self.public
        self.public = job["id"]
        if self.observer:
            self.observer(job)
        self.call("publish")

    def verify(self, job):
        self.call("verify")

    def rollback(self, job):
        self.call("rollback")
        self.public = job["execution"].get("previous_release")

    def cleanup(self, job):
        self.call("cleanup")
        self.cleaned.append(job["id"])


@pytest.fixture
def setup(tmp_path):
    settings = Settings(state_dir=tmp_path, port_start=42500, port_end=42600)
    store = Store(settings)
    calls = []
    source, backend = Source(calls), Backend(calls)
    return settings, store, calls, source, backend


def worker(setup):
    settings, store, _, source, backend = setup
    return Worker(settings, store, source, backend)


def test_active_recovery_persists_forward_journal_even_when_recovery_fails(setup):
    _, store, _, _, backend = setup
    first = store.enqueue(payload(), "forward-journal")
    assert worker(setup).run_once()
    checkpoint_seen = []

    def recover(job, checkpoint):
        checkpoint({"port_forwards": {"web": {"confirmed": False}}})
        checkpoint_seen.append(store.get(job["id"])["execution"]["port_forwards"])
        raise RuntimeError("recovery interrupted")

    backend.recover_active = recover
    worker(setup)._recover_active(store.get(first["id"]))
    current = store.get(first["id"])
    assert checkpoint_seen == [{"web": {"confirmed": False}}]
    assert current["execution"]["port_forwards"] == checkpoint_seen[0]
    assert current["execution"]["active_recovery"]["outcome"] == "failed"
    assert current["status"] == "succeeded"


def test_happy_path_persists_before_publish_and_survives_duplicate_request(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "response-loss")
    backend.observer = lambda job: assert_persisted_publish(settings, job)
    assert worker(setup).run_once()
    assert calls == ["prepare", "freeze", "build", "start", "health", "https", "publish", "verify"]
    assert Store(settings).get(first["id"])["status"] == "succeeded"
    assert store.enqueue(payload(), "response-loss")["id"] == first["id"]
    assert not worker(setup).run_once()


def assert_persisted_publish(settings, job):
    state = Store(settings).get(job["id"])["execution"]
    assert state["publish_intent"] is True
    assert "previous_release" in state


def test_failed_candidate_rolls_back_before_cleanup_and_keeps_old_active(setup):
    _, store, calls, _, backend = setup
    first = store.enqueue(payload(), "first")
    worker(setup).run_once()
    first = store.get(first["id"])
    second = store.enqueue(payload(first["app_id"]), "second")
    calls.clear()
    backend.fail_at = "verify"
    worker(setup).run_once()
    assert calls[-2:] == ["rollback", "cleanup"]
    assert backend.public == first["id"]
    assert store.apps()[0]["active_release_id"] == first["id"]
    assert store.get(second["id"])["status"] == "failed"
    assert store.get(second["id"])["execution"]["resources_released"]
    assert "raw-value" not in str(store.events(second["id"]))
    assert "raw-value" not in str(store.get(second["id"])["error"])


def test_interrupted_publish_is_rolled_back_on_restart_not_republished(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "first")
    worker(setup).run_once()
    app_id = store.get(first["id"])["app_id"]
    second = store.enqueue(payload(app_id), "second")
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    assert Store(settings).get(second["id"])["status"] == "running"
    backend.crash_at = None
    calls.clear()
    assert Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["recover_active", "rollback", "cleanup"]
    assert backend.public == first["id"]
    assert store.get(second["id"])["error"]["code"] == "INTERRUPTED_PUBLISH"


def test_failed_rollback_preserves_candidate_and_blocks_future_same_app(setup):
    _, store, calls, _, backend = setup
    job = store.enqueue(payload(), "first")
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at, backend.fail_at = None, "rollback"
    calls.clear()
    worker(setup).run_once()
    failed = store.get(job["id"])
    assert calls == ["rollback"]
    assert failed["status"] == "recovery_required"
    assert not failed["execution"].get("resources_released")
    next_job = store.enqueue(payload(failed["app_id"]), "next")
    worker(setup).run_once()
    assert store.get(next_job["id"])["error"]["code"] == "RECOVERY_REQUIRED"
    # The rejected new job can discard its own temporary files, never the
    # earlier candidate whose gateway rollback remains unconfirmed.
    assert calls == ["rollback", "cleanup"]
    assert backend.cleaned == [next_job["id"]]
    assert not store.get(job["id"])["execution"].get("resources_released")


def test_failed_cleanup_keeps_allocations_for_recovery(setup):
    _, store, calls, _, backend = setup
    job = store.enqueue(payload(), "first")
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at, backend.fail_at = None, "cleanup"
    calls.clear()
    worker(setup).run_once()
    assert calls == ["rollback", "cleanup"]
    assert store.get(job["id"])["status"] == "recovery_required"
    assert not store.get(job["id"])["execution"].get("resources_released")


def test_preallocation_failure_cleans_temporary_source_without_reservations(setup):
    _, store, calls, source, _ = setup
    job = store.enqueue(payload(), "first")
    source.fail = True
    worker(setup).run_once()
    assert calls == ["prepare", "cleanup"]
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["execution"]["resources_released"]
    assert store.apps() == []


def test_restart_before_publish_reuses_frozen_snapshot_commit_and_image(setup):
    settings, store, calls, source, backend = setup
    job = store.enqueue(payload(), "first")
    backend.crash_at = "start"
    with pytest.raises(Crash):
        worker(setup).run_once()
    persisted = Store(settings).get(job["id"])
    assert persisted["execution"]["commit"] == "a" * 40
    backend.crash_at = None
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert "prepare" not in calls
    assert "freeze" not in calls
    assert "build" not in calls
    assert store.get(job["id"])["status"] == "succeeded"


def test_old_cleanup_failure_does_not_fail_new_active_release(setup):
    _, store, _, _, backend = setup
    first = store.enqueue(payload(), "first")
    worker(setup).run_once()
    second = store.enqueue(payload(store.get(first["id"])["app_id"]), "second")
    backend.fail_at = "cleanup"
    worker(setup).run_once()
    assert store.get(second["id"])["status"] == "succeeded"
    assert store.apps()[0]["active_release_id"] == second["id"]
    assert not store.get(first["id"])["execution"].get("resources_released")
    assert any("cleanup" in event["message"].lower() for event in store.events(second["id"]))


def test_global_flock_covers_whole_execution_and_is_available_to_children(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "first")
    entered, release = threading.Event(), threading.Event()
    original = source.prepare

    def slow_prepare(job, checkpoint):
        assert backend.lock_fd is not None
        fcntl.fcntl(backend.lock_fd, fcntl.F_GETFD)
        entered.set()
        assert release.wait(timeout=5)
        return original(job, checkpoint)

    source.prepare = slow_prepare
    thread = threading.Thread(target=lambda: worker(setup).run_once())
    thread.start()
    assert entered.wait(timeout=5)
    try:
        independent = Backend([])
        assert not Worker(settings, Store(settings), Source([]), independent).run_once()
        assert store.get(first["id"])["phase"] != "complete"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert store.get(first["id"])["status"] == "succeeded"
    assert backend.lock_fd is None


def test_inherited_flock_prevents_takeover_after_worker_crash(setup):
    import subprocess
    import sys

    settings, store, _, source, backend = setup
    store.enqueue(payload(), "first")
    child = None

    def interrupted_source(job, checkpoint):
        nonlocal child
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(source.lock_fd,),
        )
        raise Crash("worker died while a child is running")

    source.prepare = interrupted_source
    try:
        with pytest.raises(Crash):
            worker(setup).run_once()
        assert child.poll() is None
        assert not Worker(settings, store, Source([]), Backend([])).run_once()
    finally:
        if child:
            child.communicate(timeout=5)
    assert Worker(settings, store, Source([]), backend).run_once()


def test_crash_after_commit_pin_preserves_commit_for_source_retry(setup):
    settings, store, _, source, backend = setup
    job = store.enqueue(payload(), "first")

    def interrupted_source(job, checkpoint):
        checkpoint({"commit": "b" * 40})
        raise Crash("after pin")

    source.prepare = interrupted_source
    with pytest.raises(Crash):
        worker(setup).run_once()
    new_source = Source([])
    Worker(settings, Store(settings), new_source, backend).run_once()
    assert store.get(job["id"])["snapshot"]["commit"] == "b" * 40
    assert store.get(job["id"])["execution"]["commit"] == "b" * 40


def test_crash_after_failure_release_commit_never_restarts_candidate(setup, monkeypatch):
    import sqlite3

    settings, store, calls, source, backend = setup
    job = store.enqueue(payload(), "failure-release-crash")
    backend.fail_at = "health"
    release = store.release

    def release_then_crash(job_id):
        result = release(job_id)
        raise Crash("after resources were released")
        return result

    monkeypatch.setattr(store, "release", release_then_crash)
    with pytest.raises(Crash):
        worker(setup).run_once()
    calls.clear()
    reopened = Store(settings)
    Worker(settings, reopened, source, backend).run_once()
    assert calls == []
    assert reopened.get(job["id"])["status"] == "failed"
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_legacy_running_job_with_released_resources_is_failed_without_restarting(setup):
    settings, store, calls, source, backend = setup
    job = store.enqueue(payload(), "legacy-released")
    backend.crash_at = "start"
    with pytest.raises(Crash):
        worker(setup).run_once()
    store.release(job["id"])
    backend.crash_at = None
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert calls == []
    assert store.get(job["id"])["status"] == "failed"


def test_crash_after_finish_commit_recovers_retirement_without_republishing(setup, monkeypatch):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "retirement-first")
    worker(setup).run_once()
    second = store.enqueue(payload(store.get(first["id"])["app_id"]), "retirement-second")
    finish = store.finish

    def finish_then_crash(job_id):
        finish(job_id)
        raise Crash("after successful activation commit")

    monkeypatch.setattr(store, "finish", finish_then_crash)
    with pytest.raises(Crash):
        worker(setup).run_once()
    assert store.get(second["id"])["status"] == "succeeded"
    assert not store.get(first["id"])["execution"].get("resources_released")
    calls.clear()
    reopened = Store(settings)
    assert Worker(settings, reopened, source, backend).run_once()
    assert calls == ["recover_active"]
    assert Worker(settings, reopened, source, backend).run_once()
    assert calls == ["recover_active", "cleanup"]
    assert reopened.get(first["id"])["execution"]["resources_released"]
    assert reopened.apps()[0]["active_release_id"] == second["id"]
    assert not Worker(settings, reopened, source, backend).run_once()


def test_retirement_failures_back_off_durably_and_retry_when_due(setup, monkeypatch):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "backoff-first")
    worker(setup).run_once()
    second = store.enqueue(payload(store.get(first["id"])["app_id"]), "backoff-second")
    backend.fail_at = "cleanup"
    worker(setup).run_once()
    state = store.get(second["id"])["execution"]
    assert state["retirement_pending"] == [first["id"]]
    assert state["retirement_retry_at"] > 0
    calls.clear()
    assert not Worker(settings, Store(settings), source, backend).run_once()
    assert calls == []
    monkeypatch.setattr("awesome_deploy.store.time.time", lambda: state["retirement_retry_at"] + 1)
    backend.fail_at = None
    assert Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["recover_active", "cleanup"]
    assert store.get(second["id"])["execution"]["retirement_pending"] == []
    assert store.get(first["id"])["execution"]["resources_released"]


def test_new_success_inherits_older_retirement_backlog(setup):
    _, store, calls, _, backend = setup
    first = store.enqueue(payload(), "chain-first")
    worker(setup).run_once()
    app_id = store.get(first["id"])["app_id"]
    second = store.enqueue(payload(app_id), "chain-second")
    backend.fail_at = "cleanup"
    worker(setup).run_once()
    third = store.enqueue(payload(app_id), "chain-third")
    backend.fail_at = None
    calls.clear()
    worker(setup).run_once()
    assert store.get(third["id"])["status"] == "succeeded"
    assert store.apps()[0]["active_release_id"] == third["id"]
    assert store.get(first["id"])["execution"]["resources_released"]
    assert store.get(second["id"])["execution"]["resources_released"]


def test_boot_recovery_only_restores_existing_active_and_preserves_routes(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "restart-active")
    worker(setup).run_once()
    first = store.get(first["id"])
    backend.boot_id = "new-boot"
    backend.restarted = ["web"]
    calls.clear()
    assert Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["recover_active"]
    assert backend.recovered == [first["id"]]
    assert backend.public == first["id"]
    assert store.get(first["id"])["status"] == "succeeded"
    assert not worker(setup).run_once()


def test_recovery_failure_is_redacted_and_backs_off_across_worker_restart(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "recovery-fail")
    worker(setup).run_once()
    backend.boot_id = "new-boot"
    backend.fail_at = "recover_active"
    calls.clear()
    assert worker(setup).run_once()
    assert calls == ["recover_active"]
    assert not Worker(settings, Store(settings), source, backend).run_once()
    active = store.get(first["id"])
    assert active["status"] == "succeeded" and active["error"] is None
    assert "raw-value" not in str(active) + str(store.events(first["id"]))
    assert backend.cleaned == []


def test_interrupted_publish_recovers_only_previous_active_before_rollback(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "recover-before-rollback-first")
    worker(setup).run_once()
    second = store.enqueue(payload(store.get(first["id"])["app_id"]), "recover-before-rollback-second")
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at = None
    backend.boot_id = "reboot"
    backend.restarted = ["web"]
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["recover_active", "rollback", "cleanup"]
    assert backend.recovered == [first["id"]]
    assert second["id"] not in backend.recovered
    assert backend.public == first["id"]


def test_failed_previous_recovery_preserves_ambiguous_candidate_resources(setup):
    settings, store, calls, source, backend = setup
    first = store.enqueue(payload(), "previous-first")
    worker(setup).run_once()
    second = store.enqueue(payload(store.get(first["id"])["app_id"]), "previous-second")
    backend.crash_at = "publish"
    with pytest.raises(Crash):
        worker(setup).run_once()
    backend.crash_at = None
    backend.fail_at = "recover_active"
    calls.clear()
    Worker(settings, Store(settings), source, backend).run_once()
    assert calls == ["recover_active"]
    assert store.get(second["id"])["status"] == "recovery_required"
    assert backend.public == second["id"]
    assert not store.get(second["id"])["execution"].get("resources_released")
    assert store.get(first["id"])["status"] == "succeeded"


@pytest.mark.parametrize("method", ["begin_active_recovery", "finish_active_recovery"])
def test_post_finish_recovery_metadata_failure_cannot_fail_active_release(setup, monkeypatch, method):
    _, store, calls, _, backend = setup
    submitted = store.enqueue(payload(), "successful-then-marker-fails")
    monkeypatch.setattr(
        store, method, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("marker failed"))
    )
    worker(setup).run_once()
    active = store.get(submitted["id"])
    assert active["status"] == "succeeded"
    assert not active["execution"].get("failure_intent")
    assert store.apps()[0]["active_release_id"] == active["id"]
    assert backend.public == active["id"]
    assert "rollback" not in calls and "cleanup" not in calls


@pytest.mark.parametrize("running", [False, True])
def test_fresh_boot_attempts_all_active_before_long_candidate_work(setup, running):
    settings, store, calls, source, backend = setup
    active_ids = []
    for name in ["one", "two"]:
        submitted = store.enqueue({**payload(), "subdomain": name}, name)
        worker(setup).run_once()
        active_ids.append(submitted["id"])
    candidate = store.enqueue(payload(store.get(active_ids[0])["app_id"]) | {"subdomain": "one"}, "update")
    if running:
        backend.crash_at = "build"
        with pytest.raises(Crash):
            worker(setup).run_once()
        backend.crash_at = None
    backend.boot_id = "new-host-boot"
    calls.clear()
    restarted = Worker(settings, Store(settings), source, backend)
    assert restarted.run_once()
    assert calls == ["recover_active"]
    assert restarted.run_once()
    assert calls == ["recover_active", "recover_active"]
    assert set(backend.recovered) == set(active_ids)
    assert store.get(candidate["id"])["status"] == ("running" if running else "queued")
    assert restarted.run_once()
    assert "build" in calls[2:]
    assert store.get(candidate["id"])["status"] == "succeeded"


def test_same_boot_due_recovery_does_not_starve_queued_work(setup, monkeypatch):
    _, store, calls, _, _ = setup
    submitted = store.enqueue(payload(), "old-active")
    worker(setup).run_once()
    active = store.get(submitted["id"])
    candidate = store.enqueue(payload(active["app_id"]), "queued-update")
    next_at = active["execution"]["active_recovery"]["next_at"]
    monkeypatch.setattr("awesome_deploy.store.time.time", lambda: next_at + 1)
    calls.clear()
    worker(setup).run_once()
    assert calls[0:2] == ["recover_active", "prepare"]
    assert store.get(candidate["id"])["status"] == "succeeded"


def test_previous_recovery_requires_current_database_active_pointer(setup):
    _, store, calls, _, backend = setup
    first = store.enqueue(payload(), "pointer-first")
    worker(setup).run_once()
    app_id = store.get(first["id"])["app_id"]
    second = store.enqueue(payload(app_id), "pointer-second")
    worker(setup).run_once()
    calls.clear()
    stale = {"app_id": app_id, "execution": {"previous_release": first["id"]}}
    with pytest.raises(DeployError, match="no longer active"):
        worker(setup)._recover_previous_for_rollback(stale)
    assert calls == []
    assert backend.public == second["id"]


def test_fresh_boot_failed_active_does_not_block_other_active_or_queue(setup):
    _, store, calls, _, backend = setup
    for name in ["one", "two"]:
        store.enqueue({**payload(), "subdomain": name}, name)
        worker(setup).run_once()
    candidate = store.enqueue({**payload(), "subdomain": "three"}, "third")
    backend.boot_id = "reboot"
    backend.fail_at = "recover_active"
    calls.clear()
    assert worker(setup).run_once()
    assert calls == ["recover_active"]
    backend.fail_at = None
    assert worker(setup).run_once()
    assert calls == ["recover_active", "recover_active"]
    assert worker(setup).run_once()
    assert store.get(candidate["id"])["status"] == "succeeded"


def test_fresh_boot_attempts_all_active_before_retirement(setup):
    _, store, calls, _, backend = setup
    first = store.enqueue(payload(), "retire-boot-first")
    worker(setup).run_once()
    current = store.enqueue(payload(store.get(first["id"])["app_id"]), "retire-boot-current")
    backend.fail_at = "cleanup"
    worker(setup).run_once()
    backend.fail_at = None
    store.enqueue({**payload(), "subdomain": "other"}, "retire-boot-other")
    worker(setup).run_once()
    store.checkpoint(current["id"], execution={"retirement_retry_at": 0})
    backend.boot_id = "new-boot"
    calls.clear()
    assert worker(setup).run_once()
    assert calls == ["recover_active"]
    assert worker(setup).run_once()
    assert calls == ["recover_active", "recover_active"]
    assert worker(setup).run_once()
    assert calls == ["recover_active", "recover_active", "cleanup"]
