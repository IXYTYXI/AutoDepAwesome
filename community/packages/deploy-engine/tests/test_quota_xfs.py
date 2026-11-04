"""The native quota/ownership boundary is faked; directory operations are real.

These tests are intentionally runnable without root, XFS, Linux, or Docker.
They are not the privileged Linux acceptance suite.
"""

import ctypes
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_xfs_adapter_is_implemented():
    source = Path(__file__).parents[1] / "ops/storage_quota/xfs.py"
    assert source.is_file(), "the real XFS backend is missing"


@pytest.fixture
def xfs():
    assert importlib.util.find_spec("ops.storage_quota.xfs"), "XFS backend is missing"
    from ops.storage_quota import xfs

    return xfs


@pytest.fixture
def record():
    return {
        "storage_id": "a" * 64,
        "generation": "b" * 32,
        "project_id": 1001,
        "size_mib": 8,
        "max_inodes": 100,
    }


@pytest.fixture
def backend(xfs, tmp_path, monkeypatch):
    root = tmp_path / "pool"
    root.mkdir(mode=0o711)
    config = {
        "pool_mount": str(tmp_path),
        "pool_root": str(root),
        "pool_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "pool_device": "/dev/loop99",
        "rootless_daemon_pid_file": "/run/user/1000/docker.pid",
        "rootless_uid": 1000,
        "runtime_uid": 65532,
        "runtime_gid": 65532,
        "expected_uid_map": [[0, 1000, 1], [1, 100000, 65536]],
        "expected_gid_map": [[0, 1000, 1], [1, 100000, 65536]],
    }
    result = xfs.XFSBackend(config)
    attrs = {}
    quotas = {}

    def get_attrs(fd):
        return attrs.get(os.fstat(fd).st_ino, (0, 0))

    def set_attrs(fd, project_id, flags):
        attrs[os.fstat(fd).st_ino] = (project_id, flags)

    def query(fd, project_id):
        return dict(quotas.get(project_id, {
            "size_mib": 0, "max_inodes": 0,
            "allocated_bytes": 0, "used_inodes": 0,
        }))

    def limits(fd, project_id, size_mib, max_inodes):
        existing = query(fd, project_id)
        existing.update(size_mib=size_mib, max_inodes=max_inodes)
        quotas[project_id] = existing

    monkeypatch.setattr(result, "_pool", lambda fd: {
        "mount_id": 42, "mapping_fingerprint": "map-v1",
        "host_uid": os.getuid(), "host_gid": os.getgid(),
        "free_bytes": 10**10, "free_inodes": 100000,
    })
    monkeypatch.setattr(result, "_mount_id", lambda fd: 42)
    monkeypatch.setattr(result, "_open_root", lambda: os.open(root, os.O_RDONLY))
    monkeypatch.setattr(xfs, "_protected", lambda s: not (stat.S_IMODE(s.st_mode) & 0o022))
    monkeypatch.setattr(xfs.os, "fchown", lambda *args: None)
    monkeypatch.setattr(result, "_get_attrs", get_attrs)
    monkeypatch.setattr(result, "_set_attrs", set_attrs)
    monkeypatch.setattr(result, "_get_quota", query)
    monkeypatch.setattr(result, "_set_quota", limits)
    result.test_attrs = attrs
    result.test_quotas = quotas
    return result


def test_kernel_structures_match_linux_uapi(xfs):
    assert ctypes.sizeof(xfs.DiskQuota) == 112
    assert ctypes.sizeof(xfs.QuotaStatV) == 160
    assert xfs.DiskQuota.d_blk_hardlimit.offset == 8
    assert xfs.QuotaStatV.qs_pquota.offset == 56
    assert xfs.FS_IOC_FSGETXATTR == 0x801C581F
    assert xfs.FS_IOC_FSSETXATTR == 0x401C5820


@pytest.mark.parametrize("flags", [0, 0x10, 0x20])
def test_accounting_and_enforcement_are_both_required(xfs, flags):
    with pytest.raises(xfs.QuotaError, match="enforcement"):
        xfs._enforced(flags)


def test_uid_maps_are_exact_and_runtime_id_does_not_map_to_daemon(xfs):
    expected = [[0, 1000, 1], [1, 100000, 65536]]
    assert xfs._mapped_id("0 1000 1\n1 100000 65536\n", expected, 65532) == 165531
    for text in ("0 1000 1\n1 200000 65536\n", "0 0 4294967295\n"):
        with pytest.raises(xfs.QuotaError) as raised:
            xfs._mapped_id(text, expected, 65532)
        assert raised.value.code == "QUOTA_MAPPING_MISMATCH"
    with pytest.raises(xfs.QuotaError):
        xfs._mapped_id("0 1000 1\n1 100000 65536\n", expected, 0)


