"""Privileged quota ledger: no Docker or real filesystem operations."""
import copy
import io
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))


def test_quota_error_has_stable_safe_category():
    from storage_quota.errors import QuotaError
    error = QuotaError("QUOTA_UNAVAILABLE", "Query unavailable")
    assert error.code == "QUOTA_UNAVAILABLE"
    assert error.message == str(error) == "Query unavailable"


class FakeBackend:
    def __init__(self):
        self.rows = {}
        self.errors = {}
        self.fail_after_bytes = False
        self.delete_ok = True
        self.calls = []

    def check_pool(self):
        return {"free_bytes": 100000 * 1048576, "free_inodes": 10000000, "enforced": True}

    def create(self, record):
        self.calls.append(("create", record["project_id"]))
        self.rows.setdefault(record["generation"], {
            "size_mib": 0, "max_inodes": 0, "allocated_bytes": 0, "used_inodes": 1,
        })
        return {"path": f'/pool/{record["storage_id"]}/{record["generation"]}/payload',
                "device": 42, "inode": record["project_id"]}

    def set_limits(self, record, size_mib, max_inodes):
        row = self.rows[record["generation"]]
        row["size_mib"] = size_mib
        if self.fail_after_bytes:
            self.fail_after_bytes = False
            from storage_quota.errors import QuotaError
            raise QuotaError("QUOTA_UNAVAILABLE", "Interrupted after bytes")
        row["max_inodes"] = max_inodes

    def inspect(self, record):
        if record["generation"] in self.errors:
            raise self.errors[record["generation"]]
        return {**self.rows[record["generation"]], "checked_at": "2026-10-20T00:00:00Z"}

    def delete(self, record):
        if self.delete_ok:
            self.rows.pop(record["generation"], None)
        return self.delete_ok


@pytest.fixture
def setup(tmp_path):
    from storage_quota.service import QuotaService
    config = {"global_budget_mib": 40, "global_budget_inodes": 400,
              "app_budget_mib": 30, "app_budget_inodes": 300,
              "service_budget_mib": 30, "service_budget_inodes": 300,
              "reserve_free_mib": 1, "reserve_free_inodes": 1}
    backend = FakeBackend()
    return QuotaService(config, backend, tmp_path), backend, config


def ensure(service, name="data", **kwargs):
    return service.execute({"op": "ensure", "identity": {"app_id": "app-1", "service": "web", "name": name},
                            "operation_id": f"ensure-{name}", "limits": {"size_mib": 10, "max_inodes": 100}, **kwargs})


def test_ensure_readback_is_stable_and_never_shrinks_or_silently_expands(setup):
    service, backend, _ = setup
    row = ensure(service)
    assert row["size_mib"] == 10 and row["max_inodes"] == 100
    assert row["reference"]["backend"] == "xfs-project"
    assert len(row["reference"]["storage_id"]) == 64
    assert len(row["reference"]["generation"]) == 32
    smaller = ensure(service, expected=row["reference"], limits={"size_mib": 1, "max_inodes": 2},
                     operation_id="ensure-small")
    assert smaller["size_mib"] == 10
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError, match="explicit expansion"):
        ensure(service, expected=row["reference"], limits={"size_mib": 11, "max_inodes": 101},
               operation_id="ensure-big")
    assert len(backend.rows) == 1


def test_missing_expected_never_allocates_and_invalid_input_never_reaches_backend(setup):
    service, backend, _ = setup
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        ensure(service, expected={"backend": "xfs-project", "storage_id": "a" * 64, "generation": "b" * 32})
    for request in [
        {"op": "ensure", "identity": {"app_id": "../app", "service": "web", "name": "data"},
         "operation_id": "bad"},
        {"op": "check", "path": "/etc"},
        {"op": "retire", "container_id": "short"},
    ]:
        with pytest.raises(QuotaError):
            service.execute(request)
    assert not backend.rows


