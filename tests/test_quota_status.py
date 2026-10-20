import json
import os
import time

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


def test_quota_capability_distinguishes_support_configuration_and_fresh_verification(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    disabled = quota_capability(Settings(state_dir=tmp_path))
    assert disabled["supported"] is True
    assert disabled["configured"] is False and disabled["enabled"] is False
    configured = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    assert quota_capability(configured)["status"] == "unknown"
    record_quota_status(configured, {"enforced": True, "path": "/private/never-public"}, observation=begin_quota_observation(configured))
    result = quota_capability(configured)
    assert result["enabled"] is True and result["status"] == "ready"
    assert result["checkedAt"] <= time.time()
    assert "never-public" not in json.dumps(result)
    assert "never-public" not in (tmp_path / "quota-status.json").read_text()
    record_quota_status(configured, error=DeployError("QUOTA_UNAVAILABLE", "private"), observation=begin_quota_observation(configured))
    assert quota_capability(configured)["status"] == "unknown"
    assert quota_capability(configured)["enabled"] is False


def test_stale_or_malformed_cache_never_claims_live_protection(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    record_quota_status(settings, {"enforced": True}, observation=begin_quota_observation(settings))
    path = tmp_path / "quota-status.json"
    saved = json.loads(path.read_text())
    saved["checkedAt"] = time.time() - 300
    path.write_text(json.dumps(saved))
    assert quota_capability(settings)["status"] == "unknown"
    assert quota_capability(settings)["enabled"] is False
    for value in ['{"status":"ready","checkedAt":NaN}', "null", "not-json"]:
        path.write_text(value)
        assert quota_capability(settings)["enabled"] is False


def test_patrol_blocked_unknown_and_stop_failure_cannot_publish_ready(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    record_quota_status(settings, {"enforced": True, "blocked": [{"writers": []}]}, observation=begin_quota_observation(settings))
    assert quota_capability(settings)["status"] == "blocked"
    record_quota_status(settings, {"enforced": True, "unknown": ["unreachable"]}, observation=begin_quota_observation(settings))
    assert quota_capability(settings)["status"] == "unknown"
    record_quota_status(settings, {"enforced": True, "stop_failures": [{"container_id": "secret"}]}, observation=begin_quota_observation(settings))
    assert quota_capability(settings)["enabled"] is False
    record_quota_status(settings, {"enforced": True, "pool_blocked": True}, observation=begin_quota_observation(settings))
    assert quota_capability(settings)["status"] == "blocked"
    record_quota_status(settings, {}, observation=begin_quota_observation(settings))
    assert quota_capability(settings)["enabled"] is False


def test_cache_symlink_is_not_read_as_verified_and_write_does_not_follow_it(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    target = tmp_path / "other"
    target.write_text("must remain")
    (tmp_path / "quota-status.json").symlink_to(target)
    assert quota_capability(settings)["enabled"] is False
    record_quota_status(settings, {"enforced": True}, observation=begin_quota_observation(settings))
    assert target.read_text() == "must remain"
    assert os.stat(tmp_path / "quota-status.json").st_mode & 0o077 == 0



def test_old_rpc_completion_cannot_overwrite_newer_pool_block(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    old = begin_quota_observation(settings)
    newer = begin_quota_observation(settings)
    record_quota_status(settings, {"pool_blocked": True}, observation=newer)
    record_quota_status(settings, {"enforced": True}, observation=old)
    assert quota_capability(settings)["status"] == "blocked"


def test_old_helper_timestamp_never_extends_freshness(tmp_path):
    from datetime import datetime, timezone
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    checked = time.time() - 300
    report = {"enforced": True, "checked_at": datetime.fromtimestamp(checked, timezone.utc).isoformat()}
    for _ in range(2):
        record_quota_status(settings, report, observation=begin_quota_observation(settings))
        result = quota_capability(settings)
        assert result["status"] == "unknown" and result["enabled"] is False
        assert abs(result["checkedAt"] - checked) < .001


def test_current_rpc_returning_older_report_cannot_clear_block(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    record_quota_status(settings, {"pool_blocked": True}, observation=begin_quota_observation(settings))
    record_quota_status(settings, {"enforced": True, "checked_at": time.time() - 60},
                        observation=begin_quota_observation(settings))
    assert quota_capability(settings)["status"] == "blocked"


def test_unknown_retains_last_successful_observation_time(tmp_path):
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    observation = begin_quota_observation(settings)
    record_quota_status(settings, {"enforced": True}, observation=observation)
    record_quota_status(settings, error=DeployError("QUOTA_UNAVAILABLE", "timeout"),
                        observation=begin_quota_observation(settings))
    result = quota_capability(settings)
    assert result["status"] == "unknown"
    assert result["lastSuccessfulCheckAt"] == observation["startedAt"]


def test_old_cache_format_is_unknown_even_if_recent(tmp_path):
    from awesome_deploy.storage_status import quota_capability
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    path = tmp_path / "quota-status.json"
    path.write_text(json.dumps({"version": 1, "status": "ready", "checkedAt": time.time()}))
    path.chmod(0o600)
    assert quota_capability(settings)["enabled"] is False


def test_concurrent_updates_keep_newest_observation_under_thread_contention(tmp_path):
    import threading
    from awesome_deploy.storage_status import begin_quota_observation, quota_capability, record_quota_status
    settings = Settings(state_dir=tmp_path, storage_backend="xfs-project")
    tokens = [begin_quota_observation(settings) for _ in range(20)]
    barrier = threading.Barrier(len(tokens))
    failures = []
    def update(index):
        try:
            barrier.wait(timeout=5)
            report = {"pool_blocked": True} if index == len(tokens) - 1 else {"enforced": True}
            record_quota_status(settings, report, observation=tokens[index])
        except Exception as error:
            failures.append(error)
    threads = [threading.Thread(target=update, args=(index,)) for index in range(len(tokens))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures and not any(thread.is_alive() for thread in threads)
    assert quota_capability(settings)["status"] == "blocked"
