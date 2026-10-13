"""Versioned operator-managed secrets; values never belong in deployment records."""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .config import Settings
from .errors import DeployError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"[0-9a-f]{32}\Z")


class SecretStore:
    def __init__(self, settings: Settings):
        self.root = settings.state_dir / "secrets"

    def _directory(self, repository: str, subdomain: str, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise DeployError("INVALID_SECRET_NAME", "秘密配置名称格式不正确。")
        scope = hashlib.sha256((repository + "\0" + subdomain).encode()).hexdigest()
        return self.root / scope / name

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())

    def set(self, repository: str, subdomain: str, name: str, value: str) -> str:
        if not value or "\x00" in value or len(value.encode()) > 16384:
            raise DeployError("INVALID_SECRET", "秘密配置不能为空、包含 NUL 或超过 16 KiB。")
        directory = self._directory(repository, subdomain, name)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Intermediate directories must not expose values to other local users.
        for parent in (self.root, directory.parent, directory):
            if parent.is_symlink():
                raise DeployError("UNSAFE_SECRET_STORE", "秘密配置目录不能是符号链接。")
            parent.chmod(0o700)
        version = uuid.uuid4().hex
        self._write(directory / (version + ".json"), {"version": version, "value": value})
        pointer = directory / (".current-" + version)
        self._write(pointer, {"version": version})
        os.replace(pointer, directory / "current.json")
        return version

    def versions(self, repository: str, subdomain: str, refs: dict[str, str]) -> dict[str, str]:
        versions = {}
        for name in refs.values():
            directory = self._directory(repository, subdomain, name)
            try:
                pointer = directory / "current.json"
                if pointer.is_symlink():
                    raise OSError("unsafe pointer")
                version = json.loads(pointer.read_text())["version"]
                if not _VERSION.fullmatch(version):
                    raise ValueError("invalid version")
                versions[name] = version
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise DeployError("SECRET_NOT_FOUND", f"请先通过安全入口配置 {name}。", "secretRefs") from exc
        return versions

    def resolve(
        self, repository: str, subdomain: str, refs: dict[str, str], versions: dict[str, str] | None = None
    ) -> dict[str, str]:
        versions = versions if versions is not None else self.versions(repository, subdomain, refs)
        result = {}
        for env_name, name in refs.items():
            directory = self._directory(repository, subdomain, name)
            version = versions.get(name, "")
            if not _VERSION.fullmatch(version):
                raise DeployError("SECRET_NOT_FOUND", f"未找到已冻结的配置版本：{name}。", "secretRefs")
            path = directory / (version + ".json")
            try:
                if path.is_symlink() or path.stat().st_mode & 0o077:
                    raise OSError("unsafe secret file")
                result[env_name] = json.loads(path.read_text())["value"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise DeployError(
                    "SECRET_NOT_FOUND", f"无法读取配置 {name} 的指定版本。", "secretRefs"
                ) from exc
        return result