def test_expansion_partial_failure_reserves_budget_and_retries_after_restart(setup, tmp_path):
    service, backend, config = setup
    row = ensure(service)
    request = {"op": "expand", "expected": row["reference"], "limits": {"size_mib": 25, "max_inodes": 250},
               "operation_id": "expand-1", "expected_revision": row["revision"]}
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute(request)
    inventory = service.execute({"op": "inventory"})
    assert inventory["totals"] == {"size_mib": 25, "max_inodes": 250}
    with pytest.raises(QuotaError) as caught:
        ensure(service, "second")
    assert caught.value.code == "QUOTA_BUDGET_EXCEEDED"
    from storage_quota.service import QuotaService
    restarted = QuotaService(config, backend, tmp_path)
    expanded = restarted.execute(request)
    assert expanded["size_mib"] == 25 and expanded["max_inodes"] == 250
    assert expanded["revision"] == row["revision"] + 1
    assert restarted.execute(request)["revision"] == expanded["revision"]
    with pytest.raises(QuotaError):
        restarted.execute({**request, "limits": {"size_mib": 26, "max_inodes": 260}})


def test_failed_delete_still_counts_and_project_ids_never_reused(setup):
    service, backend, _ = setup
    first = ensure(service)
    delete = {"op": "delete", "expected": first["reference"], "operation_id": "delete-1"}
    backend.delete_ok = False
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute(delete)
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 10
    backend.delete_ok = True
    assert service.execute(delete)["deleted"] is True
    assert service.execute(delete)["deleted"] is True
    second = ensure(service, operation_id="ensure-again")
    assert second["reference"]["generation"] != first["reference"]["generation"]
    assert [call[1] for call in backend.calls] == [1, 2]


def writer(container="a"):
    return {"container_id": container * 64, "container_name": "awesome-app1-web-1", "app_id": "app-1",
            "release_id": "rel-1", "service": "web"}


def test_guard_and_patrol_fence_writers_until_explicit_unblock(setup):
    service, backend, _ = setup
    row = ensure(service)
    service.execute({"op": "guard", "items": [{"expected": row["reference"], "writer": writer()}]})
    from storage_quota.errors import QuotaError
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_LIMIT_MISMATCH", "Hard limit changed")
    patrol = service.execute({"op": "patrol"})
    assert patrol["blocked"][0]["writers"] == [writer()]
    backend.errors.clear()
    with pytest.raises(QuotaError) as caught:
        service.execute({"op": "guard", "items": [{"expected": row["reference"], "writer": writer("b")}]})
    assert caught.value.code == "QUOTA_BLOCKED"
    with pytest.raises(QuotaError):
        service.execute({"op": "delete", "expected": row["reference"], "operation_id": "delete"})
    service.execute({"op": "retire", "container_id": "a" * 64})
    service.execute({"op": "unblock", "expected": row["reference"]})
    assert service.execute({"op": "inspect", "expected": row["reference"]})["state"] == "ready"


def test_unknown_patrol_never_invents_a_stop_decision(setup):
    service, backend, _ = setup
    row = ensure(service)
    from storage_quota.errors import QuotaError
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_UNAVAILABLE", "Query failed")
    patrol = service.execute({"op": "patrol"})
    assert patrol["blocked"] == [] and len(patrol["unknown"]) == 1
    assert service.execute({"op": "inventory"})["records"][0]["state"] == "ready"


def test_guard_validates_entire_batch_before_any_registration(setup):
    service, backend, _ = setup
    row = ensure(service)
    from storage_quota.errors import QuotaError
    wrong = copy.deepcopy(row["reference"])
    wrong["generation"] = "b" * 32
    with pytest.raises(QuotaError):
        service.execute({"op": "guard", "items": [{"expected": row["reference"], "writer": writer()},
                                                   {"expected": wrong, "writer": writer("b")}]})
    assert service.execute({"op": "inventory"})["records"][0]["writers"] == []


def test_same_admission_lock_serializes_guard_and_patrol(setup, tmp_path):
    service, backend, config = setup
    from storage_quota.service import QuotaService
    other = QuotaService(config, backend, tmp_path)
    entered = threading.Event()
    finished = threading.Event()
    def patrol():
        entered.set()
        other.execute({"op": "patrol"})
        finished.set()
    with service.admission():
        thread = threading.Thread(target=patrol)
        thread.start()
        assert entered.wait(1)
        assert not finished.wait(0.05)
    thread.join(2)
    assert finished.is_set()


