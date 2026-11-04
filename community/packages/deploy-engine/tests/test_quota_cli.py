import json
import os

import pytest

from awesome_deploy.cli import main


@pytest.fixture(autouse=True)
def restore_cli_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"storage_backend": "xfs-project", "state_dir": str(tmp_path / "state")}))
    return ["--config", str(path)]


def test_cli_expands_only_an_explicit_stable_reference_and_revision(tmp_path, monkeypatch, capsys):
    from awesome_deploy.quota_client import QuotaClient
    calls = []
    def call(self, operation, **values):
        calls.append((operation, values))
        return {"state": "ready", "size_mib": 4096, "max_inodes": 100000, "revision": 2}
    monkeypatch.setattr(QuotaClient, "call", call)
    args = config(tmp_path) + [
        "storage-expand", "--storage-id", "a" * 64, "--generation", "b" * 32,
        "--size-mib", "4096", "--max-inodes", "100000", "--expected-revision", "1",
        "--operation-id", "expand-app-001",
    ]
    assert main(args) == 0
    assert calls == [("expand", {"expected": {"backend": "xfs-project", "storage_id": "a" * 64,
                                            "generation": "b" * 32},
                                  "limits": {"size_mib": 4096, "max_inodes": 100000},
                                  "expected_revision": 1, "operation_id": "expand-app-001"})]
    assert json.loads(capsys.readouterr().out)["revision"] == 2


def test_cli_status_is_readonly_and_publishes_no_fake_readiness(tmp_path, monkeypatch, capsys):
    from awesome_deploy.quota_client import QuotaClient
    calls = []
    monkeypatch.setattr(QuotaClient, "call",
                        lambda self, op, **values: calls.append((op, values)) or {"records": []})
    assert main(config(tmp_path) + ["storage-status", "--app-id", "app1"]) == 0
    assert calls == [("inventory", {"app_id": "app1"})]
    assert json.loads(capsys.readouterr().out) == {"records": []}
    assert not (tmp_path / "state" / "quota-status.json").exists()


def test_cli_check_caches_actual_pool_check(tmp_path, monkeypatch):
    from awesome_deploy.quota_client import QuotaClient
    monkeypatch.setattr(QuotaClient, "call", lambda self, op, **values: {"enforced": True})
    assert main(config(tmp_path) + ["storage-check"]) == 0
    assert json.loads((tmp_path / "state" / "quota-status.json").read_text())["status"] == "ready"


def test_cli_patrol_reports_failure_and_does_not_take_worker_build_lock(tmp_path, monkeypatch):
    from awesome_deploy.runtime import DeploymentBackend
    monkeypatch.setattr(DeploymentBackend, "quota_patrol",
                        lambda self: {"pool": {"enforced": True}, "blocked": [{"reason": "bad"}],
                                      "unknown": [], "stop_failures": []})
    assert main(config(tmp_path) + ["storage-patrol"]) == 1
    assert json.loads((tmp_path / "state" / "quota-status.json").read_text())["status"] == "blocked"
    assert not (tmp_path / "state" / "worker.lock").exists()


@pytest.mark.parametrize("field,value", [("--size-mib", "0"), ("--max-inodes", "-1"),
                                        ("--expected-revision", "0"), ("--storage-id", "../escape")])
def test_cli_rejects_invalid_expansion_before_helper(tmp_path, monkeypatch, field, value):
    from awesome_deploy.quota_client import QuotaClient
    monkeypatch.setattr(QuotaClient, "call", lambda *a, **k: pytest.fail("invalid request called helper"))
    values = {"--storage-id": "a" * 64, "--generation": "b" * 32, "--size-mib": "4096",
              "--max-inodes": "1000", "--expected-revision": "1", "--operation-id": "test"}
    values[field] = value
    args = [item for pair in values.items() for item in pair]
    assert main(config(tmp_path) + ["storage-expand", *args]) == 1


def test_schema_cli_version_two_keeps_v1_default(tmp_path, capsys):
    assert main(config(tmp_path) + ["schema", "--version", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["properties"]["schemaVersion"]["const"] == 2


def test_cli_pool_unblock_is_explicit_and_cannot_mix_reference(tmp_path, monkeypatch):
    from awesome_deploy.quota_client import QuotaClient
    calls = []
    monkeypatch.setattr(QuotaClient, "call",
                        lambda self, op, **values: calls.append((op, values)) or {})
    assert main(config(tmp_path) + ["storage-unblock", "--pool"]) == 0
    assert calls == [("unblock", {"pool": True})]
    assert main(config(tmp_path) + ["storage-unblock", "--pool", "--storage-id", "a" * 64,
                                   "--generation", "b" * 32]) == 1
    assert len(calls) == 1


def test_cli_patrol_latched_pool_block_is_not_ready(tmp_path, monkeypatch):
    from awesome_deploy.runtime import DeploymentBackend
    monkeypatch.setattr(DeploymentBackend, "quota_patrol",
                        lambda self: {"pool": {"enforced": True}, "pool_blocked": True,
                                      "blocked": [], "unknown": [], "stop_failures": []})
    assert main(config(tmp_path) + ["storage-patrol"]) == 1
    assert json.loads((tmp_path / "state" / "quota-status.json").read_text())["status"] == "blocked"


def test_cli_retries_only_original_operation_for_exact_reference(tmp_path, monkeypatch):
    from awesome_deploy.quota_client import QuotaClient
    calls = []
    monkeypatch.setattr(QuotaClient, "call",
                        lambda self, op, **values: calls.append((op, values)) or {"state": "ready"})
    assert main(config(tmp_path) + ["storage-retry", "--storage-id", "a" * 64,
                                   "--generation", "b" * 32, "--operation-id", "original-001"]) == 0
    assert calls == [("retry", {"expected": {"backend": "xfs-project", "storage_id": "a" * 64,
                                           "generation": "b" * 32}, "operation_id": "original-001"})]
