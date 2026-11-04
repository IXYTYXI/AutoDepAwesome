"""Fixed, privileged XFS operations; no shell, caller paths, or archive extraction.

Requires Linux >= 5.14 on x86_64/aarch64, root, and an already mounted XFS pool.
ABI sources: Linux include/uapi/linux/{dqblk_xfs.h,fs.h},
include/uapi/asm-generic/unistd.h and fs/quota/quota.c (quotactl_fd).
No operation enables quotas, mounts a filesystem, or formats a device.
The service's admission lock must cover calls and all registered writers must
be stopped before delete. Parent directories and ledger are root controlled.
"""

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from .errors import QuotaError


FS_XFLAG_PROJINHERIT = 0x200
FS_IOC_FSGETXATTR = 0x801C581F
FS_IOC_FSSETXATTR = 0x401C5820
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIRFLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _DIRECTORY
_MARKER = ".identity.json"


class DiskQuota(ctypes.Structure):
    _fields_ = [
        ("d_version", ctypes.c_int8), ("d_flags", ctypes.c_int8),
        ("d_fieldmask", ctypes.c_uint16), ("d_id", ctypes.c_uint32),
        ("d_blk_hardlimit", ctypes.c_uint64), ("d_blk_softlimit", ctypes.c_uint64),
        ("d_ino_hardlimit", ctypes.c_uint64), ("d_ino_softlimit", ctypes.c_uint64),
        ("d_bcount", ctypes.c_uint64), ("d_icount", ctypes.c_uint64),
        ("d_itimer", ctypes.c_int32), ("d_btimer", ctypes.c_int32),
        ("d_iwarns", ctypes.c_uint16), ("d_bwarns", ctypes.c_uint16),
        ("d_itimer_hi", ctypes.c_int8), ("d_btimer_hi", ctypes.c_int8),
        ("d_rtbtimer_hi", ctypes.c_int8), ("d_padding2", ctypes.c_int8),
        ("d_rtb_hardlimit", ctypes.c_uint64), ("d_rtb_softlimit", ctypes.c_uint64),
        ("d_rtbcount", ctypes.c_uint64), ("d_rtbtimer", ctypes.c_int32),
        ("d_rtbwarns", ctypes.c_uint16), ("d_padding3", ctypes.c_int16),
        ("d_padding4", ctypes.c_char * 8),
    ]


class _FileStatV(ctypes.Structure):
    _fields_ = [
        ("ino", ctypes.c_uint64), ("nblks", ctypes.c_uint64),
        ("nextents", ctypes.c_uint32), ("pad", ctypes.c_uint32),
    ]


class QuotaStatV(ctypes.Structure):
    _fields_ = [
        ("qs_version", ctypes.c_int8), ("qs_pad1", ctypes.c_uint8),
        ("qs_flags", ctypes.c_uint16), ("qs_incoredqs", ctypes.c_uint32),
        ("qs_uquota", _FileStatV), ("qs_gquota", _FileStatV), ("qs_pquota", _FileStatV),
        ("qs_btimelimit", ctypes.c_int32), ("qs_itimelimit", ctypes.c_int32),
        ("qs_rtbtimelimit", ctypes.c_int32), ("qs_bwarnlimit", ctypes.c_uint16),
        ("qs_iwarnlimit", ctypes.c_uint16), ("qs_rtbwarnlimit", ctypes.c_uint16),
        ("qs_pad3", ctypes.c_uint16), ("qs_pad4", ctypes.c_uint32),
        ("qs_pad2", ctypes.c_uint64 * 7),
    ]


def _fail(code, message):
    raise QuotaError(code, message)


def _positive(value, maximum=(1 << 63) - 1):
    return type(value) is int and 0 < value <= maximum


def _protected(info):
    return info.st_uid == 0 and not (stat.S_IMODE(info.st_mode) & 0o022)


def _enforced(flags):
    if flags & 0x30 != 0x30:
        _fail("QUOTA_NOT_ENFORCED", "XFS project accounting and enforcement must both be enabled")