def test_concurrent_allocation_has_one_budget_winner(setup, tmp_path):
    service, backend, config = setup
    from storage_quota.service import QuotaService
    from storage_quota.errors import QuotaError
    config["global_budget_mib"] = 10
    other = QuotaService(config, backend, tmp_path)
    outcomes = []
    def allocate(instance, name):
        try:
            ensure(instance, name)
            outcomes.append("ready")
        except QuotaError as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=allocate, args=(instance, name))
               for instance, name in [(service, "one"), (other, "two")]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert sorted(outcomes) == ["QUOTA_BUDGET_EXCEEDED", "ready"]


def test_bounded_protocol_rejects_extra_keys_duplicate_json_and_oversized_lines(setup):
    from storage_quota.protocol import serve, MAX_LINE
    service, _, _ = setup
    for raw in ['{"op":"check","op":"inventory"}\n', '{"op":"check","uid":0}\n',
                '{"op":"check","padding":"' + "x" * MAX_LINE + '"}\n']:
        output = io.StringIO()
        serve(service, io.StringIO(raw), output)
        result = json.loads(output.getvalue())
        assert result["ok"] is False
        assert result["error"]["code"] == "QUOTA_INVALID_REQUEST"


def test_session_retires_without_deadlock_and_acknowledges_release(setup):
    from storage_quota.protocol import serve
    service, _, _ = setup
    row = ensure(service)
    requests = [{"op": "guard", "items": [{"expected": row["reference"], "writer": writer()}]},
                {"op": "retire", "container_id": "a" * 64}, {"op": "release"}]
    output = io.StringIO()
    serve(service, io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 3 and all(r["ok"] for r in responses)
    assert service.execute({"op": "inventory"})["records"][0]["writers"] == []


def test_new_storage_without_explicit_limits_is_rejected(setup):
    service, backend, _ = setup
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute({"op": "ensure", "identity": {"app_id": "app-1", "service": "web", "name": "data"},
                         "operation_id": "no-limits"})
    assert backend.rows == {}


def test_patrol_verifies_pool_even_when_ledger_is_empty(setup):
    service, backend, _ = setup
    result = service.execute({"op": "patrol"})
    assert result["pool"]["enforced"] is True
    assert result["checked_at"]


def test_partial_expansion_patrol_still_fences_identity_or_enforcement_failure(setup):
    service, backend, _ = setup
    row = ensure(service)
    service.execute({"op": "guard", "items": [{"expected": row["reference"], "writer": writer()}]})
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute({"op": "expand", "expected": row["reference"], "limits": {"size_mib": 15, "max_inodes": 150},
                         "operation_id": "expanding", "expected_revision": 1})
    assert service.execute({"op": "patrol"})["blocked"] == []
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_NOT_ENFORCED", "Disabled")
    result = service.execute({"op": "patrol"})
    assert result["blocked"][0]["writers"] == [writer()]
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 15


def test_partial_expansion_can_be_safely_deleted_without_releasing_before_zero(setup):
    service, backend, _ = setup
    row = ensure(service)
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute({"op": "expand", "expected": row["reference"], "limits": {"size_mib": 15, "max_inodes": 150},
                         "operation_id": "expanding", "expected_revision": 1})
    backend.delete_ok = False
    request = {"op": "delete", "expected": row["reference"], "operation_id": "cleanup-partial"}
    with pytest.raises(QuotaError) as caught:
        service.execute(request)
    assert caught.value.code == "QUOTA_DELETE_PENDING"
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 15
    backend.delete_ok = True
    assert service.execute(request)["deleted"]
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 0


def test_confirmed_failed_mutation_is_blocked_before_return(setup):
    service, backend, _ = setup
    from storage_quota.errors import QuotaError
    def mismatch(*args):
        raise QuotaError("QUOTA_DIRECTORY_MISMATCH", "Replaced directory")
    backend.set_limits = mismatch
    with pytest.raises(QuotaError):
        ensure(service)
    inventory = service.execute({"op": "inventory"})
    assert inventory["records"][0]["state"] == "blocked"
    assert inventory["totals"]["size_mib"] == 10


def test_blocked_pending_storage_cannot_be_implicitly_unblocked_by_retry(setup):
    service, backend, _ = setup
    from storage_quota.errors import QuotaError
    original = backend.inspect
    def mismatch(record):
        raise QuotaError("QUOTA_DIRECTORY_MISMATCH", "Replaced directory")
    backend.inspect = mismatch
    with pytest.raises(QuotaError):
        ensure(service)
    backend.inspect = original
    with pytest.raises(QuotaError) as caught:
        ensure(service)
    assert caught.value.code == "QUOTA_BLOCKED"


def test_shrink_and_stale_revision_never_modify_kernel(setup):
    service, backend, _ = setup
    row = ensure(service)
    from storage_quota.errors import QuotaError
    for limits, revision, code in [({"size_mib": 9, "max_inodes": 100}, 1, "QUOTA_SHRINK_FORBIDDEN"),
                                    ({"size_mib": 11, "max_inodes": 101}, 2, "QUOTA_REVISION_CONFLICT")]:
        with pytest.raises(QuotaError) as caught:
            service.execute({"op": "expand", "expected": row["reference"], "limits": limits,
                             "expected_revision": revision, "operation_id": code})
        assert caught.value.code == code
    assert backend.rows[row["reference"]["generation"]]["size_mib"] == 10


def test_inode_budgets_and_physical_reserve_both_apply(setup):
    service, backend, config = setup
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError) as caught:
        ensure(service, limits={"size_mib": 1, "max_inodes": 301})
    assert caught.value.code == "QUOTA_BUDGET_EXCEEDED"
    backend.check_pool = lambda: {"free_bytes": 10 * 1048576, "free_inodes": 10000}
    with pytest.raises(QuotaError) as caught:
        ensure(service)
    assert caught.value.code == "QUOTA_POOL_RESERVE"
    assert not backend.rows


