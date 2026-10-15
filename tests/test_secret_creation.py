import concurrent.futures
import os
from pathlib import Path
import stat
import threading

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secrets_store import SecretStore


REPOSITORY = "https://github.com/example/demo.git"
SUBDOMAIN = "demo"
NAME = "APP_SECRET"
VALUE = "SECRET_VALUE_MUST_NOT_APPEAR_IN_ERRORS"


@pytest.fixture
def store(tmp_path):
    return SecretStore(Settings(state_dir=tmp_path.resolve() / "state"))


def directory(store):
    return store._directory(REPOSITORY, SUBDOMAIN, NAME)


def create(store, value=VALUE):
    return store.create_missing(REPOSITORY, SUBDOMAIN, NAME, value)


def inspect(store):
    return store.inspect(REPOSITORY, SUBDOMAIN, NAME)


def assert_safe_error(error, code):
    assert error.value.code == code
    assert VALUE not in str(error.value.as_dict())
    assert "RAW_FILESYSTEM_ERROR" not in str(error.value.as_dict())
    assert error.value.path == ""


def test_missing_status_is_read_only(store):
    assert inspect(store) == "missing"
    assert not store.root.parent.exists()


def test_create_missing_is_private_and_compatible_with_runtime(store):
    version = create(store)
    assert len(version) == 32
    assert inspect(store) == "configured"
    assert store.versions(REPOSITORY, SUBDOMAIN, {"TOKEN": NAME}) == {NAME: version}
    assert store.resolve(REPOSITORY, SUBDOMAIN, {"TOKEN": NAME}) == {"TOKEN": VALUE}
    target = directory(store)
    for path in (store.root.parent, store.root, target.parent, target):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert {p.name for p in target.iterdir()} == {"current.json", version + ".json"}
    for path in target.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_uid == os.getuid()


def test_status_never_opens_secret_value(store, monkeypatch):
    version = create(store)
    value_path = directory(store) / (version + ".json")
    value_path.write_bytes(b"value contents deliberately are not valid JSON")
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert Path(path).name != value_path.name, "status opened a secret value"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    assert inspect(store) == "configured"


def test_existing_value_is_never_overwritten(store):
    version = create(store)
    before = {p.name: p.read_bytes() for p in directory(store).iterdir()}
    with pytest.raises(DeployError) as error:
        create(store, "replacement")
    assert_safe_error(error, "SECRET_ALREADY_CONFIGURED")
    assert {p.name: p.read_bytes() for p in directory(store).iterdir()} == before
    assert store.resolve(REPOSITORY, SUBDOMAIN, {"TOKEN": NAME}, {NAME: version}) == {"TOKEN": VALUE}


@pytest.mark.parametrize("value", [None, True, 12, 1.5, [], {}, b"bytes", "", "a\nb", "a\rb", "a\0b",
                                  "x" * 16385, "é" * 8193, "\ud800"],
                         ids=["null", "bool", "int", "float", "list", "object", "bytes", "empty", "lf",
                              "cr", "nul", "long-ascii", "long-unicode", "invalid-unicode"])
def test_invalid_values_are_rejected_before_any_write(store, value):
    with pytest.raises(DeployError) as error:
        create(store, value)
    assert_safe_error(error, "INVALID_SECRET")
    assert not store.root.parent.exists()


@pytest.mark.parametrize("value", ["a", " ", "é" * 8192], ids=["ascii", "space", "unicode-limit"])
def test_value_validator_can_validate_complete_requests_before_writing(store, value):
    assert store.validate_value(value) is None
    assert not store.root.parent.exists()


@pytest.mark.parametrize("component", ["state", "secrets", "scope", "name"])
def test_existing_unsafe_directory_permissions_are_not_repaired(store, component):
    create(store)
    target = directory(store)
    path = {"state": store.root.parent, "secrets": store.root, "scope": target.parent, "name": target}[component]
    path.chmod(0o755)
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


@pytest.mark.parametrize("component", ["state", "secrets", "scope", "name"])
def test_directory_symlinks_are_never_followed_or_repaired(store, component):
    create(store)
    target = directory(store)
    path = {"state": store.root.parent, "secrets": store.root, "scope": target.parent, "name": target}[component]
    moved = path.with_name(path.name + "-real")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=True)
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert path.is_symlink()


def test_symlink_in_state_ancestor_is_rejected(tmp_path):
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir(mode=0o700)
    alias = root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    store = SecretStore(Settings(state_dir=alias / "state"))
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert not (real / "state").exists()


@pytest.mark.parametrize("file_kind", ["pointer", "version"])
@pytest.mark.parametrize("problem", ["symlink", "directory", "fifo", "permissions", "owner"])
def test_unsafe_pointer_or_version_is_unavailable(store, monkeypatch, file_kind, problem):
    version = create(store)
    path = directory(store) / ("current.json" if file_kind == "pointer" else version + ".json")
    if problem == "permissions":
        path.chmod(0o644)
    elif problem == "owner":
        actual_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    else:
        path.unlink()
        if problem == "symlink":
            path.symlink_to("missing-target")
        elif problem == "directory":
            path.mkdir(mode=0o700)
        else:
            os.mkfifo(path, 0o600)
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")


@pytest.mark.parametrize("contents", [b"", b"not json", b"[]", b"{}", b'{"version":null}',
                                    b'{"version":"../escape"}', b'{"version":42}', b"x" * 4096,
                                    b'{"version":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'],
                         ids=["empty", "non-json", "array", "missing", "null", "path", "number", "large",
                              "missing-version"])
