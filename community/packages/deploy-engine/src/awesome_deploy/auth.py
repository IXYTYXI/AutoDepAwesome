"""Operator credentials for the restricted first-phase management endpoint."""

import hashlib
import hmac
import json
import os
from pathlib import Path

from .errors import DeployError
from .config_validation import is_basic_username

ITERATIONS = 600_000


def initialize_credentials(path: Path, username: str, password: str) -> None:
    if (
        not 12 <= len(password) <= 1024
        or not is_basic_username(username)
        or any(not 32 <= ord(char) <= 126 for char in password)
    ):
        raise DeployError(
            "INVALID_CREDENTIALS",
            "运维账号使用 ASCII 字符：用户名 1–128 位且不含空格或冒号，密码 12–1024 位。",
        )
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DeployError("CREDENTIALS_EXIST", "管理员凭证文件已经存在，请使用单独的轮换流程。") from exc
    with os.fdopen(fd, "w") as f:
        json.dump(
            {"username": username, "salt": salt.hex(), "hash": digest.hex(), "iterations": ITERATIONS}, f
        )
        f.flush()
        os.fsync(f.fileno())


def verify_credentials(path: Path | None, username: str, password: str) -> bool:
    if path is None or len(password) > 1024 or len(username) > 128:
        return False
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return False
        data = json.loads(path.read_text())
        if data["iterations"] != ITERATIONS:
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(data["salt"]), ITERATIONS)
        return hmac.compare_digest(derived.hex(), data["hash"]) & hmac.compare_digest(
            username, data["username"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
