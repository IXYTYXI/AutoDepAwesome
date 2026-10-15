import copy
import json
import os

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secrets_store import SecretStore


REPOSITORY = "https://github.com/example/demo.git"
SUBDOMAIN = "demo"
VALUE = "NEVER_READ_OR_REPORT_THIS_SECRET"


@pytest.fixture
def store(tmp_path):
    return SecretStore(Settings(state_dir=tmp_path.resolve() / "state"))


def create(store, name="PASSWORD", subdomain=SUBDOMAIN):
    return store.create_missing(REPOSITORY, subdomain, name, VALUE)


def scope(store):
    return store._directory(REPOSITORY, SUBDOMAIN, "PASSWORD").parent


def inventory(store):
    return store.inventory_scope(REPOSITORY, SUBDOMAIN)


def denied(action):
    with pytest.raises(DeployError) as caught:
        action()
    assert caught.value.code == "SECRET_DELETE_CONFLICT"
    assert VALUE not in str(caught.value.as_dict())
    assert "RAW_PRIVATE_ERROR" not in str(caught.value.as_dict())


def test_missing_scope_inventory_and_deletion_are_read_only(store):
    plan = inventory(store)
    assert plan["repository"] == REPOSITORY and plan["subdomain"] == SUBDOMAIN
    assert plan["scope"] is None
    store.verify_deletion(plan)
    store.delete_scope(plan)
    assert not store.root.parent.exists()


def test_inventory_contains_only_metadata_and_all_rotated_versions(store, monkeypatch):
    first = create(store)
    second = store.set(REPOSITORY, SUBDOMAIN, "PASSWORD", "rotated-private-value")
    create(store, "MASTER_KEY")
    original_open = os.open

    def directories_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY, "Inventory/deletion must not open a value or pointer"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", directories_only)
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("No secret contents may be read"))
    plan = json.loads(json.dumps(inventory(store)))
    assert set(plan["items"]["PASSWORD"]["files"]) == {"current.json", first + ".json", second + ".json"}
    assert VALUE not in json.dumps(plan) and "rotated-private-value" not in json.dumps(plan)
    store.verify_deletion(plan)
    store.delete_scope(plan)
    assert not scope(store).exists()


def test_exact_deletion_preserves_other_scope_and_platform_credentials(store):
    create(store)
    other = create(store, subdomain="other")
    auth = store.root.parent / "credentials.json"
    auth.write_text("platform-only")
    plan = inventory(store)
    store.delete_scope(plan)
    store.delete_scope(plan)
    store.verify_deletion(plan)
    assert not scope(store).exists()
    assert store.resolve(REPOSITORY, "other", {"PASSWORD": "PASSWORD"}, {"PASSWORD": other}) == {"PASSWORD": VALUE}
    assert auth.read_text() == "platform-only"
    assert store.root.is_dir() and store.root.parent.is_dir()