@pytest.mark.parametrize("field,value", [
    ("storage_id", "../escape"), ("generation", "x" * 32),
    ("project_id", 0), ("size_mib", 0), ("max_inodes", 0),
    ("size_mib", True),
])
def test_invalid_record_never_touches_disk(backend, record, field, value):
    record[field] = value
    with pytest.raises(Exception):
        backend.create(record)
    assert list(Path(backend.config["pool_root"]).iterdir()) == []


def test_creation_is_empty_private_and_idempotent(backend, record):
    directory = backend.create(record)
    payload = Path(directory["path"])
    assert list(payload.iterdir()) == []
    assert payload.name == "payload"
    assert stat.S_IMODE(payload.stat().st_mode) == 0o700
    assert stat.S_IMODE(payload.parent.stat().st_mode) == 0o711
    assert backend.create(record) == directory
    record["directory"] = directory
    data = backend.inspect(record)
    assert data["size_mib"] == 8
    assert data["max_inodes"] == 100
    assert data["checked_at"]


def test_creation_recovers_a_root_owned_empty_generation_directory(backend, record):
    parent = Path(backend.config["pool_root"]) / (record["storage_id"] + "-" + record["generation"])
    parent.mkdir(mode=0o700)
    assert backend.create(record)["path"] == str(parent / "payload")


def test_missing_existing_payload_is_never_recreated(backend, record, xfs):
    record["directory"] = backend.create(record)
    Path(record["directory"]["path"]).rmdir()
    with pytest.raises(xfs.QuotaError) as raised:
        backend.create(record)
    assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"
    assert not Path(record["directory"]["path"]).exists()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_replaced_payload_is_refused(backend, record, tmp_path, xfs, replacement):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    payload.rename(payload.with_name("original"))
    if replacement == "symlink":
        payload.symlink_to(tmp_path, target_is_directory=True)
    else:
        payload.mkdir()
    for method in (backend.inspect, backend.delete):
        with pytest.raises(xfs.QuotaError) as raised:
            method(record)
        assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"


def test_changed_generation_marker_is_refused(backend, record, xfs):
    record["directory"] = backend.create(record)
    marker = Path(record["directory"]["path"]).parent / ".identity.json"
    document = json.loads(marker.read_text())
    document["generation"] = "c" * 32
    marker.write_text(json.dumps(document))
    with pytest.raises(xfs.QuotaError) as raised:
        backend.inspect(record)
    assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"


def test_nonzero_effective_limits_and_project_inheritance_are_required(backend, record, xfs):
    record["directory"] = backend.create(record)
    inode = Path(record["directory"]["path"]).stat().st_ino
    backend.test_attrs[inode] = (record["project_id"], 0)
    with pytest.raises(xfs.QuotaError) as raised:
        backend.inspect(record)
    assert raised.value.code == "QUOTA_PROJECT_MISMATCH"
    backend.test_attrs[inode] = (record["project_id"], xfs.FS_XFLAG_PROJINHERIT)
    backend.test_quotas[record["project_id"]]["size_mib"] = 0
    with pytest.raises(xfs.QuotaError) as raised:
        backend.inspect(record)
    assert raised.value.code == "QUOTA_LIMIT_MISMATCH"


def test_expand_preserves_larger_partial_kernel_limit_and_never_shrinks(backend, record, xfs):
    record["directory"] = backend.create(record)
    backend.test_quotas[record["project_id"]]["size_mib"] = 16
    with pytest.raises(xfs.QuotaError):
        backend.set_limits(record, 8, 200)
    backend.set_limits(record, 16, 200)
    assert backend.inspect(record)["max_inodes"] == 200


def test_delete_unlinks_symlinks_without_following_and_waits_for_open_file_usage(
    backend, record, tmp_path,
):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    (payload / "link").symlink_to(outside)
    (payload / "sub").mkdir()
    (payload / "sub" / "data").write_bytes(b"data")
    quota = backend.test_quotas[record["project_id"]]
    quota.update(allocated_bytes=4096, used_inodes=1)
    assert backend.delete(record) is False
    assert not payload.exists()
    assert (outside / "keep").read_text() == "safe"
    quota.update(allocated_bytes=0, used_inodes=0)
    assert backend.delete(record) is True
    assert backend.delete(record) is True