def test_strict_scalar_schemas_reject_booleans_and_unbounded_ids(setup):
    service, backend, _ = setup
    from storage_quota.errors import QuotaError
    for limits in [{"size_mib": True, "max_inodes": 100}, {"size_mib": 1, "max_inodes": 0},
                   {"size_mib": 2**63, "max_inodes": 100}]:
        with pytest.raises(QuotaError):
            ensure(service, limits=limits)
    assert not backend.rows


def test_untrusted_paths_reject_worker_owned_files_and_symlinks(tmp_path):
    from storage_quota.protocol import trusted_path
    from storage_quota.errors import QuotaError
    path = tmp_path / "config.json"
    path.write_text("{}")
    with pytest.raises(QuotaError):
        trusted_path(path)
    link = tmp_path / "config-link.json"
    link.symlink_to(path)
    with pytest.raises(QuotaError):
        trusted_path(link)


def test_trusted_install_entrypoint_refuses_running_checkout():
    import subprocess
    script = Path(__file__).resolve().parents[1] / "ops" / "awesome-storage"
    result = subprocess.run([sys.executable, "-I", str(script)], text=True, input='{"op":"check"}\n',
                            capture_output=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "QUOTA_CONFIG_UNTRUSTED"


def test_lost_ledger_does_not_reset_project_id_sequence(setup, tmp_path):
    service, backend, config = setup
    ensure(service)
    (tmp_path / "ledger.sqlite3").unlink()
    from storage_quota.errors import QuotaError
    from storage_quota.service import QuotaService
    with pytest.raises(QuotaError) as caught:
        QuotaService(config, backend, tmp_path)
    assert caught.value.code == "QUOTA_LEDGER_MISSING"


def test_missing_ledger_with_existing_pool_data_is_never_initialized(setup, tmp_path):
    _, backend, config = setup
    pool = tmp_path / "existing-pool"
    pool.mkdir()
    (pool / "old-generation").mkdir()
    state = tmp_path / "new-state"
    from storage_quota.errors import QuotaError
    from storage_quota.service import QuotaService
    with pytest.raises(QuotaError) as caught:
        QuotaService({**config, "pool_root": str(pool)}, backend, state)
    assert caught.value.code == "QUOTA_LEDGER_MISSING"


def test_audit_keeps_verified_actor_intent_error_and_before_after_values(setup, tmp_path):
    service, backend, config = setup
    import sqlite3
    from storage_quota.service import QuotaService
    from storage_quota.errors import QuotaError
    service = QuotaService(config, backend, tmp_path, actor_uid=1234)
    row = ensure(service)
    backend.fail_after_bytes = True
    request = {"op": "expand", "expected": row["reference"], "limits": {"size_mib": 15, "max_inodes": 150},
               "expected_revision": 1, "operation_id": "audited-expand"}
    with pytest.raises(QuotaError):
        service.execute(request)
    service.execute(request)
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        db.row_factory = sqlite3.Row
        audit = [dict(row) for row in db.execute("SELECT * FROM audit WHERE operation_id='audited-expand' ORDER BY id")]
    assert [row["result"] for row in audit] == ["intent", "error", "success"]
    assert all(row["actor_uid"] == 1234 and row["created_at"] for row in audit)
    assert json.loads(audit[0]["before_json"])["size_mib"] == 10
    assert json.loads(audit[-1]["after_json"])["size_mib"] == 15
    assert audit[1]["error_code"] == "QUOTA_UNAVAILABLE"


def test_inventory_queries_effective_usage_without_claiming_unknown_is_zero(setup):
    service, backend, _ = setup
    row = ensure(service)
    backend.rows[row["reference"]["generation"]]["allocated_bytes"] = 12345
    assert service.execute({"op": "inventory"})["records"][0]["allocated_bytes"] == 12345
    from storage_quota.errors import QuotaError
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_UNAVAILABLE", "Unknown")
    status = service.execute({"op": "inventory"})["records"][0]
    assert status["allocated_bytes"] is None and status["query_error"] == "QUOTA_UNAVAILABLE"


def test_conflicting_identity_in_one_guard_batch_is_never_registered(setup):
    service, backend, _ = setup
    row = ensure(service)
    from storage_quota.errors import QuotaError
    other = {**writer(), "release_id": "different-release"}
    with pytest.raises(QuotaError):
        service.execute({"op": "guard", "items": [{"expected": row["reference"], "writer": writer()},
                                                   {"expected": row["reference"], "writer": other}]})
    assert service.execute({"op": "inventory"})["records"][0]["writers"] == []


def _installed_entrypoint(monkeypatch, extra=None):
    import os
    import stat
    import types
    import storage_quota.protocol
    script = Path(__file__).resolve().parents[1] / "ops" / "awesome-storage"
    installed = "/usr/local/libexec/awesome-storage"
    library = Path("/usr/local/libexec/awesome-storage-lib")
    names = {"__init__.py", "errors.py", "service.py", "protocol.py", "xfs.py"}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "flags", types.SimpleNamespace(isolated=1))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(Path, "lstat", lambda path: types.SimpleNamespace(
        st_uid=0, st_nlink=1, st_mode=(stat.S_IFREG | 0o644) if path.name in names | {"awesome-storage"}
        else (stat.S_IFDIR | 0o755)))
    def entries(path):
        if path == library:
            return iter([library / "storage_quota"])
        return iter([path / name for name in sorted(names | ({extra} if extra else set()))])
    monkeypatch.setattr(Path, "iterdir", entries)
    calls = []
    monkeypatch.setattr(storage_quota.protocol, "main", lambda: calls.append(sys.dont_write_bytecode) or 0)
    return script.read_text(), installed, calls