def _mapped_id(text, expected, runtime_id):
    try:
        rows = [[int(value) for value in line.split()] for line in text.splitlines() if line.strip()]
        if not rows or rows != expected or not _positive(runtime_id, (1 << 32) - 2):
            raise ValueError
        for row in rows:
            if len(row) != 3 or any(type(value) is not int for value in row):
                raise ValueError
            inside, outside, length = row
            if min(inside, outside) < 0 or length <= 0 or max(inside, outside) + length > (1 << 32) - 1:
                raise ValueError
        for index, row in enumerate(rows):
            for other in rows[index + 1:]:
                if any(max(row[axis], other[axis]) < min(
                    row[axis] + row[2], other[axis] + other[2],
                ) for axis in (0, 1)):
                    raise ValueError
        root_id = next(outside - inside for inside, outside, length in rows if inside == 0)
        for inside, outside, length in rows:
            if inside <= runtime_id < inside + length:
                mapped = outside + runtime_id - inside
                if mapped in (0, root_id):
                    raise ValueError
                return mapped
    except (ValueError, TypeError, StopIteration):
        pass
    _fail("QUOTA_MAPPING_MISMATCH", "runtime identity does not match the pinned daemon mapping")


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


class XFSBackend:
    def __init__(self, config):
        self.config = dict(config)
        for name in ("pool_mount", "pool_root", "pool_device", "rootless_daemon_pid_file"):
            value = self.config.get(name)
            if (not isinstance(value, str) or not value.startswith("/")
                    or os.path.normpath(value) != value or "\x00" in value):
                _fail("QUOTA_CONFIG_INVALID", f"{name} must be a normalized absolute path")
        mount, root = self.config["pool_mount"], self.config["pool_root"]
        if mount == "/" or not root.startswith(mount + "/"):
            _fail("QUOTA_CONFIG_INVALID", "pool_root must be below a dedicated pool_mount")
        try:
            if str(UUID(self.config["pool_uuid"])) != self.config["pool_uuid"]:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            _fail("QUOTA_CONFIG_INVALID", "pool_uuid must be a canonical UUID")
        for name in ("rootless_uid", "runtime_uid", "runtime_gid"):
            if not _positive(self.config.get(name), (1 << 32) - 2):
                _fail("QUOTA_CONFIG_INVALID", f"{name} must be a fixed nonzero ID")
        for name, runtime in (("expected_uid_map", "runtime_uid"), ("expected_gid_map", "runtime_gid")):
            rows = self.config.get(name)
            if not isinstance(rows, list):
                _fail("QUOTA_CONFIG_INVALID", f"{name} must pin the actual daemon mapping")
            try:
                text = "\n".join(" ".join(str(value) for value in row) for row in rows)
            except TypeError:
                _fail("QUOTA_CONFIG_INVALID", f"{name} is invalid")
            _mapped_id(text, rows, self.config[runtime])

    @staticmethod
    def _record(record):
        for name, length in (("storage_id", 64), ("generation", 32)):
            if not isinstance(record.get(name), str) or not re.fullmatch(
                rf"[0-9a-f]{{{length}}}", record[name],
            ):
                _fail("QUOTA_IDENTITY_INVALID", "invalid storage identity")
        if not _positive(record.get("project_id"), (1 << 32) - 2):
            _fail("QUOTA_IDENTITY_INVALID", "invalid allocated project ID")
        if not _positive(record.get("size_mib"), ((1 << 63) - 1) // 1048576):
            _fail("QUOTA_LIMIT_INVALID", "byte hard limit must be positive")
        if not _positive(record.get("max_inodes")):
            _fail("QUOTA_LIMIT_INVALID", "inode hard limit must be positive")

    @staticmethod
    def _runtime_supported():
        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
            _fail("QUOTA_UNAVAILABLE", "XFS helper requires Linux x86_64 or aarch64")
        if os.geteuid() != 0:
            _fail("QUOTA_UNAVAILABLE", "XFS helper requires the installed root entry point")

    @staticmethod
    def _mount_id(fd):
        try:
            data = Path(f"/proc/self/fdinfo/{fd}").read_text()
            return int(re.search(r"^mnt_id:\s*(\d+)$", data, re.MULTILINE)[1])
        except (OSError, ValueError, TypeError):
            _fail("QUOTA_UNAVAILABLE", "cannot read mount identity")

    def _open_root(self):
        self._runtime_supported()
        fd = os.open("/", _DIRFLAGS)
        try:
            for component in Path(self.config["pool_root"]).parts[1:]:
                child = os.open(component, _DIRFLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                if not _protected(os.fstat(fd)):
                    _fail("QUOTA_DIRECTORY_MISMATCH", "pool ancestors must be root owned and protected")
            return fd
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                _fail("QUOTA_DIRECTORY_MISMATCH", "pool directory is missing or replaced")
            _fail("QUOTA_UNAVAILABLE", "cannot open protected pool directory")
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _quota_call(fd, command, project_id, result):
        # quotactl_fd is syscall 443 on the supported architectures. Keeping a
        # filesystem FD pins the target even if a mount/device pathname changes.
        libc = ctypes.CDLL(None, use_errno=True)
        qcmd = (((ord("X") << 8) | command) << 8) | 2
        rc = libc.syscall(
            ctypes.c_long(443), ctypes.c_uint(fd), ctypes.c_uint(qcmd),
            ctypes.c_uint(project_id), ctypes.byref(result),
        )
        if rc != 0:
            _fail("QUOTA_UNAVAILABLE", "kernel project quota operation is unavailable")

    def _get_quota(self, fd, project_id):
        quota = DiskQuota()
        self._quota_call(fd, 3, project_id, quota)
        if quota.d_version != 1 or quota.d_flags != 2 or quota.d_id != project_id:
            _fail("QUOTA_UNAVAILABLE", "invalid kernel quota response")
        if (quota.d_blk_hardlimit % 2048 or quota.d_rtbcount
                or quota.d_blk_softlimit or quota.d_ino_softlimit):
            _fail("QUOTA_LIMIT_MISMATCH", "quota units, soft limits, or realtime allocation are unexpected")
        return {
            "size_mib": quota.d_blk_hardlimit // 2048,
            "max_inodes": quota.d_ino_hardlimit,
            "allocated_bytes": quota.d_bcount * 512,
            "used_inodes": quota.d_icount,
        }

    def _set_quota(self, fd, project_id, size_mib, max_inodes):
        quota = DiskQuota()
        quota.d_version, quota.d_flags, quota.d_id = 1, 2, project_id
        # Both hard limits and zero soft limits in one request. No global quota
        # changes, grace-time changes, accounting writes, or unlimited hard limits.
        quota.d_fieldmask = 0x0F
        quota.d_blk_hardlimit, quota.d_ino_hardlimit = size_mib * 2048, max_inodes
        self._quota_call(fd, 4, project_id, quota)

    @staticmethod
    def _get_attrs(fd):
        buffer = bytearray(28)
        try:
            fcntl.ioctl(fd, FS_IOC_FSGETXATTR, buffer, True)
        except OSError:
            _fail("QUOTA_UNAVAILABLE", "cannot read XFS project attributes")
        flags, _, _, project_id, _ = struct.unpack("=5I8x", buffer)
        return project_id, flags

    def _set_attrs(self, fd, project_id, flags):
        buffer = bytearray(28)
        try:
            fcntl.ioctl(fd, FS_IOC_FSGETXATTR, buffer, True)
            values = list(struct.unpack("=5I8x", buffer))
            values[0], values[3] = flags, project_id
            fcntl.ioctl(fd, FS_IOC_FSSETXATTR, struct.pack("=5I8x", *values))
        except OSError:
            _fail("QUOTA_UNAVAILABLE", "cannot set XFS project attributes")

    def _runtime_mapping(self):
        try:
            pidfd = os.open(self.config["rootless_daemon_pid_file"], os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
            with os.fdopen(pidfd) as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, self.config["rootless_uid"])
                        or stat.S_IMODE(info.st_mode) & 0o022):
                    _fail("QUOTA_MAPPING_MISMATCH", "daemon PID file is not protected")
                pidtext = handle.read(32).strip()
            if not re.fullmatch(r"[1-9][0-9]{0,9}", pidtext):
                _fail("QUOTA_MAPPING_MISMATCH", "daemon PID is invalid")
            proc = Path("/proc") / pidtext
            before = (proc / "stat").read_text()
            uid_text, gid_text = (proc / "uid_map").read_text(), (proc / "gid_map").read_text()
            status = (proc / "status").read_text()
            exe = os.readlink(proc / "exe")
            executable = os.stat(proc / "exe")
            after = (proc / "stat").read_text()
            # Field 22 (starttime), accounting for spaces/parentheses in comm.
            if before.rsplit(")", 1)[1].split()[19] != after.rsplit(")", 1)[1].split()[19]:
                _fail("QUOTA_UNAVAILABLE", "daemon changed while reading identity")
            uids = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", status, re.MULTILINE)
            if (not uids or any(int(value) != self.config["rootless_uid"] for value in uids.groups())
                    or Path(exe).name != "dockerd" or not _protected(executable)):
                _fail("QUOTA_MAPPING_MISMATCH", "PID is not the configured rootless Docker daemon")
            host_uid = _mapped_id(uid_text, self.config["expected_uid_map"], self.config["runtime_uid"])
            host_gid = _mapped_id(gid_text, self.config["expected_gid_map"], self.config["runtime_gid"])
            fingerprint = hashlib.sha256(json.dumps([
                self.config["expected_uid_map"], self.config["expected_gid_map"],
                self.config["runtime_uid"], self.config["runtime_gid"],
            ], separators=(",", ":")).encode()).hexdigest()
            return {"host_uid": host_uid, "host_gid": host_gid, "mapping_fingerprint": fingerprint}
        except (OSError, ValueError, IndexError):
            _fail("QUOTA_UNAVAILABLE", "cannot query the actual rootless daemon mapping")

    def _pool(self, fd):
        self._runtime_supported()
        try:
            mount_id = self._mount_id(fd)
            rows = Path("/proc/self/mountinfo").read_text().splitlines()
            matches = [line.split() for line in rows if line.split()[0] == str(mount_id)]
            if len(matches) != 1:
                _fail("QUOTA_POOL_MISMATCH", "pool mount is missing")
            row = matches[0]
            separator = row.index("-")
            if (row[separator + 1] != "xfs" or _unescape_mount(row[4]) != self.config["pool_mount"]
                    or _unescape_mount(row[3]) != "/" or "rw" not in row[5].split(",")):
                _fail("QUOTA_POOL_MISMATCH", "pool must be the configured writable XFS mount")
            device = os.stat(self.config["pool_device"], follow_symlinks=False)
            if not stat.S_ISBLK(device.st_mode):
                _fail("QUOTA_POOL_MISMATCH", "configured pool device is not a block device")
            expected_dev = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
            if row[2] != expected_dev or os.fstat(fd).st_dev != device.st_rdev:
                _fail("QUOTA_POOL_MISMATCH", "pool device identity changed")
            # Fixed binary/argv, no shell. -p bypasses blkid's cache.
            result = subprocess.run(
                ["/usr/sbin/blkid", "-p", "-s", "UUID", "-o", "value", self.config["pool_device"]],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, check=False,
            )
            if result.returncode:
                _fail("QUOTA_UNAVAILABLE", "cannot read the pool filesystem UUID")
            if result.stdout.strip().lower() != self.config["pool_uuid"]:
                _fail("QUOTA_POOL_MISMATCH", "pool filesystem UUID changed")
            state = QuotaStatV()
            state.qs_version = 1
            self._quota_call(fd, 8, 0, state)
            if state.qs_version != 1:
                _fail("QUOTA_UNAVAILABLE", "unsupported kernel quota status")
            _enforced(state.qs_flags)
            values = os.fstatvfs(fd)
            return {
                "mount_id": mount_id, "pool_uuid": self.config["pool_uuid"],
                "free_bytes": values.f_bavail * values.f_frsize,
                "free_inodes": values.f_favail, **self._runtime_mapping(),
            }
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            _fail("QUOTA_UNAVAILABLE", "cannot query the XFS pool")

    @contextlib.contextmanager
    def _root(self):
        fd = self._open_root()
        try:
            pool = self._pool(fd)
            project_id, flags = self._get_attrs(fd)
            if project_id or flags & (FS_XFLAG_PROJINHERIT | 0x101):
                _fail("QUOTA_PROJECT_MISMATCH", "pool metadata must not inherit a service or realtime project")
            yield fd, {**pool, "root_inode": os.fstat(fd).st_ino}
        finally:
            os.close(fd)

    def check_pool(self):
        with self._root() as (_, pool):
            return {**pool, "enforced": True, "checked_at": datetime.now(timezone.utc).isoformat()}

    def _name(self, record):
        return record["storage_id"] + "-" + record["generation"]

    def _identity(self, record, parentfd, payloadfd, pool):
        info = os.fstat(payloadfd)
        return {
            "path": os.path.join(self.config["pool_root"], self._name(record), "payload"),
            "device": info.st_dev, "inode": info.st_ino,
            "parent_inode": os.fstat(parentfd).st_ino,
            "root_inode": pool["root_inode"],
            "mapping_fingerprint": pool["mapping_fingerprint"],
        }

    def _base_marker(self, record, pool):
        return {
            "storage_id": record["storage_id"], "generation": record["generation"],
            "project_id": record["project_id"], "pool_uuid": self.config["pool_uuid"],
            "mapping_fingerprint": pool["mapping_fingerprint"],
        }

    @staticmethod
    def _read_marker(parentfd):
        fd = os.open(_MARKER, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=parentfd)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not _protected(info) or info.st_nlink != 1:
                _fail("QUOTA_DIRECTORY_MISMATCH", "generation marker is not protected")
            try:
                result = json.loads(handle.read(4097))
            except (ValueError, UnicodeError):
                _fail("QUOTA_DIRECTORY_MISMATCH", "generation marker is invalid")
            if not isinstance(result, dict):
                _fail("QUOTA_DIRECTORY_MISMATCH", "generation marker is invalid")
            return result

    @staticmethod
    def _write_marker(parentfd, marker):
        # A stale, root-controlled temp file from a crash may be overwritten.
        temporary = _MARKER + ".new"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                     0o600, dir_fd=parentfd)
        try:
            payload = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, _MARKER, src_dir_fd=parentfd, dst_dir_fd=parentfd)
        os.fsync(parentfd)

    def _open_child(self, parentfd, name, pool, protected=False):
        fd = os.open(name, _DIRFLAGS, dir_fd=parentfd)
        try:
            if self._mount_id(fd) != pool["mount_id"]:
                _fail("QUOTA_DIRECTORY_MISMATCH", "unexpected nested mount")
            if protected and not _protected(os.fstat(fd)):
                _fail("QUOTA_DIRECTORY_MISMATCH", "generation directory must be root protected")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextlib.contextmanager
    def _instance(self, rootfd, record, pool, missing_payload=False):
        parentfd = self._open_child(rootfd, self._name(record), pool, protected=True)
        payloadfd = None
        try:
            expected = record.get("directory")
            if (not isinstance(expected, dict)
                    or expected.get("parent_inode") != os.fstat(parentfd).st_ino):
                _fail("QUOTA_DIRECTORY_MISMATCH", "recorded directory parent changed")
            try:
                marker = self._read_marker(parentfd)
            except FileNotFoundError:
                # Recovery for a crash between unlink(marker) and rmdir(parent).
                # Only a known, identical, entirely empty protected parent is safe.
                if missing_payload and not os.listdir(parentfd):
                    yield parentfd, None
                    return
                raise
            if any(marker.get(key) != value for key, value in self._base_marker(record, pool).items()):
                _fail("QUOTA_DIRECTORY_MISMATCH", "generation identity changed")
            if not isinstance(expected, dict) or marker.get("directory") != expected:
                _fail("QUOTA_DIRECTORY_MISMATCH", "recorded directory identity changed")
            if (expected.get("parent_inode") != os.fstat(parentfd).st_ino
                    or expected.get("path") != os.path.join(self.config["pool_root"], self._name(record), "payload")):
                _fail("QUOTA_DIRECTORY_MISMATCH", "directory parent or path changed")
            try:
                payloadfd = self._open_child(parentfd, "payload", pool)
            except FileNotFoundError:
                if not missing_payload:
                    raise
            if payloadfd is not None:
                if self._identity(record, parentfd, payloadfd, pool) != expected:
                    _fail("QUOTA_DIRECTORY_MISMATCH", "payload directory was replaced")
                info = os.fstat(payloadfd)
                if (info.st_uid != pool["host_uid"] or info.st_gid != pool["host_gid"]
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    _fail("QUOTA_MAPPING_MISMATCH", "payload ownership or permissions changed")
            yield parentfd, payloadfd
        finally:
            if payloadfd is not None:
                os.close(payloadfd)
            os.close(parentfd)

    @contextlib.contextmanager
    def _errors(self):
        try:
            yield
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EXDEV):
                _fail("QUOTA_DIRECTORY_MISMATCH", "storage directory is missing, replaced, or mounted over")
            _fail("QUOTA_UNAVAILABLE", "storage filesystem operation failed")

    def _verify_attrs(self, payloadfd, project_id):
        actual_project, flags = self._get_attrs(payloadfd)
        if actual_project != project_id or not flags & FS_XFLAG_PROJINHERIT or flags & 0x101:
            _fail("QUOTA_PROJECT_MISMATCH", "project ownership or inheritance changed")

    def create(self, record):
        self._record(record)
        if record.get("directory") is not None:
            self.inspect(record)
            return record["directory"]
        with self._errors(), self._root() as (rootfd, pool):
            name = self._name(record)
            try:
                os.mkdir(name, 0o700, dir_fd=rootfd)
                os.fsync(rootfd)
            except FileExistsError:
                pass
            parentfd = self._open_child(rootfd, name, pool, protected=True)
            try:
                marker = None
                try:
                    marker = self._read_marker(parentfd)
                except FileNotFoundError:
                    pass
                base = self._base_marker(record, pool)
                if marker and any(marker.get(key) != value for key, value in base.items()):
                    _fail("QUOTA_DIRECTORY_MISMATCH", "generation identity changed")
                entries = set(os.listdir(parentfd))
                # The parent is inaccessible to runtime until initialized; only
                # an empty root-owned partial payload is recoverable without ID.
                if not marker or "directory" not in marker:
                    if entries - {"payload", _MARKER, _MARKER + ".new"}:
                        _fail("QUOTA_DIRECTORY_MISMATCH", "unrecognized partial initialization")
                    if _MARKER + ".new" in entries:
                        temp = os.stat(_MARKER + ".new", dir_fd=parentfd, follow_symlinks=False)
                        if not stat.S_ISREG(temp.st_mode) or not _protected(temp) or temp.st_nlink != 1:
                            _fail("QUOTA_DIRECTORY_MISMATCH", "unsafe interrupted marker")
                        os.unlink(_MARKER + ".new", dir_fd=parentfd)
                    try:
                        os.mkdir("payload", 0o700, dir_fd=parentfd)
                        os.fsync(parentfd)
                    except FileExistsError:
                        pass
                    payloadfd = self._open_child(parentfd, "payload", pool)
                    try:
                        if not _protected(os.fstat(payloadfd)) or os.listdir(payloadfd):
                            _fail("QUOTA_DIRECTORY_MISMATCH", "partial initialization is not empty and protected")
                        project_id, flags = self._get_attrs(payloadfd)
                        if project_id not in (0, record["project_id"]) or flags & 0x101:
                            _fail("QUOTA_PROJECT_MISMATCH", "partial payload belongs to a different project")
                        self._set_attrs(payloadfd, record["project_id"], flags | FS_XFLAG_PROJINHERIT)
                        actual = self._get_quota(rootfd, record["project_id"])
                        if actual["used_inodes"] > 1:
                            _fail("QUOTA_PROJECT_MISMATCH", "new project already accounts for unrelated inodes")
                        if (actual["size_mib"] not in (0, record["size_mib"])
                                or actual["max_inodes"] not in (0, record["max_inodes"])):
                            _fail("QUOTA_LIMIT_MISMATCH", "partial project has unexpected limits")
                        self._set_quota(rootfd, record["project_id"], record["size_mib"], record["max_inodes"])
                        marker = {**base, "directory": self._identity(record, parentfd, payloadfd, pool)}
                        self._write_marker(parentfd, marker)
                    finally:
                        os.close(payloadfd)
                payloadfd = self._open_child(parentfd, "payload", pool)
                try:
                    if self._identity(record, parentfd, payloadfd, pool) != marker["directory"]:
                        _fail("QUOTA_DIRECTORY_MISMATCH", "initialized payload was replaced")
                    self._verify_attrs(payloadfd, record["project_id"])
                    actual = self._get_quota(rootfd, record["project_id"])
                    if (actual["size_mib"], actual["max_inodes"]) != (record["size_mib"], record["max_inodes"]):
                        _fail("QUOTA_LIMIT_MISMATCH", "initial hard limits did not take effect")
                    info = os.fstat(payloadfd)
                    if (info.st_uid, info.st_gid) != (pool["host_uid"], pool["host_gid"]):
                        if not _protected(info) or os.listdir(payloadfd):
                            _fail("QUOTA_MAPPING_MISMATCH", "initialized payload ownership changed")
                        os.fchown(payloadfd, pool["host_uid"], pool["host_gid"])
                    os.fchmod(payloadfd, 0o700)
                    os.fsync(payloadfd)
                    os.fchmod(parentfd, 0o711)
                    os.fsync(parentfd)
                    return marker["directory"]
                finally:
                    os.close(payloadfd)
            finally:
                os.close(parentfd)

    def inspect(self, record):
        self._record(record)
        with self._errors(), self._root() as (rootfd, pool):
            with self._instance(rootfd, record, pool) as (_, payloadfd):
                self._verify_attrs(payloadfd, record["project_id"])
                actual = self._get_quota(rootfd, record["project_id"])
                if not actual["size_mib"] or not actual["max_inodes"]:
                    _fail("QUOTA_LIMIT_MISMATCH", "unlimited quota is forbidden")
                return {**actual, "checked_at": datetime.now(timezone.utc).isoformat()}

    def set_limits(self, record, size_mib, max_inodes):
        self._record({**record, "size_mib": size_mib, "max_inodes": max_inodes})
        with self._errors(), self._root() as (rootfd, pool):
            with self._instance(rootfd, record, pool) as (_, payloadfd):
                self._verify_attrs(payloadfd, record["project_id"])
                old = self._get_quota(rootfd, record["project_id"])
                if not old["size_mib"] or not old["max_inodes"]:
                    _fail("QUOTA_LIMIT_MISMATCH", "existing quota is unlimited")
                if old["size_mib"] > size_mib or old["max_inodes"] > max_inodes:
                    _fail("QUOTA_LIMIT_MISMATCH", "hard limits cannot be reduced")
                self._set_quota(rootfd, record["project_id"], size_mib, max_inodes)
                actual = self._get_quota(rootfd, record["project_id"])
                if (actual["size_mib"], actual["max_inodes"]) != (size_mib, max_inodes):
                    _fail("QUOTA_UNAVAILABLE", "quota update has not been confirmed")

    def _remove_contents(self, fd, pool):
        # Keep only the original root and current directory pinned, regardless
        # of depth. Reopen ancestors from the pinned root with nofollow and
        # recorded inode/mount checks before postorder removal; never use '..'.
        def identity(info):
            return info.st_dev, info.st_ino

        def reopen_parent(frames):
            parent = os.dup(fd)
            try:
                for frame in frames[1:-1]:
                    child = self._open_child(parent, frame[0], pool)
                    os.close(parent)
                    parent = child
                    if identity(os.fstat(parent)) != frame[1]:
                        _fail("QUOTA_DIRECTORY_MISMATCH", "ancestor changed during deletion")
                return parent
            except BaseException:
                os.close(parent)
                raise

        current = os.dup(fd)
        frames = [(None, identity(os.fstat(current)), iter(os.listdir(current)))]
        try:
            while frames:
                name = next(frames[-1][2], None)
                if name is None:
                    os.fsync(current)
                    if len(frames) == 1:
                        break
                    parent = reopen_parent(frames)
                    try:
                        name, expected, _ = frames[-1]
                        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if identity(after) != expected:
                            _fail("QUOTA_DIRECTORY_MISMATCH", "directory changed during deletion")
                        os.rmdir(name, dir_fd=parent)
                    except BaseException:
                        os.close(parent)
                        raise
                    os.close(current)
                    current = parent
                    frames.pop()
                    continue
                before = os.stat(name, dir_fd=current, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = self._open_child(current, name, pool)
                    try:
                        if identity(os.fstat(child)) != identity(before):
                            _fail("QUOTA_DIRECTORY_MISMATCH", "directory changed during deletion")
                        frame = (name, identity(before), iter(os.listdir(child)))
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(current)
                    current = child
                    frames.append(frame)
                else:
                    # Symlinks themselves are removed; their targets are never read.
                    if before.st_dev != os.fstat(current).st_dev:
                        _fail("QUOTA_DIRECTORY_MISMATCH", "unexpected nested file mount")
                    if not stat.S_ISLNK(before.st_mode):
                        flags = getattr(os, "O_PATH", os.O_RDONLY) | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK
                        child = os.open(name, flags, dir_fd=current)
                        try:
                            opened = os.fstat(child)
                            if (self._mount_id(child) != pool["mount_id"]
                                    or identity(opened) != identity(before)):
                                _fail("QUOTA_DIRECTORY_MISMATCH", "file changed or is mounted over")
                        finally:
                            os.close(child)
                    os.unlink(name, dir_fd=current)
        finally:
            os.close(current)

    def delete(self, record):
        self._record(record)
        if not isinstance(record.get("directory"), dict):
            _fail("QUOTA_DIRECTORY_MISMATCH", "deletion requires the original recorded directory identity")
        with self._errors(), self._root() as (rootfd, pool):
            name = self._name(record)
            expected = record["directory"]
            if (expected.get("path") != os.path.join(self.config["pool_root"], name, "payload")
                    or expected.get("mapping_fingerprint") != pool["mapping_fingerprint"]
                    or expected.get("root_inode") != pool["root_inode"]
                    or expected.get("device") != os.fstat(rootfd).st_dev
                    or not _positive(expected.get("inode"))
                    or not _positive(expected.get("parent_inode"))):
                _fail("QUOTA_DIRECTORY_MISMATCH", "original directory identity does not match the pool")
            try:
                # Missing parent is safe only with a trusted ledger identity.
                os.stat(name, dir_fd=rootfd, follow_symlinks=False)
            except FileNotFoundError:
                actual = self._get_quota(rootfd, record["project_id"])
                return actual["allocated_bytes"] == actual["used_inodes"] == 0
            with self._instance(rootfd, record, pool, missing_payload=True) as (parentfd, payloadfd):
                if payloadfd is not None:
                    self._verify_attrs(payloadfd, record["project_id"])
                    self._remove_contents(payloadfd, pool)
                    now = os.stat("payload", dir_fd=parentfd, follow_symlinks=False)
                    if (now.st_dev, now.st_ino) != (
                        record["directory"]["device"], record["directory"]["inode"],
                    ):
                        _fail("QUOTA_DIRECTORY_MISMATCH", "payload changed during deletion")
                    os.rmdir("payload", dir_fd=parentfd)
                    os.fsync(parentfd)
            # All FDs to the payload must be closed before quota inode readback.
            actual = self._get_quota(rootfd, record["project_id"])
            if actual["allocated_bytes"] or actual["used_inodes"]:
                return False
            parentfd = self._open_child(rootfd, name, pool, protected=True)
            try:
                if os.fstat(parentfd).st_ino != record["directory"]["parent_inode"]:
                    _fail("QUOTA_DIRECTORY_MISMATCH", "generation directory was replaced")
                try:
                    os.unlink(_MARKER, dir_fd=parentfd)
                except FileNotFoundError:
                    if os.listdir(parentfd):
                        _fail("QUOTA_DIRECTORY_MISMATCH", "generation marker disappeared")
                os.fsync(parentfd)
                os.rmdir(name, dir_fd=rootfd)
                os.fsync(rootfd)
            finally:
                os.close(parentfd)
            return True
