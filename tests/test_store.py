import concurrent.futures
from dataclasses import replace
import socket
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(Settings(state_dir=tmp_path, port_start=42000, port_end=42100))


def payload(name="demo", app_id=None):
    result = {"repository": "https://github.com/acme/demo", "ref": "main", "subdomain": name}
    if app_id:
        result["appId"] = app_id
    return result


def snapshot(cpu=1, memory=256, names=("web",)):
    return {
        "manifest": {"services": {name: {"resources": {"cpu": cpu, "memoryMiB": memory}} for name in names}},
        "commit": "a" * 40,
    }


def reserved(store, name="demo"):
    job = store.enqueue(payload(name), "key-" + name)
    store.claim(job["id"])
    return store.reserve(job["id"], snapshot())


def test_idempotency_survives_reopening_and_detects_changed_input(store):
    original = store.enqueue(payload(), "same")
    reopened = Store(store.settings)
    assert reopened.enqueue(payload(), "same")["id"] == original["id"]
    with pytest.raises(DeployError) as caught:
        reopened.enqueue(payload("other"), "same")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(store.list_jobs()) == 1


def test_checkpoints_preserve_pinned_commit_and_previous_images(store):
    job = store.enqueue(payload(), "same")
    store.checkpoint(job["id"], execution={"commit": "a" * 40, "images": {"web": "sha256:1"}})
    store.checkpoint(job["id"], execution={"images": {"api": "sha256:2"}, "build_started": True})
    execution = Store(store.settings).get(job["id"])["execution"]
    assert execution["commit"] == "a" * 40
    assert execution["images"] == {"web": "sha256:1", "api": "sha256:2"}


def test_reservation_assigns_distinct_ports_to_all_services_and_skips_bound_port(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        store = Store(Settings(state_dir=tmp_path, port_start=port, port_end=port + 5))
        job = store.enqueue(payload(), "key")
        result = store.reserve(job["id"], snapshot(names=("web", "api")))
        assert len(set(result["execution"]["ports"].values())) == 2
        assert port not in result["execution"]["ports"].values()
        assert store.reserve(job["id"], snapshot(names=("web", "api")))["execution"] == result["execution"]
    finally:
        sock.close()


def test_domain_cannot_be_hijacked_and_app_identity_must_match(store):
    first = reserved(store)
    second = store.enqueue(payload(), "key-second")
    with pytest.raises(DeployError) as caught:
        store.reserve(second["id"], snapshot())
    assert caught.value.code == "DOMAIN_CONFLICT"
    changed = store.enqueue(payload("renamed", first["app_id"]), "key-third")
    with pytest.raises(DeployError):
        store.prepare_context(changed["id"])
    assert len(store.apps()) == 1


def test_concurrent_same_domain_reservation_has_exactly_one_owner(store):
    jobs = [store.enqueue(payload(), f"key-{i}") for i in range(2)]

    def attempt(job):
        try:
            return store.reserve(job["id"], snapshot())["id"]
        except DeployError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(attempt, jobs))
    assert results.count("DOMAIN_CONFLICT") == 1
    assert len(store.apps()) == 1


def test_active_and_candidate_both_count_against_quota(tmp_path):
    store = Store(Settings(state_dir=tmp_path, max_cpu=1.5))
    first = reserved(store)
    store.finish(first["id"])
    next_job = store.enqueue(payload(app_id=first["app_id"]), "second")
    store.prepare_context(next_job["id"])
    with pytest.raises(DeployError) as caught:
        store.reserve(next_job["id"], snapshot())
    assert caught.value.code == "RESOURCE_QUOTA"
    assert store.apps()[0]["active_release_id"] == first["id"]
    assert not store.get(next_job["id"])["execution"].get("ports")