def test_installed_entrypoint_disables_bytecode_before_both_calls(monkeypatch):
    source, installed, calls = _installed_entrypoint(monkeypatch)
    for _ in range(2):
        with pytest.raises(SystemExit) as stopped:
            exec(compile(source, installed, "exec"), {"__file__": installed})
        assert stopped.value.code == 0
    assert calls == [True, True]


def test_installed_entrypoint_rejects_import_cache_or_extension_shadows(monkeypatch, capsys):
    for extra in ("__pycache__", "__init__.so", "service.so"):
        with monkeypatch.context() as patch:
            source, installed, calls = _installed_entrypoint(patch, extra)
            with pytest.raises(SystemExit) as stopped:
                exec(compile(source, installed, "exec"), {"__file__": installed})
            assert stopped.value.code == 1
            assert not calls
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "QUOTA_CONFIG_UNTRUSTED"


def test_inventory_exact_reference_returns_deleted_tombstone_without_backend(setup):
    service, backend, _ = setup
    row = ensure(service)
    service.execute({"op": "delete", "expected": row["reference"], "operation_id": "delete-tombstone"})
    def unexpected(record):
        pytest.fail("Deleted tombstone must not require backend readiness")
    backend.inspect = unexpected
    result = service.execute({"op": "inventory", "expected": row["reference"]})
    assert result["records"][0]["state"] == "deleted"
    assert result["totals"] == {"size_mib": 0, "max_inodes": 0}
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError) as caught:
        service.execute({"op": "inventory", "expected": {**row["reference"], "storage_id": "c" * 64}})
    assert caught.value.code == "QUOTA_REFERENCE_MISSING"