def test_unknown_query_failure_cannot_release_storage(backend, record, monkeypatch, xfs):
    record["directory"] = backend.create(record)

    def unavailable(*args):
        raise xfs.QuotaError("QUOTA_UNAVAILABLE", "kernel query unavailable")

    monkeypatch.setattr(backend, "_get_quota", unavailable)
    with pytest.raises(xfs.QuotaError) as raised:
        backend.delete(record)
    assert raised.value.code == "QUOTA_UNAVAILABLE"


def test_delete_recovers_crash_after_generation_marker_unlink(backend, record):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    parent = payload.parent
    payload.rmdir()
    (parent / ".identity.json").unlink()
    assert backend.delete(record) is True
    assert not parent.exists()


def test_delete_missing_directory_still_checks_original_record_path(backend, record, xfs):
    record["directory"] = backend.create(record)
    assert backend.delete(record)
    record["directory"]["path"] = "/unrelated/path"
    with pytest.raises(xfs.QuotaError) as raised:
        backend.delete(record)
    assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"


def test_same_device_nested_mount_blocks_recursive_delete(backend, record, monkeypatch, xfs):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    mounted = payload / "mounted"
    mounted.write_text("belongs to another mount")
    inode = mounted.stat().st_ino
    monkeypatch.setattr(backend, "_mount_id", lambda fd: 43 if os.fstat(fd).st_ino == inode else 42)
    with pytest.raises(xfs.QuotaError) as raised:
        backend.delete(record)
    assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"
    assert mounted.exists()