def test_application_memory_limit_rejects_all_services_without_partial_reservation(store):
    job = store.enqueue(payload(), "too-large")
    with pytest.raises(DeployError) as caught:
        store.reserve(job["id"], snapshot(memory=513, names=("web", "api")))
    assert caught.value.code == "RESOURCE_QUOTA"
    assert store.apps() == []
    assert not store.get(job["id"])["execution"].get("ports")
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize("candidate_memory", [192, 193])
def test_application_memory_limit_includes_active_and_candidate_services(store, candidate_memory):
    first = store.enqueue(payload(), "active")
    first = store.reserve(first["id"], snapshot(memory=640))
    store.finish(first["id"])
    candidate = store.enqueue(payload(app_id=first["app_id"]), "candidate")
    requested = snapshot(memory=candidate_memory, names=("web", "api"))
    if candidate_memory == 192:
        result = store.reserve(candidate["id"], requested)
        assert store.reserve(candidate["id"], requested)["execution"] == result["execution"]
    else:
        with pytest.raises(DeployError) as caught:
            store.reserve(candidate["id"], requested)
        assert caught.value.code == "RESOURCE_QUOTA"
        assert not store.get(candidate["id"])["execution"].get("ports")
        with sqlite3.connect(store.settings.database_path) as db:
            assert db.execute("SELECT sum(memory_mib) FROM reservations").fetchone()[0] == 640
    assert store.active_release(first["app_id"])["id"] == first["id"]


def test_application_memory_limit_is_independent_for_each_application(store):
    for name in ("first", "second"):
        job = store.enqueue(payload(name), name)
        result = store.reserve(job["id"], snapshot(memory=1024))
        assert store.reserve(job["id"], snapshot(memory=1024))["execution"] == result["execution"]
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT sum(memory_mib) FROM reservations").fetchone()[0] == 2048


def test_application_memory_limit_rechecks_existing_reservations_after_config_change(store):
    first = reserved(store)
    store.finish(first["id"])
    candidate = store.enqueue(payload(app_id=first["app_id"]), "candidate")
    result = store.reserve(candidate["id"], snapshot(memory=768))
    restricted = Store(replace(store.settings, max_app_memory_mib=768))
    with pytest.raises(DeployError) as caught:
        restricted.reserve(candidate["id"], snapshot(memory=768))
    assert caught.value.code == "RESOURCE_QUOTA"
    assert restricted.get(candidate["id"])["execution"] == result["execution"]
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT sum(memory_mib) FROM reservations").fetchone()[0] == 1024


def test_concurrent_candidates_cannot_exceed_application_memory_limit(store):
    first = store.enqueue(payload(), "active")
    first = store.reserve(first["id"], snapshot(memory=512))
    store.finish(first["id"])
    candidates = [store.enqueue(payload(app_id=first["app_id"]), f"candidate-{i}") for i in range(2)]

    def attempt(job):
        try:
            store.reserve(job["id"], snapshot(memory=384))
            return "reserved"
        except DeployError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(attempt, candidates))
    assert sorted(results) == ["RESOURCE_QUOTA", "reserved"]
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT sum(memory_mib) FROM reservations").fetchone()[0] == 896


def test_finish_switches_atomically_and_only_inactive_cleanup_releases_ports(store):
    first = reserved(store)
    store.finish(first["id"])
    second = store.enqueue(payload(app_id=first["app_id"]), "second")
    second = store.prepare_context(second["id"])
    assert second["execution"]["previous_release"] == first["id"]
    assert second["execution"]["previous_manifest"] == snapshot()["manifest"]
    second = store.reserve(second["id"], snapshot())
    assert second["execution"]["ports"] != first["execution"]["ports"]
    with pytest.raises(DeployError):
        store.release(first["id"])
    store.finish(second["id"])
    store.release(first["id"])
    assert store.get(first["id"])["execution"]["resources_released"] is True
    assert store.apps()[0]["active_release_id"] == second["id"]