@pytest.mark.parametrize("corrupt", ["truncate", "tables", "version"])
def test_established_corrupted_ledger_is_never_reinitialized(setup, tmp_path, corrupt):
    service, backend, config = setup
    ensure(service)
    import sqlite3
    database = tmp_path / "ledger.sqlite3"
    if corrupt == "truncate":
        database.write_bytes(b"")
    else:
        with sqlite3.connect(database) as db:
            db.execute("DROP TABLE allocations" if corrupt == "tables" else "PRAGMA user_version=999")
    from storage_quota.errors import QuotaError
    from storage_quota.service import QuotaService
    with pytest.raises(QuotaError) as caught:
        QuotaService(config, backend, tmp_path)
    assert caught.value.code == "QUOTA_LEDGER_CORRUPT"


def test_confirmed_pool_block_survives_recovery_and_gates_new_identity(setup, tmp_path):
    service, backend, config = setup
    row = ensure(service)
    original = backend.check_pool
    from storage_quota.errors import QuotaError
    def disabled():
        raise QuotaError("QUOTA_NOT_ENFORCED", "Disabled")
    backend.check_pool = disabled
    assert service.execute({"op": "patrol"})["pool_blocked"]
    backend.check_pool = original
    from storage_quota.service import QuotaService
    restarted = QuotaService(config, backend, tmp_path)
    assert restarted.execute({"op": "patrol"})["pool_blocked"]
    with pytest.raises(QuotaError) as caught:
        ensure(restarted, "new-identity")
    assert caught.value.code == "QUOTA_POOL_BLOCKED"
    with pytest.raises(QuotaError):
        restarted.execute({"op": "unblock", "expected": row["reference"]})
    restarted.execute({"op": "unblock", "pool": True})
    restarted.execute({"op": "unblock", "expected": row["reference"]})
    assert not restarted.execute({"op": "patrol"})["pool_blocked"]
    assert ensure(restarted, "new-identity")["state"] == "ready"


def test_pool_without_allocations_can_only_unblock_explicitly_after_revalidation(setup):
    service, backend, _ = setup
    original = backend.check_pool
    from storage_quota.errors import QuotaError
    def disabled():
        raise QuotaError("QUOTA_NOT_ENFORCED", "Disabled")
    backend.check_pool = disabled
    assert service.execute({"op": "patrol"})["pool_blocked"]
    with pytest.raises(QuotaError):
        service.execute({"op": "unblock", "pool": True})
    backend.check_pool = original
    assert service.execute({"op": "unblock", "pool": True})["pool_blocked"] is False
    assert ensure(service)["state"] == "ready"


def test_blocked_initial_creation_without_directory_can_resume_after_explicit_unblock(setup):
    service, backend, _ = setup
    original = backend.create
    from storage_quota.errors import QuotaError
    def interrupted(record):
        raise QuotaError("QUOTA_DIRECTORY_MISMATCH", "Creation identity not yet confirmed")
    backend.create = interrupted
    with pytest.raises(QuotaError):
        ensure(service)
    row = service.execute({"op": "inventory"})["records"][0]
    backend.create = original
    # Real backend.create itself installs both limits before returning identity.
    def initialize(record):
        directory = original(record)
        backend.set_limits(record, record["size_mib"], record["max_inodes"])
        return directory
    backend.create = initialize
    resumed = service.execute({"op": "unblock", "expected": row["reference"]})
    assert resumed["state"] == "allocating"
    assert ensure(service)["state"] == "ready"


def test_control_ledger_cannot_reside_anywhere_in_pool_mount(monkeypatch):
    from storage_quota.protocol import validate_ledger_placement
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        validate_ledger_placement({"pool_root": "/var/lib/payload", "pool_mount": "/var/lib"})