def test_pool_query_validates_real_mount_device_uuid_and_flags(
    backend, monkeypatch, xfs,
):
    root = Path(backend.config["pool_root"])
    device = root.stat().st_dev
    mount = backend.config["pool_mount"]
    state = {"fstype": "xfs", "uuid": backend.config["pool_uuid"], "flags": 0x30}
    original_read = Path.read_text
    original_stat = os.stat

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return f"42 1 {os.major(device)}:{os.minor(device)} / {mount} rw - {state['fstype']} /dev/loop99 rw,prjquota\n"
        return original_read(path, *args, **kwargs)

    def read_stat(path, *args, **kwargs):
        if path == backend.config["pool_device"]:
            return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=device)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(os, "stat", read_stat)
    monkeypatch.setattr(backend, "_runtime_supported", lambda: None)
    monkeypatch.setattr(backend, "_runtime_mapping", lambda: {"mapping_fingerprint": "map"})
    monkeypatch.setattr(xfs.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(
        returncode=0, stdout=state["uuid"],
    ))
    monkeypatch.setattr(backend, "_quota_call", lambda fd, cmd, pid, value: setattr(
        value, "qs_flags", state["flags"],
    ))
    fd = os.open(root, os.O_RDONLY)
    try:
        result = xfs.XFSBackend._pool(backend, fd)
        assert result["free_bytes"] > 0
        state["fstype"] = "ext4"
        with pytest.raises(xfs.QuotaError) as raised:
            xfs.XFSBackend._pool(backend, fd)
        assert raised.value.code == "QUOTA_POOL_MISMATCH"
        state["fstype"] = "xfs"
        state["uuid"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        with pytest.raises(xfs.QuotaError) as raised:
            xfs.XFSBackend._pool(backend, fd)
        assert raised.value.code == "QUOTA_POOL_MISMATCH"
        state["uuid"] = backend.config["pool_uuid"]
        state["flags"] = 0x10
        with pytest.raises(xfs.QuotaError) as raised:
            xfs.XFSBackend._pool(backend, fd)
        assert raised.value.code == "QUOTA_NOT_ENFORCED"
    finally:
        os.close(fd)


def test_native_quota_conversion_and_dual_hardlimit_request(backend, monkeypatch, xfs):
    requests = []

    def quota_call(fd, command, project_id, quota):
        if command == 3:
            quota.d_version, quota.d_flags, quota.d_id = 1, 2, project_id
            quota.d_blk_hardlimit, quota.d_ino_hardlimit = 16384, 100
            quota.d_bcount, quota.d_icount = 9, 4
        else:
            requests.append((command, project_id, quota.d_fieldmask,
                             quota.d_blk_hardlimit, quota.d_ino_hardlimit,
                             quota.d_blk_softlimit, quota.d_ino_softlimit))

    monkeypatch.setattr(backend, "_quota_call", quota_call)
    assert xfs.XFSBackend._get_quota(backend, 5, 1001) == {
        "size_mib": 8, "max_inodes": 100, "allocated_bytes": 4608, "used_inodes": 4,
    }
    xfs.XFSBackend._set_quota(backend, 5, 1001, 16, 200)
    assert requests == [(4, 1001, 15, 32768, 200, 0, 0)]


def test_native_mapping_reads_running_daemon_and_detects_changed_uid_map(
    backend, monkeypatch, xfs, tmp_path,
):
    uid = os.getuid()
    pidfile = tmp_path / "docker.pid"
    pidfile.write_text("12345\n")
    pidfile.chmod(0o600)
    backend.config["rootless_daemon_pid_file"] = str(pidfile)
    backend.config["rootless_uid"] = uid
    backend.config["expected_uid_map"][0][1] = uid
    state = {"map": f"0 {uid} 1\n1 100000 65536\n", "exe": "/usr/bin/dockerd"}
    original_read, original_stat = Path.read_text, os.stat

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/12345/"):
            return {
                "stat": "12345 (dockerd) " + " ".join(["S"] + ["0"] * 18 + ["456"]),
                "status": f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
                "uid_map": state["map"],
                "gid_map": "0 1000 1\n1 100000 65536\n",
            }[path.name]
        return original_read(path, *args, **kwargs)

    def read_stat(path, *args, **kwargs):
        if str(path) == "/proc/12345/exe":
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(os, "stat", read_stat)
    monkeypatch.setattr(os, "readlink", lambda path: state["exe"])
    actual = backend._runtime_mapping()
    assert actual["host_uid"] == actual["host_gid"] == 165531
    assert len(actual["mapping_fingerprint"]) == 64
    state["map"] = f"0 {uid} 1\n1 200000 65536\n"
    with pytest.raises(xfs.QuotaError) as raised:
        backend._runtime_mapping()
    assert raised.value.code == "QUOTA_MAPPING_MISMATCH"
    state["map"] = f"0 {uid} 1\n1 100000 65536\n"
    state["exe"] = "/usr/bin/unrelated"
    with pytest.raises(xfs.QuotaError) as raised:
        backend._runtime_mapping()
    assert raised.value.code == "QUOTA_MAPPING_MISMATCH"


def test_replaced_pool_root_is_detected_even_if_original_instance_is_moved(
    backend, record, xfs,
):
    record["directory"] = backend.create(record)
    root = Path(backend.config["pool_root"])
    original = root.with_name("original-pool")
    root.rename(original)
    root.mkdir()
    for item in original.iterdir():
        item.rename(root / item.name)
    with pytest.raises(xfs.QuotaError) as raised:
        backend.inspect(record)
    assert raised.value.code == "QUOTA_DIRECTORY_MISMATCH"


def test_nonzero_softlimits_are_not_silently_accepted(backend, monkeypatch, xfs):
    def quota_call(fd, command, project_id, quota):
        quota.d_version, quota.d_flags, quota.d_id = 1, 2, project_id
        quota.d_blk_hardlimit, quota.d_ino_hardlimit = 16384, 100
        quota.d_blk_softlimit = 2048

    monkeypatch.setattr(backend, "_quota_call", quota_call)
    with pytest.raises(xfs.QuotaError) as raised:
        xfs.XFSBackend._get_quota(backend, 5, 1001)
    assert raised.value.code == "QUOTA_LIMIT_MISMATCH"


def test_protected_directory_policy_requires_root_and_no_writable_group(xfs):
    assert xfs._protected(SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o711))
    assert not xfs._protected(SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700))
    assert not xfs._protected(SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o770))


def test_pool_metadata_never_inherits_a_service_project(backend, monkeypatch, xfs):
    monkeypatch.setattr(backend, "_get_attrs", lambda fd: (1001, xfs.FS_XFLAG_PROJINHERIT))
    with pytest.raises(xfs.QuotaError) as raised:
        backend.check_pool()
    assert raised.value.code == "QUOTA_PROJECT_MISMATCH"


def test_successful_pool_query_exposes_verified_enforcement(backend):
    assert backend.check_pool()["enforced"] is True


def test_create_resumes_after_crash_between_project_assignment_and_marker_commit(
    backend, record, monkeypatch,
):
    write_marker = backend._write_marker
    monkeypatch.setattr(backend, "_write_marker", lambda *args: (_ for _ in ()).throw(
        RuntimeError("simulated crash"),
    ))
    with pytest.raises(RuntimeError, match="simulated crash"):
        backend.create(record)
    monkeypatch.setattr(backend, "_write_marker", write_marker)
    directory = backend.create(record)
    record["directory"] = directory
    assert backend.inspect(record)["size_mib"] == 8