def test_failed_cleanup_keeps_ports_and_recovery_blocks_same_app(store):
    first = reserved(store)
    store.finish(first["id"])
    failed = store.enqueue(payload(app_id=first["app_id"]), "failed")
    failed = store.reserve(failed["id"], snapshot())
    store.fail(failed["id"], {"code": "ROLLBACK_FAILED"}, recovery=True)
    next_job = store.enqueue(payload(app_id=first["app_id"]), "next")
    with pytest.raises(DeployError) as caught:
        store.prepare_context(next_job["id"])
    assert caught.value.code == "RECOVERY_REQUIRED"
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 2


def test_queue_prioritizes_running_and_events_are_bounded(store):
    first = store.enqueue(payload("first"), "first")
    second = store.enqueue(payload("second"), "second")
    store.claim(second["id"])
    assert store.next_job()["id"] == second["id"]
    store.event(first["id"], "build", "ok\n" + "x" * 10000)
    event = store.events(first["id"])[0]
    assert "\n" not in event["message"]
    assert len(event["message"]) <= 512
    with pytest.raises(DeployError) as caught:
        store.get("missing")
    assert caught.value.code == "NOT_FOUND"


def test_partial_port_allocation_rolls_back_application_and_every_port(tmp_path):
    store = Store(Settings(state_dir=tmp_path, port_start=42800, port_end=42800))
    job = store.enqueue(payload(), "exhausted")
    with pytest.raises(DeployError) as caught:
        store.reserve(job["id"], snapshot(names=("web", "api")))
    assert caught.value.code == "PORTS_EXHAUSTED"
    assert store.apps() == []
    with sqlite3.connect(store.settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_pinned_commit_cannot_be_changed_by_later_checkpoint(store):
    job = store.enqueue(payload(), "pin")
    store.checkpoint(job["id"], execution={"commit": "a" * 40})
    with pytest.raises(DeployError) as caught:
        store.checkpoint(job["id"], execution={"commit": "b" * 40})
    assert caught.value.code == "PIN_CONFLICT"
    assert store.get(job["id"])["execution"]["commit"] == "a" * 40


def test_active_recovery_selects_only_current_successful_release_and_legacy_rows(store):
    old = reserved(store)
    store.finish(old["id"])
    current = store.enqueue(payload(app_id=old["app_id"]), "new-active")
    current = store.reserve(current["id"], snapshot())
    store.finish(current["id"])
    candidate = store.enqueue(payload(app_id=old["app_id"]), "candidate")
    candidate = store.reserve(candidate["id"], snapshot())
    store.fail(candidate["id"], {"code": "ROLLBACK_FAILED"}, recovery=True)
    assert store.next_active_recovery("boot-a")["id"] == current["id"]
    assert store.active_release(old["app_id"])["id"] == current["id"]
    store.begin_active_recovery(current["id"], "boot-a", now=100)
    store.finish_active_recovery(current["id"], success=True, now=101)
    assert store.next_active_recovery("boot-a", now=150) is None
    assert store.next_active_recovery("boot-b", now=150)["id"] == current["id"]
    for rejected in (old, candidate):
        with pytest.raises(DeployError):
            store.begin_active_recovery(rejected["id"], "boot-a")


def test_active_recovery_failure_backoff_survives_reopen_and_keeps_active_pointer(store):
    active = reserved(store)
    store.finish(active["id"])
    store.begin_active_recovery(active["id"], "boot-a", now=100)
    store.finish_active_recovery(active["id"], success=False, now=300)
    reopened = Store(store.settings)
    assert reopened.next_active_recovery("boot-a", now=359) is None
    assert reopened.next_active_recovery("boot-a", now=360)["id"] == active["id"]
    retry = reopened.begin_active_recovery(active["id"], "boot-a", now=360)
    assert retry["execution"]["active_recovery"]["retry_health"] is True
    failed = reopened.finish_active_recovery(active["id"], success=False, now=400)
    assert failed["execution"]["active_recovery"]["next_at"] == 520
    assert failed["status"] == "succeeded" and failed["error"] is None
    assert reopened.apps()[0]["active_release_id"] == active["id"]