def test_control_ledger_cannot_share_pool_device_through_alias(monkeypatch):
    import types
    from storage_quota.protocol import validate_ledger_placement
    from storage_quota.errors import QuotaError
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: types.SimpleNamespace(st_dev=42))
    with pytest.raises(QuotaError):
        validate_ledger_placement({"pool_root": "/srv/data/payload", "pool_mount": "/srv/data"})


def test_deleted_generation_tombstone_survives_same_identity_reallocation(setup):
    service, backend, _ = setup
    first = ensure(service)
    service.execute({"op": "delete", "expected": first["reference"], "operation_id": "delete-old"})
    second = ensure(service, operation_id="ensure-new-generation")
    assert second["reference"] != first["reference"]
    old = service.execute({"op": "inventory", "expected": first["reference"]})["records"][0]
    assert old["state"] == "deleted" and old["reference"] == first["reference"]
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 10


def test_lower_declaration_explicitly_reports_retained_effective_and_initial_limits(setup):
    service, backend, _ = setup
    first = ensure(service)
    result = ensure(service, operation_id="old-declaration", expected=first["reference"],
                    limits={"size_mib": 5, "max_inodes": 50})
    assert result["requested_limits"] == {"size_mib": 5, "max_inodes": 50}
    assert result["initial_limits"] == {"size_mib": 10, "max_inodes": 100}
    assert result["kept_current_limits"] is True
    assert result["size_mib"] == 10


def test_pool_audit_has_global_before_after_and_registered_writer_requests(setup, tmp_path):
    service, backend, _ = setup
    row = ensure(service)
    request = {"op": "guard", "items": [{"expected": row["reference"], "writer": writer()}]}
    service.execute(request)
    from storage_quota.errors import QuotaError
    def disabled():
        raise QuotaError("QUOTA_NOT_ENFORCED", "Disabled")
    backend.check_pool = disabled
    service.execute({"op": "patrol"})
    import sqlite3
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        guard = db.execute("SELECT request_json FROM audit WHERE operation='guard'").fetchone()
        patrol = db.execute("SELECT before_json,after_json FROM audit WHERE operation='patrol'").fetchone()
    assert json.loads(guard[0]) == request
    assert json.loads(patrol[0])["blocked"] is False
    assert json.loads(patrol[1])["blocked"] is True


def test_retry_replays_saved_pending_ensure_without_new_identity(setup):
    service, backend, _ = setup
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        ensure(service)
    pending = service.execute({"op": "inventory"})["records"][0]
    result = service.execute({"op": "retry", "expected": pending["reference"], "operation_id": "ensure-data"})
    assert result["reference"] == pending["reference"] and result["state"] == "ready"
    assert len(backend.rows) == 1
    with pytest.raises(QuotaError):
        service.execute({"op": "retry", "expected": pending["reference"], "operation_id": "wrong-operation"})


def test_retry_preserves_expansion_revision_and_does_not_bypass_block(setup):
    service, backend, _ = setup
    row = ensure(service)
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    request = {"op": "expand", "expected": row["reference"], "limits": {"size_mib": 15, "max_inodes": 150},
               "operation_id": "retry-expand", "expected_revision": 1}
    with pytest.raises(QuotaError):
        service.execute(request)
    retry = {"op": "retry", "expected": row["reference"], "operation_id": "retry-expand"}
    result = service.execute(retry)
    assert result["revision"] == 2 and result["size_mib"] == 15
    assert service.execute(retry)["revision"] == 2
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_DIRECTORY_MISMATCH", "Replaced")
    service.execute({"op": "patrol"})
    backend.errors.clear()
    with pytest.raises(QuotaError) as caught:
        service.execute(retry)
    assert caught.value.code == "QUOTA_BLOCKED"


def test_slow_delete_releases_admission_for_patrol_and_retains_budget_until_finish(setup, tmp_path):
    service, backend, config = setup
    from storage_quota.service import QuotaService
    other = QuotaService(config, backend, tmp_path)
    row = ensure(service)
    entered = threading.Event()
    proceed = threading.Event()
    completed = threading.Event()
    original = backend.delete
    errors = []
    def slow_delete(record):
        entered.set()
        assert proceed.wait(3)
        return original(record)
    backend.delete = slow_delete
    def delete():
        try:
            service.execute({"op": "delete", "expected": row["reference"], "operation_id": "slow-delete"})
        except Exception as exc:
            errors.append(exc)
    deletion = threading.Thread(target=delete)
    deletion.start()
    assert entered.wait(1)
    def patrol():
        other.execute({"op": "patrol"})
        completed.set()
    patrol_thread = threading.Thread(target=patrol)
    patrol_thread.start()
    try:
        assert completed.wait(0.5), "Patrol was held behind recursive deletion"
        assert other.execute({"op": "inventory"})["totals"]["size_mib"] == 10
    finally:
        proceed.set()
        deletion.join(3)
        patrol_thread.join(3)
    assert not errors
    assert other.execute({"op": "inventory"})["totals"]["size_mib"] == 0