def test_create_resumes_after_durable_marker_before_payload_ownership(
    backend, record, monkeypatch,
):
    chmod = os.fchmod
    monkeypatch.setattr(os, "fchmod", lambda *args: (_ for _ in ()).throw(
        RuntimeError("simulated crash"),
    ))
    with pytest.raises(RuntimeError, match="simulated crash"):
        backend.create(record)
    monkeypatch.setattr(os, "fchmod", chmod)
    directory = backend.create(record)
    record["directory"] = directory
    assert backend.inspect(record)["max_inodes"] == 100


def test_new_project_cannot_capture_unrelated_existing_files(backend, record, xfs):
    backend.test_quotas[record["project_id"]] = {
        "size_mib": 0, "max_inodes": 0, "allocated_bytes": 4096, "used_inodes": 9,
    }
    with pytest.raises(xfs.QuotaError) as raised:
        backend.create(record)
    assert raised.value.code == "QUOTA_PROJECT_MISMATCH"
    assert backend.test_quotas[record["project_id"]]["size_mib"] == 0


def test_quotactl_fd_command_is_fixed_and_native_errors_are_unknown(xfs, monkeypatch):
    calls = []

    def syscall(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(syscall=syscall))
    xfs.XFSBackend._quota_call(17, 3, 1001, xfs.DiskQuota())
    assert [arg.value for arg in calls[0][:4]] == [443, 17, 0x580302, 1001]
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(syscall=lambda *args: -1))
    with pytest.raises(xfs.QuotaError) as raised:
        xfs.XFSBackend._quota_call(17, 3, 1001, xfs.DiskQuota())
    assert raised.value.code == "QUOTA_UNAVAILABLE"


def test_delete_deep_tree_without_recursion_or_unbounded_descriptors(backend, record, monkeypatch):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    current = os.open(payload, os.O_RDONLY)
    try:
        for _ in range(1050):
            os.mkdir("d", dir_fd=current)
            child = os.open("d", os.O_RDONLY, dir_fd=current)
            os.close(current)
            current = child
    finally:
        os.close(current)
    opened, maximum = set(), 0
    original_open, original_close, original_dup = os.open, os.close, os.dup
    def track_open(*args, **kwargs):
        nonlocal maximum
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        maximum = max(maximum, len(opened))
        return fd
    def track_dup(*args, **kwargs):
        nonlocal maximum
        fd = original_dup(*args, **kwargs)
        opened.add(fd)
        maximum = max(maximum, len(opened))
        return fd
    def track_close(fd):
        opened.discard(fd)
        return original_close(fd)
    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "dup", track_dup)
    try:
        assert backend.delete(record) is True
        assert maximum <= 8
        assert not payload.parent.exists()
    finally:
        # A failing implementation must not leave pytest's recursive temp cleanup
        # with the deliberately deep directory tree.
        monkeypatch.setattr(os, "open", original_open)
        monkeypatch.setattr(os, "close", original_close)
        monkeypatch.setattr(os, "dup", original_dup)
        if payload.exists():
            import sys
            previous = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(10000)
                backend.delete(record)
            finally:
                sys.setrecursionlimit(previous)


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_iterative_delete_revalidates_ancestors_before_ascending(
    backend, record, monkeypatch, tmp_path, xfs, replacement,
):
    record["directory"] = backend.create(record)
    payload = Path(record["directory"]["path"])
    (payload / "sub" / "leaf").mkdir(parents=True)
    outside = tmp_path / "outside-ancestor"
    outside.mkdir()
    (outside / "keep").write_text("unrelated")
    original = backend._open_child
    count = 0
    def change_ancestor(parentfd, name, pool, protected=False):
        nonlocal count
        if name == "sub":
            count += 1
            if count == 2:
                (payload / "sub").rename(payload / "original-sub")
                if replacement == "symlink":
                    (payload / "sub").symlink_to(outside, target_is_directory=True)
                else:
                    (payload / "sub").mkdir()
                    (payload / "sub" / "keep").write_text("replacement")
        return original(parentfd, name, pool, protected)
    monkeypatch.setattr(backend, "_open_child", change_ancestor)
    with pytest.raises(xfs.QuotaError) as exc:
        backend.delete(record)
    assert exc.value.code == "QUOTA_DIRECTORY_MISMATCH"
    assert (outside / "keep").read_text() == "unrelated"
    if replacement == "directory":
        assert (payload / "sub" / "keep").read_text() == "replacement"
    monkeypatch.setattr(backend, "_open_child", original)
    assert backend.delete(record)
    assert (outside / "keep").read_text() == "unrelated"
