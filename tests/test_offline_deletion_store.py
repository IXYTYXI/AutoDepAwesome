import copy
import sqlite3
import uuid

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.storage import ManagedStorage
from awesome_deploy.store import Store


REPO = "https://github.com/acme/demo"
SPEC = {"name": "images", "mountPath": "/data"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_available", staticmethod(lambda port: True))
    return Store(Settings(state_dir=tmp_path, port_start=42500, port_end=42900))


def payload(domain="demo", app_id=None, repository=REPO):
    data = {"repository": repository, "ref": "main", "subdomain": domain}
    if app_id is not None:
        data["appId"] = app_id
    return data


def snapshot(domain="demo", *, storage=True):
    service = {"resources": {"cpu": 0.5, "memoryMiB": 128},
               "secretRefs": {"MASTER_KEY": "MASTER_KEY"}}
    if storage:
        service["storage"] = copy.deepcopy(SPEC)
    return {"repository": REPO, "commit": "a" * 40,
            "manifest": {"app": {"subdomain": domain}, "services": {"web": service}}}


def active(store, domain="demo", app_id=None):
    job = store.enqueue(payload(domain, app_id), uuid.uuid4().hex)
    store.claim(job["id"])
    store.prepare_context(job["id"])
    job = store.reserve(job["id"], snapshot(domain))
    name, _ = ManagedStorage.identity(job["app_id"], "web", SPEC)
    expected = {"name": name, "created_at": "2026-10-15T00:00:00Z", "instance": "a" * 32}
    previous = job["execution"].get("previous_release")
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": expected}})
    job = store.finish(job["id"])
    if previous:
        store.release(previous)
        job = store.checkpoint(job["id"], execution={"retirement_pending": [], "previous_retired": True})
    return job


def missing_secret(store, domain="demo", app_id=None, *, assigned=False, phase="freeze"):
    job = store.enqueue(payload(domain, app_id), uuid.uuid4().hex)
    if assigned:
        store.prepare_context(job["id"])
    store.checkpoint(job["id"], phase=phase, snapshot=snapshot(domain))
    return store.fail(job["id"], {"code": "SECRET_NOT_FOUND"})


def request_delete(store, job, key="delete"):
    return store.enqueue_offline(job["app_id"], job["id"], key, data_policy="delete",
                                 delete_confirmation="删除 " + job["input"]["subdomain"])


def complete(store, job, *, policy="retain", key=None):
    key = key or uuid.uuid4().hex
    if policy == "delete":
        operation = request_delete(store, job, key)
    else:
        operation = store.enqueue_offline(job["app_id"], job["id"], key)
    store.claim_offline(operation["id"])
    state = {"gateway_deactivated": True, "containers_stopped": True, "cleanup_confirmed": True}
    if policy == "delete":
        state["data_deleted"] = True
    store.checkpoint_offline(operation["id"], state)
    return store.finish_offline(operation["id"])


def history(store):
    first = active(store, "old")
    failed = missing_secret(store, "old")
    complete(store, first)
    current = active(store, "new", first["app_id"])
    return first, current, failed


def test_default_retain_keeps_existing_inheritance_and_never_tombstones(store):
    job = active(store)
    operation = complete(store, job)
    assert operation["execution"]["data_policy"] == "retain"
    assert not store.get(job["id"])["execution"].get("data_deleted")
    queued = store.enqueue(payload("other", job["app_id"]), "new")
    prepared = store.prepare_context(queued["id"])
    assert prepared["execution"]["storage_volumes"] == job["execution"]["storage_volumes"]


@pytest.mark.parametrize("confirmation", [None, "删除demo", "删除 other", "删除 demo ", "保留 demo"])
def test_delete_requires_exact_current_domain_confirmation_before_enqueue(store, confirmation):
    job = active(store)
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job["app_id"], job["id"], "delete", data_policy="delete",
                              delete_confirmation=confirmation)
    assert error.value.code == "OFFLINE_DELETE_CONFIRMATION"
    assert store.list_offline_operations() == []
    assert store.active_release(job["app_id"])["id"] == job["id"]


def test_metadata_freezes_all_history_scopes_failed_forms_and_deduplicated_volume(store):
    first, current, failed = history(store)
    operation = request_delete(store, current)
    metadata = operation["execution"]["deletion_metadata"]
    assert metadata["app_id"] == first["app_id"]
    assert set(metadata["job_ids"]) == {first["id"], current["id"], failed["id"]}
    assert metadata["scopes"] == [{"repository": REPO, "subdomain": "new"},
                                   {"repository": REPO, "subdomain": "old"}]
    assert metadata["volumes"] == [{"service": "web", "spec": SPEC,
                                     "expected": first["execution"]["storage_volumes"]["web"]}]


