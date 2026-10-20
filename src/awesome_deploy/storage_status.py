"""Unprivileged, ordered capability observations; never an admission authority."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import math
import os
import stat
import tempfile
import time


MAX_AGE = 120


def _timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _empty():
    return {"version": 2, "issuedSequence": 0, "acceptedSequence": 0,
            "status": "unknown", "checkedAt": None, "lastSuccessfulCheckAt": None}


def _read(settings):
    try:
        fd = os.open(settings.state_dir / "quota-status.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                return _empty()
            text = stream.read(4097)
        if len(text) > 4096:
            return _empty()
        value = json.loads(text)
        if (not isinstance(value, dict) or value.get("version") != 2
                or type(value.get("issuedSequence")) is not int
                or type(value.get("acceptedSequence")) is not int
                or not 0 <= value["acceptedSequence"] <= value["issuedSequence"]
                or value.get("status") not in ("ready", "unknown", "blocked")):
            return _empty()
        return {key: value.get(key) for key in _empty()}
    except (OSError, ValueError):
        return _empty()


@contextmanager
def _locked(settings):
    settings.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(settings.state_dir / "quota-status.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise OSError("Unsafe quota status lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(settings, value):
    fd, temporary = tempfile.mkstemp(prefix=".quota-status-", dir=settings.state_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, settings.state_dir / "quota-status.json")
        directory_fd = os.open(settings.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def begin_quota_observation(settings):
    """Allocate ordering and capture the start BEFORE dispatching the helper RPC."""
    with _locked(settings):
        value = _read(settings)
        value["issuedSequence"] += 1
        observation = {"sequence": value["issuedSequence"], "startedAt": time.time()}
        _write(settings, value)
        return observation


def record_quota_status(settings, report=None, *, error=None, observation):
    """Accept only a newer observation, without refreshing old helper evidence."""
    if (not isinstance(observation, dict) or type(observation.get("sequence")) is not int
            or observation["sequence"] <= 0 or _timestamp(observation.get("startedAt")) is None):
        raise ValueError("A quota observation captured before the RPC is required")
    report = report if isinstance(report, dict) else {}
    pool = report.get("pool", report)
    confirmed = isinstance(pool, dict) and pool.get("enforced") is True
    status = "ready" if confirmed else "unknown"
    if report.get("pool_blocked") or report.get("blocked") or report.get("stop_failures"):
        status = "blocked"
    elif report.get("unknown"):
        status = "unknown"
    if error is not None:
        code = getattr(error, "code", "")
        status = "blocked" if code.endswith("_MISMATCH") or code in {
            "QUOTA_NOT_ENFORCED", "QUOTA_BLOCKED", "QUOTA_POOL_BLOCKED",
        } else "unknown"
    checked = observation["startedAt"]
    for source in (report, pool):
        if isinstance(source, dict) and "checked_at" in source:
            actual = _timestamp(source["checked_at"])
            if actual is None:
                status = "unknown" if status == "ready" else status
            else:
                checked = min(checked, actual)
    with _locked(settings):
        previous = _read(settings)
        sequence = observation["sequence"]
        if sequence > previous["issuedSequence"]:
            raise ValueError("Unknown quota observation sequence")
        previous_checked = _timestamp(previous.get("checkedAt"))
        if (sequence <= previous["acceptedSequence"]
                or previous_checked is not None and checked < previous_checked):
            return False
        value = {**previous, "acceptedSequence": sequence, "status": status, "checkedAt": checked}
        if status == "ready":
            value["lastSuccessfulCheckAt"] = checked
        _write(settings, value)
    return True


def quota_capability(settings):
    configured = settings.storage_backend == "xfs-project"
    result = {
        "supported": True, "configured": configured, "enabled": False,
        "backend": "xfs-project", "status": "unknown" if configured else "disabled",
        "checkedAt": None, "lastSuccessfulCheckAt": None, "maxCheckAgeSeconds": MAX_AGE,
        "dimensions": ["allocatedBytes", "inodes"], "initialization": "empty",
        "automaticMigration": False,
    }
    if not configured:
        return result
    value = _read(settings)
    checked = _timestamp(value.get("checkedAt"))
    result["checkedAt"] = checked
    result["lastSuccessfulCheckAt"] = _timestamp(value.get("lastSuccessfulCheckAt"))
    if checked is not None and 0 <= time.time() - checked <= MAX_AGE:
        result["status"] = value["status"]
        result["enabled"] = value["status"] == "ready"
    return result