def test_malformed_or_dangling_current_pointer_is_unavailable(store, contents):
    create(store)
    pointer = directory(store) / "current.json"
    pointer.write_bytes(contents)
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert pointer.read_bytes() == contents


def test_missing_pointer_with_existing_version_cannot_replace_original_secret(store):
    old_version = create(store)
    (directory(store) / "current.json").unlink()
    before = (directory(store) / (old_version + ".json")).read_bytes()
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store, "new value")
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert {p.name for p in directory(store).iterdir()} == {old_version + ".json"}
    assert (directory(store) / (old_version + ".json")).read_bytes() == before


@pytest.mark.parametrize("leftover", [".current-incomplete", "unknown.json", "unexpected-file"])
def test_missing_pointer_with_any_leftover_entry_is_unavailable(store, leftover):
    version = create(store)
    for path in directory(store).iterdir():
        path.unlink()
    orphan = directory(store) / leftover
    orphan.write_text(version)
    orphan.chmod(0o600)
    assert inspect(store) == "unavailable"
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert {p.name for p in directory(store).iterdir()} == {leftover}


def test_atomic_create_race_has_one_winner_and_no_orphaned_values(store, monkeypatch):
    barrier = threading.Barrier(2)
    original_inspect = store._inspect_directory
    count_lock = threading.Lock()
    calls = 0

    def racing_inspect(descriptor):
        nonlocal calls
        state = original_inspect(descriptor)
        with count_lock:
            calls += 1
            initial_call = calls <= 2
        if initial_call:
            barrier.wait(timeout=5)
        return state

    monkeypatch.setattr(store, "_inspect_directory", racing_inspect)

    def attempt(value):
        try:
            return ("created", create(store, value))
        except DeployError as exc:
            return (exc.code, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["first", "second"]))
    assert sorted(code for code, _ in results) == ["SECRET_ALREADY_CONFIGURED", "created"]
    version = next(version for code, version in results if code == "created")
    assert {p.name for p in directory(store).iterdir()} == {"current.json", version + ".json"}
    assert inspect(store) == "configured"


def test_pointer_symlink_race_never_overwrites_existing_destination(store, monkeypatch):
    original_link = os.link

    def raced_link(source, destination, *args, **kwargs):
        os.symlink("untouched-target", destination, dir_fd=kwargs["dst_dir_fd"])
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", raced_link)
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    pointer = directory(store) / "current.json"
    assert pointer.is_symlink()
    assert os.readlink(pointer) == "untouched-target"
    assert {p.name for p in directory(store).iterdir()} == {"current.json"}


def test_publication_failure_cleans_only_own_files_and_does_not_echo_error(store, monkeypatch):
    old_version = create(store)
    unrelated = directory(store).parent / "unrelated-version.json"
    (directory(store) / (old_version + ".json")).rename(unrelated)
    (directory(store) / "current.json").unlink()
    before = unrelated.read_bytes()

    def fail_link(*args, **kwargs):
        raise OSError("RAW_FILESYSTEM_ERROR " + VALUE)

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert list(directory(store).iterdir()) == []
    assert unrelated.read_bytes() == before
    assert inspect(store) == "missing"


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_failure_before_publication_cleans_only_new_files(store, monkeypatch, failed_sync):
    version = create(store)
    unrelated = directory(store).parent / "unrelated-version.json"
    (directory(store) / (version + ".json")).rename(unrelated)
    (directory(store) / "current.json").unlink()
    before = unrelated.read_bytes()
    original_sync = os.fsync
    calls = 0

    def fail_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("RAW_FILESYSTEM_ERROR")
        return original_sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert list(directory(store).iterdir()) == []
    assert unrelated.read_bytes() == before
    assert inspect(store) == "missing"


def test_failure_after_publication_preserves_successful_value(store, monkeypatch):
    original_sync = os.fsync

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("RAW_FILESYSTEM_ERROR")
        return original_sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert inspect(store) == "configured"
    assert store.resolve(REPOSITORY, SUBDOMAIN, {"TOKEN": NAME}) == {"TOKEN": VALUE}
    assert len(list(directory(store).iterdir())) == 2


def test_version_collision_does_not_delete_existing_file(store, monkeypatch):
    version = create(store)
    (directory(store) / "current.json").unlink()
    before = (directory(store) / (version + ".json")).read_bytes()
    (directory(store) / (version + ".json")).unlink()
    from awesome_deploy import secrets_store

    class ExistingVersion:
        hex = version

    def inject_existing_version():
        path = directory(store) / (version + ".json")
        path.write_bytes(before)
        path.chmod(0o600)
        return ExistingVersion()

    monkeypatch.setattr(secrets_store.uuid, "uuid4", inject_existing_version)
    with pytest.raises(DeployError) as error:
        create(store)
    assert_safe_error(error, "SECRET_UNAVAILABLE")
    assert {p.name for p in directory(store).iterdir()} == {version + ".json"}
    assert (directory(store) / (version + ".json")).read_bytes() == before


def test_state_directory_context_opens_existing_only_and_closes_descriptor(store):
    with pytest.raises(FileNotFoundError):
        with store._open_state_directory():
            pytest.fail("missing state directory was created")
    assert not store.root.parent.exists()
    create(store)
    with store._open_state_directory() as descriptor:
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
    with pytest.raises(OSError):
        os.fstat(descriptor)
