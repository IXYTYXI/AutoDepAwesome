import copy
import os
from pathlib import Path
import re
import stat
import sys

import pytest

from awesome_deploy.errors import DeployError
from test_runtime import DIGEST, backend, job


def source_job(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    source = Path(record["snapshot"]["source_dir"])
    source.mkdir(mode=0o700)
    (source / "Dockerfile").write_text("FROM scratch\nCOPY app /app\n")
    (source / "app").mkdir(mode=0o755)
    (source / "app" / "main").write_bytes(b"unchanged application bytes\x00\xff")
    (source / "app" / "main").chmod(0o644)
    service = record["snapshot"]["manifest"]["services"]["web"]
    service.pop("image")
    service["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    record["execution"]["images"] = {}
    return engine, record, source


def freeze_path(engine, record):
    frozen = engine.freeze(record["snapshot"], record, lambda _: None)
    return Path(frozen["build_contexts"]["web"])


def build_call(engine, record):
    engine._builder = lambda: None
    previous = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["buildx", "build"]:
            Path(args[args.index("--iidfile") + 1]).write_text(DIGEST)
        return previous(args, **kwargs)

    engine._docker = docker
    engine.build(record, lambda _: None)
    return next(args for args in reversed(engine.calls) if args[:2] == ["buildx", "build"])


def test_context_basename_is_stable_across_jobs_and_mtime_changes(tmp_path):
    engine, record, source = source_job(tmp_path)
    first = freeze_path(engine, record)
    other = copy.deepcopy(record)
    other["id"] = "differentjob"
    os.utime(source / "app" / "main", (123, 123))
    os.utime(source / "app", (456, 456))
    second = freeze_path(engine, other)
    assert re.fullmatch(r"context-v1-[0-9a-f]{64}", first.name)
    assert first.name == second.name
    assert first.parent != second.parent
    assert first.parent.name == record["id"]
    assert (first / "app" / "main").read_bytes() == (source / "app" / "main").read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert stat.S_IMODE((first / "app").stat().st_mode) == 0o755


@pytest.mark.parametrize("change", ["bytes", "file_mode", "directory_mode", "path", "empty_directory", "type"])
def test_each_context_content_or_permission_change_gets_a_distinct_source_key(tmp_path, change):
    engine, record, source = source_job(tmp_path)
    first = freeze_path(engine, record)
    if change == "bytes":
        (source / "app" / "main").write_bytes(b"changed application bytes\x00\xff")
    elif change == "file_mode":
        (source / "app" / "main").chmod(0o755)
    elif change == "directory_mode":
        (source / "app").chmod(0o700)
    elif change == "path":
        (source / "app" / "main").rename(source / "app" / "renamed")
    elif change == "empty_directory":
        (source / "empty").mkdir()
    elif change == "type":
        (source / "app" / "main").unlink()
        (source / "app" / "main").mkdir()
    second = freeze_path(engine, record)
    assert first.name != second.name
    assert first.exists()
    assert (first / "app" / "main").read_bytes() == b"unchanged application bytes\x00\xff"


def test_freeze_keeps_dockerignore_and_multistage_dockerfile_bytes_and_excludes_git(tmp_path):
    engine, record, source = source_job(tmp_path)
    dockerfile = "FROM scratch AS original\nCOPY app /app\nFROM scratch AS final\nCOPY --from=original /app /app\n"
    (source / "Dockerfile").write_text(dockerfile)
    (source / ".dockerignore").write_text("ignored\n!app/main\n")
    (source / "ignored").write_text("not sent by Docker")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("private git internals")
    first = freeze_path(engine, record)
    assert (first / "Dockerfile").read_text() == dockerfile
    assert (first / ".dockerignore").read_text() == "ignored\n!app/main\n"
    assert not (first / ".git").exists()
    (source / ".git" / "HEAD").write_text("different git internals")
    assert freeze_path(engine, record).name == first.name


def test_legacy_context_is_copied_without_mutating_its_frozen_permissions(tmp_path):
    engine, record, source = source_job(tmp_path)
    original = engine.storage_root / "awesome-build-contexts" / record["id"] / "web"
    original.mkdir(parents=True, mode=0o700)
    (original / "Dockerfile").write_text("FROM scratch\nCOPY app /app\n")
    (original / "app").mkdir(mode=0o700)
    (original / "app" / "main").write_bytes(b"frozen old bytes")
    record["snapshot"]["build_contexts"] = {"web": str(original)}
    snapshot_before = copy.deepcopy(record["snapshot"])
    args = build_call(engine, record)
    staged = Path(args[-1])
    assert staged != original
    assert staged.parent == original.parent
    assert re.fullmatch(r"context-v1-[0-9a-f]{64}", staged.name)
    assert original.exists()
    assert stat.S_IMODE((staged / "app").stat().st_mode) == 0o700
    assert (staged / "app" / "main").read_bytes() == b"frozen old bytes"
    assert record["snapshot"] == snapshot_before
    assert "--no-cache" not in args
    assert "--network=none" in args
    assert "--pull" not in args
    assert Path(build_call(engine, record)[-1]) == staged


def test_existing_image_recovery_never_reads_or_rebuilds_context(tmp_path):
    engine, record, source = source_job(tmp_path)
    record["execution"]["images"] = {"web": DIGEST}
    record["snapshot"]["build_contexts"] = {"web": "/missing/old/frozen/context"}
    engine._builder = lambda: pytest.fail("Recovery must not require the builder")
    assert engine.build(record, lambda _: None) == {"web": DIGEST}
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)


@pytest.mark.parametrize("bad_path", ["outside", "symlink", "fifo", "parent_symlink", "root_file", "writable_parent"])
def test_build_rejects_unsafe_legacy_context_before_building(tmp_path, bad_path):
    engine, record, source = source_job(tmp_path)
    root = engine.storage_root / "awesome-build-contexts" / record["id"]
    root.mkdir(parents=True)
    context = root / "web"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n")
    if bad_path == "outside":
        context = source
    elif bad_path == "symlink":
        (context / "link").symlink_to(source / "app" / "main")
    elif bad_path == "fifo":
        os.mkfifo(context / "pipe")
    elif bad_path == "parent_symlink":
        root.rename(root.with_name("other"))
        root.symlink_to(root.with_name("other"), target_is_directory=True)
    elif bad_path == "root_file":
        (context / "Dockerfile").unlink()
        context.rmdir()
        context.write_text("not a directory")
    else:
        root.chmod(0o777)
    record["snapshot"]["build_contexts"] = {"web": str(context)}
    with pytest.raises(DeployError) as error:
        build_call(engine, record)
    assert error.value.code == "UNSAFE_BUILD_PATH"
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)