@pytest.mark.parametrize("status", ["queued", "running", "recovery_required"])
def test_pending_unassigned_job_in_historical_scope_blocks_delete(store, status):
    _, current, _ = history(store)
    pending = store.enqueue(payload("old"), "pending")
    if status == "running":
        store.claim(pending["id"])
    elif status == "recovery_required":
        store.fail(pending["id"], {"code": "FAILED"}, recovery=True)
    with pytest.raises(DeployError) as error:
        request_delete(store, current)
    assert error.value.code in {"APP_BUSY", "OFFLINE_DATA_CONFLICT"}
    assert store.active_release(current["app_id"])["id"] == current["id"]


def test_another_confirmed_app_using_historical_scope_blocks_deletion(store):
    _, current, _ = history(store)
    other = active(store, "old")
    with pytest.raises(DeployError) as error:
        request_delete(store, current)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"
    assert store.active_release(other["app_id"])["id"] == other["id"]


def test_pending_delete_protects_historical_domains_and_secret_write_scope(store):
    _, current, _ = history(store)
    request_delete(store, current)
    for repository in (REPO, "https://github.com/acme/other"):
        with pytest.raises(DeployError) as error:
            store.enqueue(payload("old", repository=repository), uuid.uuid4().hex)
        assert error.value.code == "APP_OFFLINE_BUSY"
    with pytest.raises(DeployError) as error, store.secret_write_scope(REPO, "old"):
        pytest.fail("A pending scope deletion admitted a secret writer")
    assert error.value.code == "SECRET_CONFIGURATION_BUSY"
    with store.secret_write_scope(REPO, "safe"):
        with sqlite3.connect(store.settings.database_path, timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")


@pytest.mark.parametrize("changed", ["name", "created_at", "instance", "extra", "missing"])
def test_invalid_persisted_volume_identity_never_becomes_a_deletion_plan(store, changed):
    job = active(store)
    volumes = copy.deepcopy(job["execution"]["storage_volumes"])
    if changed == "missing":
        volumes = {}
    elif changed == "extra":
        volumes["web"]["extra"] = "not allowed"
    else:
        volumes["web"][changed] = {"name": "foreign", "created_at": 7, "instance": "wrong"}[changed]
    store.checkpoint(job["id"], execution={"storage_volumes": volumes})
    with pytest.raises(DeployError) as error:
        request_delete(store, job)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"


def test_same_canonical_volume_with_different_instance_is_a_conflict(store):
    first = active(store)
    current = active(store, app_id=first["app_id"])
    changed = copy.deepcopy(current["execution"]["storage_volumes"])
    changed["web"]["instance"] = "b" * 32
    store.checkpoint(current["id"], execution={"storage_volumes": changed})
    with pytest.raises(DeployError) as error:
        request_delete(store, current)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"


@pytest.mark.parametrize("phase", ["source", "freeze", "build", "built"])
def test_pre_start_failed_declaration_without_volume_can_be_ignored(store, phase):
    current = active(store)
    failed = missing_secret(store, app_id=current["app_id"], assigned=True, phase=phase)
    operation = request_delete(store, current)
    assert failed["id"] in operation["execution"]["deletion_metadata"]["job_ids"]
    assert len(operation["execution"]["deletion_metadata"]["volumes"]) == 1


@pytest.mark.parametrize("phase", ["start", "health", "verify"])
def test_start_or_later_missing_volume_evidence_is_not_assumed_absent(store, phase):
    current = active(store)
    missing_secret(store, app_id=current["app_id"], assigned=True, phase=phase)
    with pytest.raises(DeployError) as error:
        request_delete(store, current)
    assert error.value.code == "OFFLINE_DATA_CONFLICT"


def test_idempotency_includes_policy_and_deletion_plan_is_immutable(store):
    job = active(store)
    operation = request_delete(store, job)
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job["app_id"], job["id"], "delete")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    store.claim_offline(operation["id"])
    changed = copy.deepcopy(operation["execution"]["deletion_metadata"])
    changed["job_ids"].append("not-selected")
    with pytest.raises(DeployError):
        store.checkpoint_offline(operation["id"], {"deletion_metadata": changed})
    store.fail_offline(operation["id"], {"code": "INTERRUPTED"})
    retried = request_delete(store, job)
    assert retried["id"] == operation["id"]
    assert retried["execution"]["deletion_metadata"] == operation["execution"]["deletion_metadata"]


def test_worker_file_inventory_can_only_be_frozen_once(store):
    job = active(store)
    operation = request_delete(store, job)
    store.claim_offline(operation["id"])
    plan = {"scopes": [{"identity": "original-files"}], "volumes": []}
    first = store.checkpoint_offline(operation["id"], {"deletion_plan": plan})
    assert store.checkpoint_offline(operation["id"], {"deletion_plan": plan})["execution"] == first["execution"]
    for altered in ({"scopes": [], "volumes": []}, None):
        with pytest.raises(DeployError) as error:
            store.checkpoint_offline(operation["id"], {"deletion_plan": altered})
        assert error.value.code == "OFFLINE_STATE"
    assert store.get_offline(operation["id"])["execution"]["deletion_plan"] == plan


