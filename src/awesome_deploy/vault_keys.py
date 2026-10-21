"""Read an explicitly provisioned independent KEK keyring, never create one."""

import base64
import json
import os
import re
import stat

from .config import validate_vault_settings
from .errors import DeployError

KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate keyring field")
        result[name] = value
    return result


class VaultKeyring:
    """Key bytes remain private process memory; repr never includes key material."""

    def __init__(self, settings):
        try:
            validate_vault_settings(settings)
            if settings.secret_backend != "encrypted":
                raise ValueError("vault is not enabled")
            path = settings.secret_kek_file.absolute()
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(path.anchor, flags | os.O_DIRECTORY)
            try:
                for part in path.parts[1:-1]:
                    if part in {".", ".."}:
                        raise ValueError("unsafe credential path")
                    child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                parent = os.fstat(descriptor)
                if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                    raise ValueError("unsafe credential directory")
                fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                            or not 1 <= info.st_size <= 32768):
                        raise ValueError("unsafe keyring")
                    raw = os.read(fd, 32769)
                    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink",
                              "st_size", "st_mtime_ns", "st_ctime_ns")
                    after = os.fstat(fd)
                    current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                    if (len(raw) != info.st_size or any(getattr(info, key) != getattr(after, key)
                                                      or getattr(info, key) != getattr(current, key)
                                                      for key in fields)):
                        raise ValueError("keyring changed")
                finally:
                    os.close(fd)
            finally:
                os.close(descriptor)
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if (not isinstance(payload, dict) or set(payload) != {"format", "active", "keys"}
                    or type(payload["format"]) is not int or payload["format"] != 1
                    or not isinstance(payload["active"], str) or not KEY_ID.fullmatch(payload["active"])
                    or not isinstance(payload["keys"], dict) or not 1 <= len(payload["keys"]) <= 128):
                raise ValueError("unsupported keyring")
            keys = {}
            for name, encoded in payload["keys"].items():
                if not KEY_ID.fullmatch(name) or not isinstance(encoded, str):
                    raise ValueError("invalid key")
                value = base64.b64decode(encoded, validate=True)
                if len(value) != 32 or value in keys.values():
                    raise ValueError("invalid or aliased key")
                keys[name] = value
            if payload["active"] not in keys:
                raise ValueError("active key absent")
            self.active = payload["active"]
            self._keys = keys
        except (OSError, ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
            raise DeployError("VAULT_KEY_UNAVAILABLE", "密钥库根密钥缺失、损坏或权限不安全，请恢复原密钥。") from None

    def key(self, identifier):
        try:
            return self._keys[identifier]
        except (KeyError, TypeError):
            raise DeployError("VAULT_KEY_UNAVAILABLE", "密钥库需要的根密钥版本不可用，请恢复原密钥。") from None