def test_corrupted_content_addressed_context_is_not_trusted_or_republished(tmp_path):
    engine, record, source = source_job(tmp_path)
    record["snapshot"] = engine.freeze(record["snapshot"], record, lambda _: None)
    context = Path(record["snapshot"]["build_contexts"]["web"])
    (context / "app" / "main").write_text("corrupted after freeze")
    with pytest.raises(DeployError) as error:
        build_call(engine, record)
    assert error.value.code == "UNSAFE_BUILD_PATH"
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)


def test_context_xattrs_are_preserved_and_change_source_key(tmp_path):
    engine, record, source = source_job(tmp_path)
    name = "com.awesome.cache-test" if sys.platform == "darwin" else "user.awesome.cache-test"
    path = source / "app" / "main"
    try:
        os.setxattr(path, name, b"first")
    except (AttributeError, OSError):
        pytest.skip("Filesystem does not support user xattrs")
    first = freeze_path(engine, record)
    assert os.getxattr(first / "app" / "main", name) == b"first"
    os.setxattr(path, name, b"second")
    second = freeze_path(engine, record)
    assert first.name != second.name
    assert os.getxattr(second / "app" / "main", name) == b"second"


def test_legacy_retry_rejects_corrupted_existing_sibling_without_replacing_it(tmp_path):
    engine, record, source = source_job(tmp_path)
    original = engine.storage_root / "awesome-build-contexts" / record["id"] / "web"
    original.mkdir(parents=True)
    (original / "Dockerfile").write_text("FROM scratch\n")
    (original / "app").write_bytes(b"frozen bytes")
    record["snapshot"]["build_contexts"] = {"web": str(original)}
    target = Path(build_call(engine, record)[-1])
    (target / "app").write_bytes(b"partial copy")
    engine.calls.clear()
    with pytest.raises(DeployError) as error:
        build_call(engine, record)
    assert error.value.code == "UNSAFE_BUILD_PATH"
    assert (original / "app").read_bytes() == b"frozen bytes"
    assert (target / "app").read_bytes() == b"partial copy"
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)
    assert not list(original.parent.glob(".context-*"))


def test_digest_tracks_xattrs_and_owner_without_timestamps(tmp_path, monkeypatch):
    engine, record, source = source_job(tmp_path)
    attributes = {"user.one": b"first", "user.two": b"second"}
    monkeypatch.setattr(os, "listxattr", lambda *args, **kwargs: list(reversed(attributes)), raising=False)
    monkeypatch.setattr(os, "getxattr", lambda path, name, **kwargs: attributes[name], raising=False)
    first = engine._context_digest(source)
    attributes = dict(reversed(list(attributes.items())))
    assert engine._context_digest(source) == first
    attributes["user.one"] = b"changed"
    assert engine._context_digest(source) != first
    attributes["user.one"] = b"first"
    original_stat = Path.lstat

    def other_owner(path):
        result = original_stat(path)
        return type("Metadata", (), {
            name: getattr(result, name) + (1 if name == "st_uid" else 0)
            for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        })()

    monkeypatch.setattr(Path, "lstat", other_owner)
    assert engine._context_digest(source) != first


def test_freeze_rejects_non_directory_context_before_pinning(tmp_path):
    engine, record, source = source_job(tmp_path)
    (source / "context-file").write_text("not a directory")
    service = record["snapshot"]["manifest"]["services"]["web"]
    service["build"]["context"] = "context-file"
    with pytest.raises(DeployError):
        engine.freeze(record["snapshot"], record, lambda _: None)
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)


def test_identical_service_contexts_share_frozen_path_and_keep_independent_images(tmp_path):
    engine, record, source = source_job(tmp_path)
    services = record["snapshot"]["manifest"]["services"]
    services["other"] = copy.deepcopy(services["web"])
    record["snapshot"] = engine.freeze(record["snapshot"], record, lambda _: None)
    paths = record["snapshot"]["build_contexts"]
    assert paths["web"] == paths["other"]
    context = Path(paths["web"])
    assert (context / "app" / "main").read_bytes() == b"unchanged application bytes\x00\xff"
    build_call(engine, record)
    calls = [args for args in engine.calls if args[:2] == ["buildx", "build"]]
    assert len(calls) == 2
    assert {Path(args[args.index("--iidfile") + 1]).name for args in calls} == {"web.iid", "other.iid"}
    assert {args[args.index("--tag") + 1] for args in calls} == {
        "awesome/abc123-web:candidate", "awesome/abc123-other:candidate"
    }
    assert context.exists()