@pytest.mark.parametrize("change", ["new_name", "new_version", "replace_file", "modify_file", "replace_directory", "replace_scope"])
def test_changes_after_inventory_are_rejected_before_any_deletion(store, change):
    version = create(store)
    create(store, "SECOND")
    plan = inventory(store)
    directory = scope(store) / "PASSWORD"
    path = directory / (version + ".json")
    if change == "new_name":
        create(store, "NEW_SECRET")
    elif change == "new_version":
        store.set(REPOSITORY, SUBDOMAIN, "PASSWORD", "new-version")
    elif change == "replace_file":
        replacement = directory / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, path)
    elif change == "modify_file":
        path.write_text("changed")
    else:
        target = directory if change == "replace_directory" else scope(store)
        target.rename(target.with_name(target.name + "-old"))
        target.mkdir(mode=0o700)
    before = {str(p): p.read_bytes() for p in scope(store).rglob("*") if p.is_file()}
    denied(lambda: store.verify_deletion(plan))
    denied(lambda: store.delete_scope(plan))
    assert {str(p): p.read_bytes() for p in scope(store).rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("component", ["state", "root", "scope", "name", "version", "pointer"])
@pytest.mark.parametrize("change", ["symlink", "permissions"])
def test_unsafe_metadata_is_rejected_without_repair(store, component, change):
    version = create(store)
    directory = scope(store) / "PASSWORD"
    path = {"state": store.root.parent, "root": store.root, "scope": scope(store), "name": directory,
            "version": directory / (version + ".json"), "pointer": directory / "current.json"}[component]
    if change == "symlink":
        target = path.with_name(path.name + "-original")
        path.rename(target)
        path.symlink_to(target, target_is_directory=target.is_dir())
    else:
        path.chmod(0o755 if path.is_dir() else 0o644)
    denied(lambda: inventory(store))
    if change == "symlink":
        assert path.is_symlink()
    else:
        assert path.stat().st_mode & 0o077


@pytest.mark.parametrize("change", ["hardlink", "fifo", "unknown_file", "nested_directory", "wrong_owner"])
def test_inventory_refuses_links_special_files_and_unknown_entries(store, monkeypatch, change):
    version = create(store)
    directory = scope(store) / "PASSWORD"
    path = directory / (version + ".json")
    if change == "hardlink":
        os.link(path, store.root.parent / "outside-hardlink")
    elif change == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif change == "unknown_file":
        (directory / "notes.txt").write_text("unknown")
    elif change == "nested_directory":
        (directory / ("a" * 32 + ".json")).mkdir(mode=0o700)
    else:
        owner = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    denied(lambda: inventory(store))


def test_partial_unlink_failure_can_resume_with_original_plan(store, monkeypatch):
    create(store)
    create(store, "SECOND")
    plan = inventory(store)
    original_unlink = os.unlink
    calls = []

    def interrupted(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("RAW_PRIVATE_ERROR " + VALUE)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", interrupted)
    denied(lambda: store.delete_scope(plan))
    assert len(calls) == 2
    monkeypatch.setattr(os, "unlink", original_unlink)
    store.verify_deletion(plan)
    store.delete_scope(plan)
    assert not scope(store).exists()


def test_new_contents_after_partial_deletion_are_not_deleted(store):
    version = create(store)
    plan = inventory(store)
    (scope(store) / "PASSWORD" / (version + ".json")).unlink()
    create(store, "NEW_SECRET")
    denied(lambda: store.delete_scope(plan))
    assert store.inspect(REPOSITORY, SUBDOMAIN, "NEW_SECRET") == "configured"


def test_scope_created_after_missing_inventory_is_not_deleted(store):
    plan = inventory(store)
    create(store)
    denied(lambda: store.delete_scope(plan))
    assert store.inspect(REPOSITORY, SUBDOMAIN, "PASSWORD") == "configured"


def test_deleted_directory_recreation_is_not_treated_as_a_missing_retry(store):
    create(store)
    plan = inventory(store)
    directory = scope(store) / "PASSWORD"
    moved = directory.with_name("retired-directory")
    directory.rename(moved)
    directory.mkdir(mode=0o700)
    denied(lambda: store.delete_scope(plan))
    assert directory.exists() and moved.exists()


@pytest.mark.parametrize("mutation", ["path", "missing_identity", "invalid_scope", "unknown_key"])
def test_malformed_deletion_plans_are_rejected(store, mutation):
    create(store)
    plan = copy.deepcopy(inventory(store))
    if mutation == "path":
        plan["items"]["../outside"] = plan["items"].pop("PASSWORD")
    elif mutation == "missing_identity":
        plan["items"]["PASSWORD"].pop("identity")
    elif mutation == "invalid_scope":
        plan["subdomain"] = "../outside"
    else:
        plan["path"] = "/tmp/elsewhere"
    denied(lambda: store.delete_scope(plan))
    assert store.inspect(REPOSITORY, SUBDOMAIN, "PASSWORD") == "configured"


def test_deletion_fsyncs_directories_and_preserves_private_parents(store, monkeypatch):
    create(store)
    plan = inventory(store)
    original_fsync = os.fsync
    synced = []

    def record(fd):
        synced.append(os.fstat(fd).st_ino)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    store.delete_scope(plan)
    assert plan["items"]["PASSWORD"]["identity"]["ino"] in synced
    assert plan["scope"]["ino"] in synced
    assert store.root.stat().st_ino in synced
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.root.parent.stat().st_mode & 0o777 == 0o700


def test_retry_after_final_directory_fsync_failure_syncs_removal_again(store, monkeypatch):
    create(store)
    plan = inventory(store)
    original_fsync = os.fsync
    root_inode = store.root.stat().st_ino
    synced = []

    def fail_root(fd):
        if os.fstat(fd).st_ino == root_inode:
            raise OSError("RAW_PRIVATE_ERROR")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_root)
    denied(lambda: store.delete_scope(plan))
    assert not scope(store).exists()

    def record(fd):
        synced.append(os.fstat(fd).st_ino)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    store.delete_scope(plan)
    assert root_inode in synced
