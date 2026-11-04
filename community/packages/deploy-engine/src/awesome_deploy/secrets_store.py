"""Versioned operator-managed secrets; values never belong in deployment records."""

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .config import Settings
from .errors import DeployError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"[0-9a-f]{32}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_SECRET_FILE = re.compile(r"(?:current\.json|[0-9a-f]{32}\.json|\.current-[0-9a-f]{32})\Z")
_DIRECTORY_IDENTITY = {"dev", "ino", "mode", "uid", "gid"}
_FILE_IDENTITY = _DIRECTORY_IDENTITY | {"nlink", "size", "mtime_ns", "ctime_ns"}


class SecretStore:
    def __init__(self, settings: Settings):
        self.root = settings.state_dir / "secrets"

    def _directory(self, repository: str, subdomain: str, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise DeployError("INVALID_SECRET_NAME", "秘密配置名称格式不正确。")
        scope = hashlib.sha256((repository + "\0" + subdomain).encode()).hexdigest()
        return self.root / scope / name

    @staticmethod
    def validate_value(value: str) -> None:
        """Validate the entire submitted batch before callers write any values."""
        valid = isinstance(value, str) and bool(value) and not any(c in value for c in "\r\n\x00")
        if valid:
            try:
                valid = len(value.encode("utf-8")) <= 16384
            except UnicodeError:
                valid = False
        if not valid:
            raise DeployError("INVALID_SECRET", "秘密配置必须是 1 至 16384 字节的单行文本，不能包含 NUL。")

    @staticmethod
    def _check_private(info, *, directory: bool) -> None:
        kind_matches = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not kind_matches or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
            raise OSError("unsafe secret store metadata")

    @staticmethod
    def _open_child_directory(parent: int, name: str, *, create: bool) -> int:
        try:
            return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                # Another writer may have created it; open and check without following links.
                pass
            return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)

    @contextmanager
    def _open_state_directory(self, *, create: bool = False):
        """Open a private state directory without following any ancestor symlink.

        System ancestors need not be private or owned by this UID. Existing state
        and secret directories are checked, never chmodded or repaired.
        """
        path = self.root.parent.absolute()
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        try:
            for part in path.parts[1:]:
                if part in {".", ".."}:
                    raise OSError("unsafe state path")
                child = self._open_child_directory(descriptor, part, create=create)
                os.close(descriptor)
                descriptor = child
            self._check_private(os.fstat(descriptor), directory=True)
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _open_secret_directory(self, repository: str, subdomain: str, name: str, *, create: bool):
        directory = self._directory(repository, subdomain, name)
        with self._open_state_directory(create=create) as state_descriptor:
            descriptor = os.dup(state_descriptor)
            try:
                for part in (self.root.name, directory.parent.name, directory.name):
                    child = self._open_child_directory(descriptor, part, create=create)
                    os.close(descriptor)
                    descriptor = child
                    self._check_private(os.fstat(descriptor), directory=True)
                yield descriptor
            finally:
                os.close(descriptor)

    def _inspect_directory(self, descriptor: int) -> str:
        try:
            pointer = os.open("current.json", _READ_FLAGS, dir_fd=descriptor)
        except FileNotFoundError:
            # An orphaned version may be an application's original master secret.
            # A lost pointer must never authorize silently replacing that value.
            return "unavailable" if os.listdir(descriptor) else "missing"
        try:
            info = os.fstat(pointer)
            self._check_private(info, directory=False)
            if not 1 <= info.st_size <= 512:
                return "unavailable"
            raw = os.read(pointer, 513)
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"version"}:
                return "unavailable"
            version = payload["version"]
            if not isinstance(version, str) or not _VERSION.fullmatch(version):
                return "unavailable"
            # Values never need to be opened to report whether a version is available.
            info = os.stat(version + ".json", dir_fd=descriptor, follow_symlinks=False)
            self._check_private(info, directory=False)
            return "configured"
        except (OSError, ValueError, TypeError):
            return "unavailable"
        finally:
            os.close(pointer)

    def inspect(self, repository: str, subdomain: str, name: str) -> str:
        """Return missing/configured/unavailable without reading any secret value."""
        try:
            with self._open_secret_directory(repository, subdomain, name, create=False) as descriptor:
                return self._inspect_directory(descriptor)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unavailable"

    @staticmethod
    def _delete_conflict():
        return DeployError("SECRET_DELETE_CONFLICT", "秘密配置清理范围或文件状态发生变化，已停止清理，请检查后重试。")

    @staticmethod
    def _scope_name(repository, subdomain):
        if (not isinstance(repository, str) or not repository or len(repository) > 4096 or "\0" in repository
                or not isinstance(subdomain, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", subdomain)):
            raise ValueError("invalid secret scope")
        return hashlib.sha256((repository + "\0" + subdomain).encode()).hexdigest()

    @classmethod
    def _deletion_identity(cls, info, *, directory):
        cls._check_private(info, directory=directory)
        if not directory and info.st_nlink != 1:
            raise OSError("linked secret file")
        fields = _DIRECTORY_IDENTITY if directory else _FILE_IDENTITY
        return {key: getattr(info, "st_" + key) for key in sorted(fields)}

    @contextmanager
    def _deletion_scope(self, repository, subdomain):
        """Open only checked directory descriptors; never create a missing path."""
        scope_name = self._scope_name(repository, subdomain)
        with ExitStack() as stack:
            try:
                state = stack.enter_context(self._open_state_directory())
            except FileNotFoundError:
                yield None, None
                return
            try:
                root = self._open_child_directory(state, self.root.name, create=False)
            except FileNotFoundError:
                yield None, None
                return
            stack.callback(os.close, root)
            self._check_private(os.fstat(root), directory=True)
            try:
                scope = self._open_child_directory(root, scope_name, create=False)
            except FileNotFoundError:
                yield root, None
                return
            stack.callback(os.close, scope)
            self._check_private(os.fstat(scope), directory=True)
            yield root, scope

    def _scope_inventory(self, scope):
        items = {}
        names = sorted(os.listdir(scope))
        for name in names:
            if not _NAME.fullmatch(name):
                raise ValueError("unknown scope entry")
            descriptor = self._open_child_directory(scope, name, create=False)
            try:
                identity = self._deletion_identity(os.fstat(descriptor), directory=True)
                filenames = sorted(os.listdir(descriptor))
                files = {}
                for filename in filenames:
                    if not _SECRET_FILE.fullmatch(filename):
                        raise ValueError("unknown secret entry")
                    files[filename] = self._deletion_identity(
                        os.stat(filename, dir_fd=descriptor, follow_symlinks=False), directory=False
                    )
                if (filenames != sorted(os.listdir(descriptor))
                        or identity != self._deletion_identity(
                            os.stat(name, dir_fd=scope, follow_symlinks=False), directory=True)):
                    raise OSError("secret directory changed")
                items[name] = {"identity": identity, "files": files}
            finally:
                os.close(descriptor)
        if names != sorted(os.listdir(scope)):
            raise OSError("secret scope changed")
        return items

    def inventory_scope(self, repository, subdomain):
        """Snapshot metadata only. Caller must prove ownership and hold worker.lock.

        Every rotation, current pointer and abandoned temporary pointer is included.
        The returned plan is an immutable deletion journal, never a secret backup.
        """
        try:
            with self._deletion_scope(repository, subdomain) as (root, scope):
                plan = {"version": 1, "repository": repository, "subdomain": subdomain,
                        "root": None, "scope": None, "items": {}}
                if scope is not None:
                    plan["root"] = self._deletion_identity(os.fstat(root), directory=True)
                    plan["scope"] = self._deletion_identity(os.fstat(scope), directory=True)
                    plan["items"] = self._scope_inventory(scope)
                    current = os.stat(self._scope_name(repository, subdomain), dir_fd=root, follow_symlinks=False)
                    if plan["scope"] != self._deletion_identity(current, directory=True):
                        raise OSError("secret scope changed")
                return plan
        except (OSError, ValueError, TypeError):
            raise self._delete_conflict() from None

    def _validate_deletion_plan(self, plan):
        if (not isinstance(plan, dict)
                or set(plan) != {"version", "repository", "subdomain", "root", "scope", "items"}
                or type(plan["version"]) is not int or plan["version"] != 1
                or not isinstance(plan["items"], dict)):
            raise ValueError("invalid deletion plan")
        self._scope_name(plan["repository"], plan["subdomain"])

        def metadata(value, *, directory):
            fields = _DIRECTORY_IDENTITY if directory else _FILE_IDENTITY
            mode = stat.S_IFDIR | 0o700 if directory else stat.S_IFREG | 0o600
            if (not isinstance(value, dict) or set(value) != fields
                    or any(type(item) is not int for item in value.values())
                    or value["mode"] != mode or value["uid"] != os.getuid()
                    or value["ino"] <= 0 or value["dev"] < 0 or value["gid"] < 0
                    or not directory and (value["nlink"] != 1 or value["size"] < 0)):
                raise ValueError("invalid planned metadata")

        if plan["scope"] is None:
            if plan["root"] is not None or plan["items"]:
                raise ValueError("invalid absent scope")
            return
        metadata(plan["root"], directory=True)
        metadata(plan["scope"], directory=True)
        for name, item in plan["items"].items():
            if (not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(item, dict)
                    or set(item) != {"identity", "files"} or not isinstance(item["files"], dict)):
                raise ValueError("invalid planned directory")
            metadata(item["identity"], directory=True)
            for filename, identity in item["files"].items():
                if not isinstance(filename, str) or not _SECRET_FILE.fullmatch(filename):
                    raise ValueError("invalid planned filename")
                metadata(identity, directory=False)

    @staticmethod
    def _check_deletion_subset(actual, plan):
        if actual["scope"] is None:
            return
        if (actual["root"] != plan["root"] or actual["scope"] != plan["scope"]
                or actual["items"].keys() - plan["items"].keys()):
            raise ValueError("secret scope replaced or extended")
        for name, current in actual["items"].items():
            expected = plan["items"][name]
            if (current["identity"] != expected["identity"] or current["files"].keys() - expected["files"].keys()
                    or any(identity != expected["files"][filename] for filename, identity in current["files"].items())):
                raise ValueError("secret directory replaced or extended")

    def verify_deletion(self, plan):
        """Accept only the original tree or a deletion-only subset, without reading values."""
        try:
            self._validate_deletion_plan(plan)
            self._check_deletion_subset(self.inventory_scope(plan["repository"], plan["subdomain"]), plan)
        except (OSError, ValueError, TypeError, KeyError):
            raise self._delete_conflict() from None

    def delete_scope(self, plan):
        """Resume an authorized immutable plan under worker.lock; never follow links."""
        self.verify_deletion(plan)
        try:
            with self._deletion_scope(plan["repository"], plan["subdomain"]) as (root, scope):
                if scope is None:
                    # A prior attempt may have removed the scope before its final
                    # directory fsync failed. Confirm durability on that replay too.
                    if root is not None:
                        os.fsync(root)
                    return
                scope_name = self._scope_name(plan["repository"], plan["subdomain"])
                actual = {"root": self._deletion_identity(os.fstat(root), directory=True),
                          "scope": self._deletion_identity(os.fstat(scope), directory=True),
                          "items": self._scope_inventory(scope)}
                self._check_deletion_subset(actual, plan)
                for name in sorted(actual["items"]):
                    expected = plan["items"][name]
                    descriptor = self._open_child_directory(scope, name, create=False)
                    try:
                        if self._deletion_identity(os.fstat(descriptor), directory=True) != expected["identity"]:
                            raise OSError("secret directory replaced")
                        for filename in sorted(os.listdir(descriptor)):
                            identity = self._deletion_identity(
                                os.stat(filename, dir_fd=descriptor, follow_symlinks=False), directory=False
                            )
                            if identity != expected["files"].get(filename):
                                raise OSError("secret file replaced or added")
                            # Confirm the pinned directories still occupy their planned paths.
                            for parent, child, wanted in ((root, scope_name, plan["scope"]),
                                                          (scope, name, expected["identity"])):
                                current = os.stat(child, dir_fd=parent, follow_symlinks=False)
                                if self._deletion_identity(current, directory=True) != wanted:
                                    raise OSError("secret directory replaced")
                            os.unlink(filename, dir_fd=descriptor)
                            os.fsync(descriptor)
                        if os.listdir(descriptor):
                            raise OSError("secret directory has new contents")
                        current = os.stat(name, dir_fd=scope, follow_symlinks=False)
                        if self._deletion_identity(current, directory=True) != expected["identity"]:
                            raise OSError("secret directory replaced")
                        os.rmdir(name, dir_fd=scope)
                        os.fsync(scope)
                    finally:
                        os.close(descriptor)
                if os.listdir(scope):
                    raise OSError("secret scope has new contents")
                current = os.stat(scope_name, dir_fd=root, follow_symlinks=False)
                if self._deletion_identity(current, directory=True) != plan["scope"]:
                    raise OSError("secret scope replaced")
                os.rmdir(scope_name, dir_fd=root)
                os.fsync(root)
        except (OSError, ValueError, TypeError, KeyError):
            raise self._delete_conflict() from None

    @staticmethod
    def _raise_creation_conflict(state: str) -> None:
        if state == "configured":
            raise DeployError("SECRET_ALREADY_CONFIGURED", "秘密配置已经存在，不能通过此入口覆盖。")
        if state == "unavailable":
            raise DeployError("SECRET_UNAVAILABLE", "秘密配置存储不可用，请联系管理员检查。")

    def create_missing(self, repository: str, subdomain: str, name: str, value: str) -> str:
        """Publish a new immutable value only if no current pointer already exists."""
        self.validate_value(value)
        try:
            with self._open_secret_directory(repository, subdomain, name, create=True) as descriptor:
                self._raise_creation_conflict(self._inspect_directory(descriptor))
                version = uuid.uuid4().hex
                value_name, pointer_name = version + ".json", ".current-" + version
                created = []
                published = False
                try:
                    for filename, payload in ((value_name, {"version": version, "value": value}),
                                              (pointer_name, {"version": version})):
                        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                     | os.O_CLOEXEC, 0o600, dir_fd=descriptor)
                        created.append(filename)
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            self._check_private(os.fstat(handle.fileno()), directory=False)
                            json.dump(payload, handle)
                            handle.flush()
                            os.fsync(handle.fileno())
                    try:
                        # A hard link atomically publishes complete content and cannot replace a winner.
                        os.link(pointer_name, "current.json", src_dir_fd=descriptor, dst_dir_fd=descriptor,
                                follow_symlinks=False)
                    except FileExistsError:
                        state = self._inspect_directory(descriptor)
                        self._raise_creation_conflict(state if state != "missing" else "unavailable")
                    published = True
                    os.fsync(descriptor)
                    return version
                finally:
                    for filename in created:
                        if published and filename == value_name:
                            continue
                        try:
                            os.unlink(filename, dir_fd=descriptor)
                        except OSError:
                            pass
        except OSError:
            raise DeployError("SECRET_UNAVAILABLE", "秘密配置存储不可用，请联系管理员检查。") from None

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())

    def set(self, repository: str, subdomain: str, name: str, value: str) -> str:
        self.validate_value(value)
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
