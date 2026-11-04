"""Bounded JSON-lines protocol and fixed production trust boundary."""
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys

from .errors import QuotaError
from .service import QuotaService

MAX_LINE = 65536
MAX_RESPONSE = 4 * 1024 * 1024
CONFIG_PATH = Path("/etc/awesome-deploy/storage.json")
STATE_PATH = Path("/var/lib/awesome-storage")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(line):
    try:
        if len(line.encode("utf-8")) > MAX_LINE or not line.endswith("\n"):
            raise ValueError("request bound")
        result = json.loads(line, object_pairs_hook=_pairs, parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("invalid constant")))
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise QuotaError("QUOTA_INVALID_REQUEST", "Malformed or oversized JSON request") from exc


def _reply(output, result=None, error=None):
    value = ({"ok": False, "error": {"code": error.code, "message": error.message}} if error
             else {"ok": True, "result": result})
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_RESPONSE:
        encoded = json.dumps({"ok": False, "error": {"code": "QUOTA_RESPONSE_LIMIT",
                                                       "message": "Response exceeds protocol limit"}})
    output.write(encoded + "\n")
    output.flush()


def serve(service, input_stream, output):
    try:
        line = input_stream.readline(MAX_LINE + 1)
        if not line:
            return
        request = _decode(line)
        if request.get("op") not in ("guard", "patrol"):
            _reply(output, service.execute(request))
            return
        with service.admission():
            _reply(output, service.execute(request, locked=True))
            while True:
                line = input_stream.readline(MAX_LINE + 1)
                if not line:
                    return  # EOF only after every inherited child write FD closes.
                try:
                    request = _decode(line)
                    if request == {"op": "release"}:
                        _reply(output, {"released": True})
                        return
                    if request.get("op") != "retire":
                        raise QuotaError("QUOTA_INVALID_REQUEST", "Session accepts only retire or release")
                    _reply(output, service.execute(request, locked=True))
                except QuotaError as exc:
                    _reply(output, error=exc)
                    # Never release an active admission lock just because a message is invalid.
    except QuotaError as exc:
        _reply(output, error=exc)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
        _reply(output, error=QuotaError("QUOTA_UNAVAILABLE", "Quota helper could not complete the operation"))


def trusted_path(path, *, directory=False):
    """Root-owned parents make subsequent opens race-safe against the caller."""
    path = Path(path)
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        leaf = component == path
        if (info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode)
                or (leaf and directory and not stat.S_ISDIR(info.st_mode))
                or (leaf and not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
                or (not leaf and not stat.S_ISDIR(info.st_mode))):
            raise QuotaError("QUOTA_CONFIG_UNTRUSTED", "Quota configuration or state ownership is unsafe")


def validate_ledger_placement(config):
    for name in ("pool_root", "pool_mount"):
        pool = Path(config[name])
        if STATE_PATH == pool or STATE_PATH.is_relative_to(pool):
            raise QuotaError("QUOTA_CONFIG_UNTRUSTED", "Ledger must be outside the complete quota filesystem")
    if STATE_PATH.stat().st_dev == Path(config["pool_root"]).stat().st_dev:
        raise QuotaError("QUOTA_CONFIG_UNTRUSTED", "Control ledger and quota pool require separate filesystems")


def main():
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise QuotaError("QUOTA_CALLER_FORBIDDEN", "Quota helper requires its fixed root installation")
        trusted_path(CONFIG_PATH)
        with CONFIG_PATH.open(encoding="utf-8") as source:
            raw = source.read(MAX_LINE + 1)
        config = _decode(raw.rstrip("\n") + "\n")
        caller = os.environ.get("SUDO_UID", "0")
        if not caller.isdigit() or int(caller) not in (0, config.get("rootless_uid")):
            raise QuotaError("QUOTA_CALLER_FORBIDDEN", "Caller is not the configured deployment worker")
        trusted_path(STATE_PATH.parent, directory=True)
        STATE_PATH.mkdir(mode=0o700, exist_ok=True)
        trusted_path(STATE_PATH, directory=True)
        for name in ("ledger.sqlite3", "ledger.sqlite3-journal", "ledger.sqlite3-wal", "ledger.sqlite3-shm", "admission.lock", "initialized"):
            path = STATE_PATH / name
            if path.exists() or path.is_symlink():
                trusted_path(path)
        validate_ledger_placement(config)
        os.umask(0o077)
        os.environ.clear()
        os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", LC_ALL="C")
        from .xfs import XFSBackend
        serve(QuotaService(config, XFSBackend(config), STATE_PATH, actor_uid=int(caller)), sys.stdin, sys.stdout)
    except QuotaError as exc:
        _reply(sys.stdout, error=exc)
        return 1
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        _reply(sys.stdout, error=QuotaError("QUOTA_UNAVAILABLE", "Quota helper configuration is unavailable"))
        return 1
    return 0