def test_delete_finish_rechecks_generation_and_never_releases_changed_reservation(setup):
    service, backend, _ = setup
    row = ensure(service)
    original = backend.delete
    def changed(record):
        result = original(record)
        with service._connect() as db:
            changed_row = service._lookup(db, row["reference"])
            changed_row["revision"] += 1
            service._put(db, changed_row)
        return result
    backend.delete = changed
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError) as caught:
        service.execute({"op": "delete", "expected": row["reference"], "operation_id": "changed-delete"})
    assert caught.value.code == "QUOTA_OPERATION_CONFLICT"
    assert service.execute({"op": "inventory"})["totals"]["size_mib"] == 10


def test_retry_delete_keeps_original_operation_and_has_no_second_kernel_delete(setup):
    service, backend, _ = setup
    row = ensure(service)
    backend.delete_ok = False
    from storage_quota.errors import QuotaError
    request = {"op": "delete", "expected": row["reference"], "operation_id": "recover-delete"}
    with pytest.raises(QuotaError):
        service.execute(request)
    backend.delete_ok = True
    retry = {"op": "retry", "expected": row["reference"], "operation_id": "recover-delete"}
    assert service.execute(retry)["deleted"]
    backend.delete = lambda record: pytest.fail("Completed deletion must not mutate again")
    assert service.execute(retry)["deleted"]


def test_retry_rejects_modified_saved_request_and_injected_new_parameters(setup, tmp_path):
    service, backend, _ = setup
    row = ensure(service)
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    request = {"op": "expand", "expected": row["reference"], "limits": {"size_mib": 15, "max_inodes": 150},
               "expected_revision": 1, "operation_id": "retry-integrity"}
    with pytest.raises(QuotaError):
        service.execute(request)
    retry = {"op": "retry", "expected": row["reference"], "operation_id": "retry-integrity"}
    with pytest.raises(QuotaError):
        service.execute({**retry, "limits": {"size_mib": 20, "max_inodes": 200}})
    import sqlite3
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        altered = {**request, "limits": {"size_mib": 20, "max_inodes": 200}}
        db.execute("UPDATE audit SET request_json=? WHERE operation_id=? AND result='intent'",
                   (json.dumps(altered), "retry-integrity"))
    with pytest.raises(QuotaError) as caught:
        service.execute(retry)
    assert caught.value.code == "QUOTA_OPERATION_CONFLICT"
    assert backend.rows[row["reference"]["generation"]]["size_mib"] == 15


def test_pending_inventory_distinguishes_observed_limits_from_committed_and_requested(setup):
    service, backend, _ = setup
    row = ensure(service)
    backend.fail_after_bytes = True
    from storage_quota.errors import QuotaError
    with pytest.raises(QuotaError):
        service.execute({"op": "expand", "expected": row["reference"],
                         "limits": {"size_mib": 15, "max_inodes": 150},
                         "expected_revision": 1, "operation_id": "observed-partial-expand"})
    status = service.execute({"op": "inventory", "expected": row["reference"]})["records"][0]
    assert status["size_mib"] == 10 and status["max_inodes"] == 100
    assert status["pending"]["size_mib"] == 15 and status["pending"]["max_inodes"] == 150
    assert status["observed_limits"] == {"size_mib": 15, "max_inodes": 100}
    backend.errors[row["reference"]["generation"]] = QuotaError("QUOTA_UNAVAILABLE", "Unknown query result")
    unknown = service.execute({"op": "inventory", "expected": row["reference"]})["records"][0]
    assert unknown["observed_limits"] is None
    assert unknown["size_mib"] == 10 and unknown["max_inodes"] == 100