def test_retry_never_rescans_or_expands_frozen_metadata(store, monkeypatch):
    job = active(store)
    operation = request_delete(store, job)
    store.claim_offline(operation["id"])
    store.fail_offline(operation["id"], {"code": "INTERRUPTED"})
    monkeypatch.setattr(store, "_deletion_metadata", lambda *args: pytest.fail("Retry expanded its deletion scope"))
    assert request_delete(store, job)["execution"] == operation["execution"]


def test_successful_delete_replay_keeps_original_confirmation_after_domain_release(store):
    job = active(store)
    completed = complete(store, job, policy="delete", key="delete")
    assert request_delete(store, job) == completed


def test_same_scope_orphan_volume_never_allows_fresh_key_after_reset(store):
    old = active(store)
    complete(store, old, policy="delete")
    orphan = missing_secret(store)
    store.checkpoint(orphan["id"], execution={"storage_volumes": old["execution"]["storage_volumes"]})
    fresh = missing_secret(store, app_id=old["app_id"], assigned=True)
    with store.secret_configuration_scope(fresh["id"]) as (_, can_generate):
        assert can_generate is False


def test_delete_completion_requires_data_confirmation_and_tombstones_exact_plan(store):
    first, current, failed = history(store)
    operation = request_delete(store, current)
    store.claim_offline(operation["id"])
    store.checkpoint_offline(operation["id"], {
        "gateway_deactivated": True, "containers_stopped": True, "cleanup_confirmed": True,
    })
    with pytest.raises(DeployError) as error:
        store.finish_offline(operation["id"])
    assert error.value.code == "OFFLINE_STATE"
    assert store.active_release(current["app_id"])["id"] == current["id"]
    store.checkpoint_offline(operation["id"], {"data_deleted": True})
    store.finish_offline(operation["id"])
    for job in (first, current, failed):
        after = store.get(job["id"])
        assert after["execution"]["data_deleted"] is True
        assert after["execution"]["data_reset_operation"] == operation["id"]
        assert after["snapshot"] == job["snapshot"]
    assert store.active_release(current["app_id"]) is None
    with store._connection() as db:
        assert store._retained_release(db, current["app_id"])["id"] == current["id"]


@pytest.mark.parametrize("prepare", [False, True])
def test_cleared_history_never_supplies_manifest_or_volume_to_new_generation(store, prepare):
    job = active(store)
    complete(store, job, policy="delete")
    queued = store.enqueue(payload("fresh", job["app_id"]), "fresh")
    if prepare:
        prepared = store.prepare_context(queued["id"])
        assert "previous_manifest" not in prepared["execution"]
        assert "storage_volumes" not in prepared["execution"]
    reserved = store.reserve(queued["id"], snapshot("fresh", storage=False))
    assert "previous_manifest" not in reserved["execution"]
    assert "storage_volumes" not in reserved["execution"]


def test_old_failed_secret_form_is_sealed_but_fresh_empty_generation_can_generate(store):
    old = missing_secret(store)
    job = active(store)
    complete(store, job, policy="delete")
    with pytest.raises(DeployError) as error, store.secret_configuration_scope(old["id"]):
        pytest.fail("Old failed form was reused after deletion")
    assert error.value.code == "SECRET_CONFIGURATION_NOT_AVAILABLE"
    fresh = missing_secret(store, app_id=job["app_id"], assigned=True)
    with store.secret_configuration_scope(fresh["id"]) as (_, can_generate):
        assert can_generate is True


@pytest.mark.parametrize("evidence", ["volume", "succeeded"])
def test_new_generation_data_or_success_history_prevents_random_key_replacement(store, evidence):
    old = active(store)
    complete(store, old, policy="delete")
    if evidence == "succeeded":
        active(store, app_id=old["app_id"])
    else:
        failed = missing_secret(store, app_id=old["app_id"], assigned=True)
        store.checkpoint(failed["id"], execution={"storage_volumes": old["execution"]["storage_volumes"]})
    fresh = missing_secret(store, app_id=old["app_id"], assigned=True)
    with store.secret_configuration_scope(fresh["id"]) as (_, can_generate):
        assert can_generate is False


def test_cleared_former_owner_does_not_block_a_new_application_secret_scope(store):
    old = active(store)
    complete(store, old, policy="delete")
    fresh = missing_secret(store)
    with store.secret_configuration_scope(fresh["id"]) as (_, can_generate):
        assert can_generate is True
    queued = store.enqueue(payload(), "different-app")
    frozen = snapshot()
    frozen["secret_versions"] = {"web": {"MASTER_KEY": "b" * 32}}
    reserved = store.reserve(queued["id"], frozen)
    assert reserved["app_id"] != old["app_id"]


def test_deleted_job_cannot_be_reserved_or_prepared_again(store):
    old = missing_secret(store)
    job = active(store)
    complete(store, job, policy="delete")
    for operation in (lambda: store.prepare_context(old["id"]),
                      lambda: store.reserve(old["id"], snapshot())):
        with pytest.raises(DeployError):
            operation()
