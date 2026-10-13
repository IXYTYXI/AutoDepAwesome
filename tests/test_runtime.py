import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


DIGEST = "sha256:" + "a" * 64
IMAGE = "example/web@" + DIGEST


def manifest():
    return {
        "version": 1,
        "app": {"subdomain": "demo"},
        "services": {
            "web": {
                "image": "example/web:latest",
                "port": 8080,
                "env": {},
                "secretRefs": {},
                "dependsOn": {},
                "resources": {"cpu": 0.5, "memoryMiB": 512},
                "healthcheck": {
                    "type": "http",
                    "path": "/",
                    "intervalSeconds": 1,
                    "timeoutSeconds": 1,
                    "retries": 2,
                    "startPeriodSeconds": 0,
                },
            }
        },
        "routes": [{"path": "/", "match": "prefix", "service": "web", "stripPrefix": False}],
    }


def job(tmp_path):
    return {
        "id": "abc123",
        "app_id": "app123",
        "input": {},
        "snapshot": {
            "repository": "https://github.com/a/b",
            "source_dir": str(tmp_path / "source"),
            "commit": "c" * 40,
            "manifest": manifest(),
        },
        "execution": {"ports": {"web": 20000}, "images": {"web": DIGEST}, "previous_release": None},
    }


def backend(tmp_path):
    from awesome_deploy.runtime import DeploymentBackend

    result = DeploymentBackend(Settings(state_dir=tmp_path / "state"))
    result._check_socket = lambda: None
    result._check_storage = lambda info: None
    result.storage_root = tmp_path / "bounded-docker"
    result.calls = []

    def docker(args, **kwargs):
        result.calls.append(args)
        if args[0] == "info":
            return json.dumps(
                {
                    "SecurityOptions": ["name=rootless", "name=seccomp,profile=builtin"],
                    "CgroupVersion": "2",
                    "CgroupDriver": "systemd",
                    "MemoryLimit": True,
                    "CpuCfsPeriod": True,
                    "CpuCfsQuota": True,
                    "PidsLimit": True,
                    "Warnings": [],
                }
            )
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                [{"Id": DIGEST, "RepoDigests": [IMAGE], "Config": {"Volumes": None, "OnBuild": None}}]
            )
        if args[:2] == ["container", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": "containerid",
                        "Config": {
                            "Labels": {
                                "awesome.managed": "true",
                                "awesome.job": "abc123",
                                "awesome.app": "app123",
                            }
                        },
                        "State": {"Running": True, "Health": {"Status": "healthy"}},
                    }
                ]
            )
        return ""

    result._docker = docker
    return result


def test_freeze_pins_prebuilt_before_reservation_and_does_not_mutate_input(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    original = copy.deepcopy(record["snapshot"])
    saved = []
    frozen = engine.freeze(original, record, saved.append)
    assert frozen["manifest"]["services"]["web"]["image"] == IMAGE
    assert original["manifest"]["services"]["web"]["image"] == "example/web:latest"
    assert any("frozen_images" in patch for patch in saved)


def test_build_file_is_staged_and_all_external_bases_are_pinned(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    source = Path(record["snapshot"]["source_dir"])
    source.mkdir()
    content = (
        "FROM example/web:latest AS base\nRUN echo hello\nFROM base AS final\nCOPY --from=base /app /app\n"
    )
    (source / "Dockerfile").write_text(content)
    svc = record["snapshot"]["manifest"]["services"]["web"]
    svc.pop("image")
    svc["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    frozen = engine.freeze(record["snapshot"], record, lambda _: None)
    staged = Path(frozen["build_contexts"]["web"]) / "Dockerfile"
    assert IMAGE in staged.read_text()
    assert "FROM base AS final" in staged.read_text()
    assert (source / "Dockerfile").read_text() == content


@pytest.mark.parametrize(
    "content",
    [
        "# syntax=evil/frontend\nFROM busybox\n",
        "ARG BASE\nFROM ${BASE}\n",
        "FROM busybox\nCOPY --from=other:latest /x /x\n",
        "FROM busybox\nRUN --mount=type=secret,id=x cat /x\n",
        "FROM --platform=$BUILDPLATFORM busybox\n",
        "FROM busybox\nADD https://example.com/x /x\n",
        "FROM busybox\nRUN <<EOF\necho hidden\nEOF\n",
        "FROM busybox\nVOLUME /data\n",
    ],
)
def test_unsupported_build_constructs_fail_before_reserve(tmp_path, content):
    engine = backend(tmp_path)
    record = job(tmp_path)
    source = Path(record["snapshot"]["source_dir"])
    source.mkdir()
    (source / "Dockerfile").write_text(content)
    svc = record["snapshot"]["manifest"]["services"]["web"]
    svc.pop("image")
    svc["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    with pytest.raises(DeployError):
        engine.freeze(record["snapshot"], record, lambda _: None)


def test_daemon_must_be_rootless_with_cgroup_v2(tmp_path):
    engine = backend(tmp_path)
    engine._docker = lambda args, **kw: json.dumps({"SecurityOptions": [], "CgroupVersion": "1"})
    with pytest.raises(DeployError, match="rootless"):
        engine.preflight()


def test_image_declared_volumes_are_rejected(tmp_path):
    engine = backend(tmp_path)
    old = engine._docker
    engine._docker = lambda args, **kw: (
        json.dumps([{"Id": DIGEST, "Config": {"Volumes": {"/data": {}}}, "RepoDigests": [IMAGE]}])
        if args[:2] == ["image", "inspect"]
        else old(args, **kw)
    )
    with pytest.raises(DeployError, match="volumes"):
        engine.freeze(job(tmp_path)["snapshot"], job(tmp_path), lambda _: None)


def test_cleanup_checks_actual_labels_before_deletion(tmp_path):
    engine = backend(tmp_path)
    calls = []

    def docker(args, **kw):
        calls.append(args)
        if args[:2] == ["container", "ls"]:
            return "foreign\nowned\n"
        if args[:2] == ["container", "inspect"]:
            owner = "other" if args[-1] == "foreign" else "abc123"
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "awesome.managed": "true",
                                "awesome.job": owner,
                                "awesome.app": "app123",
                            }
                        }
                    }
                ]
            )
        if args[:2] == ["network", "ls"]:
            return ""
        return ""

    engine._docker = docker
    engine.cleanup(job(tmp_path))
    assert ["container", "rm", "-f", "owned"] in calls
    assert ["container", "rm", "-f", "foreign"] not in calls


def test_health_uses_real_candidate_http_and_rejects_failure(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        code = 200

        def do_GET(self):
            self.send_response(self.code)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = backend(tmp_path)
        record = job(tmp_path)
        record["execution"]["ports"]["web"] = server.server_port
        engine.health(record)
        Handler.code = 500
        with pytest.raises(DeployError, match="health"):
            engine.health(record)
    finally:
        server.shutdown()
        server.server_close()


def test_start_enforces_resources_and_no_secret_values_in_args(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    record["snapshot"]["manifest"]["services"]["web"]["env"] = {"PUBLIC": "literal$value"}
    engine.start(record, lambda _: None)
    create = next(c for c in engine.calls if c[:2] == ["container", "create"])
    for flag in [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--log-opt",
        "--user",
        "--tmpfs",
    ]:
        assert flag in create
    assert "127.0.0.1:20000:8080" in create
    assert "literal$value" not in create
    assert any(c[:2] == ["network", "create"] and "--internal" in c for c in engine.calls)


def test_builder_inspects_supported_text_protocol_and_resource_limits(tmp_path):
    engine = backend(tmp_path)
    calls = []
    limits = {"Memory": 1024 * 1024 * 1024, "NanoCpus": 1_000_000_000, "NetworkMode": "bridge"}

    def docker(args, **kwargs):
        calls.append(args)
        if args[:2] == ["buildx", "inspect"]:
            assert "--format" not in args  # buildx inspect has no format option
            return "Name: awesome-builder\nDriver: docker-container\nNodes:\nName: awesome-builder0\nEndpoint: unix:///run/user/1001/docker.sock\nStatus: running\n org.mobyproject.buildkit.worker.oci.process-mode: sandbox\n"
        return json.dumps(
            [
                {
                    "Config": {"Image": "moby/buildkit:rootless@" + DIGEST, "User": "1000:1000"},
                    "HostConfig": limits,
                }
            ]
        )

    engine._docker = docker
    engine._builder()
    limits["Memory"] = 0
    with pytest.raises(DeployError):
        engine._builder()


def test_cgroup_none_is_rejected_even_if_version_is_two(tmp_path):
    engine = backend(tmp_path)
    engine._docker = lambda args, **kwargs: json.dumps(
        {"SecurityOptions": ["name=rootless", "name=seccomp"], "CgroupVersion": "2", "CgroupDriver": "none"}
    )
    with pytest.raises(DeployError):
        engine.preflight()


def test_readiness_check_does_not_build_or_publish(tmp_path):
    engine = backend(tmp_path)
    engine._builder = lambda: None
    engine.gateway.check = lambda: {"ok": True, "ready": True}
    result = engine.check()
    assert result["ready"] is True
    assert not any(call[0] in {"pull", "run", "build"} for call in engine.calls)


def test_freeze_accepts_unreserved_new_job_without_app_id(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    record["app_id"] = None
    assert (
        engine.freeze(record["snapshot"], record, lambda _: None)["manifest"]["services"]["web"]["image"]
        == IMAGE
    )


def test_build_passes_approved_target_and_literal_args(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    svc = record["snapshot"]["manifest"]["services"]["web"]
    svc.pop("image")
    svc["build"] = {
        "context": ".",
        "dockerfile": "Dockerfile",
        "target": "production",
        "args": {"MODE": "literal$(x)"},
    }
    record["snapshot"]["build_contexts"] = {"web": str(context)}
    record["execution"]["images"] = {}
    engine._builder = lambda: None
    previous = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["buildx", "build"]:
            Path(args[args.index("--iidfile") + 1]).write_text(DIGEST)
        return previous(args, **kwargs)

    engine._docker = docker
    assert engine.build(record, lambda _: None) == {"web": DIGEST}
    args = next(args for args in engine.calls if args[:2] == ["buildx", "build"])
    assert args[args.index("--target") + 1] == "production"
    assert args[args.index("--build-arg") + 1] == "MODE=literal$(x)"


def test_staged_build_context_uses_bounded_docker_filesystem(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    source = Path(record["snapshot"]["source_dir"])
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\nCOPY app /app\n")
    (source / "app").write_text("artifact")
    svc = record["snapshot"]["manifest"]["services"]["web"]
    svc.pop("image")
    svc["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    frozen = engine.freeze(record["snapshot"], record, lambda _: None)
    path = Path(frozen["build_contexts"]["web"])
    assert path.is_relative_to(engine.storage_root)
    assert not path.is_relative_to(engine.settings.state_dir)


def test_cleanup_removes_only_this_jobs_controlled_directories(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    for base in [
        engine.settings.state_dir / "sources",
        engine.settings.state_dir / "git",
        engine.storage_root / "awesome-build-contexts",
    ]:
        (base / "abc123").mkdir(parents=True)
        (base / "other").mkdir()
    engine._docker = lambda args, **kwargs: ""
    engine.cleanup(record)
    for base in [
        engine.settings.state_dir / "sources",
        engine.settings.state_dir / "git",
        engine.storage_root / "awesome-build-contexts",
    ]:
        assert not (base / "abc123").exists()
        assert (base / "other").exists()


def test_resumed_start_connects_networks_missing_after_crash(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    previous = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["container", "ls"]:
            return "containerid\n"
        return previous(args, **kwargs)

    engine._docker = docker
    engine.start(record, lambda _: None)
    assert ["network", "connect", "--alias", "web", "aw-abc123-s-3-web", "awesome-abc123-web"] in engine.calls


def test_runtime_root_user_configuration_rejected(tmp_path):
    from dataclasses import replace

    engine = backend(tmp_path)
    engine.settings = replace(engine.settings, runtime_user="0:0")
    with pytest.raises(DeployError, match="nonroot"):
        engine.preflight()


@pytest.mark.parametrize(
    "mounted,same_device,total_mib,free_mib,inodes,allowed",
    [
        (True, False, 81920, 3000, 2000, True),
        (False, False, 81920, 3000, 2000, False),
        (True, True, 81920, 3000, 2000, False),
        (True, False, 81921, 3000, 2000, False),
        (True, False, 81920, 1, 2000, False),
        (True, False, 81920, 3000, 1, False),
    ],
)
def test_docker_storage_is_a_bounded_separate_filesystem(
    tmp_path, monkeypatch, mounted, same_device, total_mib, free_mib, inodes, allowed
):
    from types import SimpleNamespace
    from awesome_deploy.runtime import DeploymentBackend
    import awesome_deploy.runtime as module

    engine = DeploymentBackend(Settings(state_dir=tmp_path / "state"))
    engine.settings.state_dir.mkdir()
    storage = tmp_path / "docker"
    storage.mkdir()
    original_stat = Path.stat

    def fake_stat(path, **kwargs):
        if path == storage:
            return SimpleNamespace(st_dev=engine.settings.state_dir.stat().st_dev if same_device else -1)
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(module.os.path, "ismount", lambda path: mounted)
    monkeypatch.setattr(
        module.os,
        "statvfs",
        lambda path: SimpleNamespace(
            f_blocks=total_mib, f_frsize=1024 * 1024, f_bavail=free_mib, f_files=10000, f_favail=inodes
        ),
    )
    if allowed:
        engine._check_storage({"DockerRootDir": str(storage)})
        assert engine.storage_root == storage
    else:
        with pytest.raises(DeployError):
            engine._check_storage({"DockerRootDir": str(storage)})


def test_cleanup_removes_owned_candidate_tags_without_forcing_shared_image(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    record["snapshot"]["manifest"]["services"]["other"] = copy.deepcopy(
        record["snapshot"]["manifest"]["services"]["web"]
    )
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[:2] == ["image", "ls"]:
            return DIGEST + "\n" + DIGEST + "\n"
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Config": {"Labels": engine._labels(record)},
                        "RepoTags": [
                            "awesome/abc123-web:candidate",
                            "awesome/abc123-other:candidate",
                            "operator/retained:tag",
                        ],
                    }
                ]
            )
        return ""

    engine._docker = docker
    engine.cleanup(record)
    assert ["image", "rm", "awesome/abc123-web:candidate"] in calls
    assert ["image", "rm", "awesome/abc123-other:candidate"] in calls
    assert not any(
        args[:2] == ["image", "rm"] and (DIGEST in args or "operator/retained:tag" in args or "-f" in args)
        for args in calls
    )


def test_dependency_network_names_do_not_collide_with_service_names(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    services = record["snapshot"]["manifest"]["services"]
    for name in ("api", "web-api"):
        services[name] = copy.deepcopy(services["web"])
        record["execution"]["images"][name] = DIGEST
        record["execution"]["ports"][name] = 20000 + len(services)
    services["web"]["dependsOn"] = {"api": "started"}
    engine.start(record, lambda _: None)
    created = [args[-1] for args in engine.calls if args[:2] == ["network", "create"]]
    assert len(created) == len(set(created)) == 4


@pytest.mark.parametrize("key", ["BUILDKIT_SYNTAX", "BUILDKIT_CONTEXT_KEEP_GIT_DIR", "BUILDX_CONFIG"])
def test_runtime_rejects_build_control_args_before_pinning(tmp_path, key):
    engine = backend(tmp_path)
    record = job(tmp_path)
    source = Path(record["snapshot"]["source_dir"])
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    service = record["snapshot"]["manifest"]["services"]["web"]
    service.pop("image")
    service["build"] = {"context": ".", "dockerfile": "Dockerfile", "args": {key: "evil/frontend:latest"}}
    with pytest.raises(DeployError, match="reserved"):
        engine.freeze(record["snapshot"], record, lambda _: None)
    record["snapshot"]["build_contexts"] = {"web": str(source)}
    record["execution"]["images"] = {}
    engine._builder = lambda: None
    with pytest.raises(DeployError, match="reserved"):
        engine.build(record, lambda _: None)
    assert not any(args[:2] == ["buildx", "build"] for args in engine.calls)


def test_source_failure_cleanup_never_requires_docker(tmp_path):
    engine = backend(tmp_path)
    record = job(tmp_path)
    record["execution"] = {}
    source = engine.settings.state_dir / "sources" / record["id"]
    source.mkdir(parents=True)
    engine._docker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Docker should not be needed")
    )
    engine.cleanup(record)
    assert not source.exists()


def recovery_backend(tmp_path, states):
    engine = backend(tmp_path)
    record = job(tmp_path)
    record["status"] = "succeeded"
    service = record["snapshot"]["manifest"]["services"]["web"]
    for name in states:
        if name != "web":
            record["snapshot"]["manifest"]["services"][name] = copy.deepcopy(service)
            record["execution"]["images"][name] = DIGEST
            record["execution"]["ports"][name] = 20000 + len(record["execution"]["ports"])
    baseline = engine._docker
    engine.health_calls = []
    engine._health_service = lambda job, name, deadline: engine.health_calls.append(name)

    def docker(args, **kwargs):
        if args[:2] == ["container", "inspect"]:
            engine.calls.append(args)
            values = []
            for identifier in args[2:]:
                name = identifier.removeprefix("awesome-abc123-")
                if name not in states:
                    raise DeployError("MISSING_CONTAINER", "missing")
                svc = record["snapshot"]["manifest"]["services"][name]
                values.append(
                    {
                        "Name": "/" + identifier,
                        "Image": DIGEST,
                        "Config": {"Labels": engine._labels(record), "User": "65532:65532", "Image": DIGEST},
                        "HostConfig": {
                            "RestartPolicy": {"Name": "no"},
                            "ReadonlyRootfs": True,
                            "PortBindings": {
                                str(svc["port"]) + "/tcp": [
                                    {
                                        "HostIp": "127.0.0.1",
                                        "HostPort": str(record["execution"]["ports"][name]),
                                    }
                                ]
                            },
                        },
                        "NetworkSettings": {"Networks": {"owned-internal": {}}},
                        "State": {"Running": states[name], "Paused": False, "Restarting": False},
                        "Mounts": [],
                    }
                )
            return json.dumps(values)
        if args[:2] == ["network", "inspect"]:
            engine.calls.append(args)
            return json.dumps(
                [{"Name": name, "Internal": True, "Labels": engine._labels(record)} for name in args[2:]]
            )
        if args[:2] == ["container", "start"]:
            engine.calls.append(args)
            states[args[-1].removeprefix("awesome-abc123-")] = True
            return ""
        return baseline(args, **kwargs)

    engine._docker = docker
    record["execution"]["networks"] = ["owned-internal"]
    return engine, record


def test_recovery_restarts_legacy_no_policy_in_dependency_order_without_deployment(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": False, "api": False})
    record["snapshot"]["manifest"]["services"]["web"]["dependsOn"] = {"api": "healthy"}
    engine.preflight = lambda: (_ for _ in ()).throw(AssertionError("full build admission is not recovery"))
    result = engine.recover_active(record)
    starts = [args[-1] for args in engine.calls if args[:2] == ["container", "start"]]
    assert starts == ["awesome-abc123-api", "awesome-abc123-web"]
    assert result["restarted"] == ["api", "web"]
    assert engine.health_calls == ["api", "web"]
    assert not any(
        args[0] in {"pull", "buildx"} or args[1:2] in [["create"], ["rm"]] for args in engine.calls
    )


def test_recovery_running_containers_skip_start_and_health(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": True})
    assert engine.recover_active(record) == {"restarted": []}
    assert engine.health_calls == []
    assert not any(args[:2] == ["container", "start"] for args in engine.calls)


@pytest.mark.parametrize("change", ["missing", "image", "port", "owner", "released", "failed"])
def test_recovery_refuses_missing_or_changed_release_without_recreating(tmp_path, change):
    engine, record = recovery_backend(tmp_path, {"web": False})
    before = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["container", "inspect"]:
            if change == "missing":
                raise DeployError("MISSING", "container absent")
            rows = json.loads(before(args, **kwargs))
            if change == "image":
                rows[0]["Image"] = "sha256:" + "f" * 64
            if change == "port":
                rows[0]["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostPort"] = "29999"
            if change == "owner":
                rows[0]["Config"]["Labels"]["awesome.job"] = "foreign"
            return json.dumps(rows)
        return before(args, **kwargs)

    engine._docker = docker
    if change == "released":
        record["execution"]["resources_released"] = True
    if change == "failed":
        record["status"] = "failed"
    with pytest.raises(DeployError):
        engine.recover_active(record)
    assert not any(args[:2] in [["container", "start"], ["container", "create"]] for args in engine.calls)


def test_recovery_rechecks_running_health_after_previous_attempt_failed(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": True})
    record["execution"]["active_recovery"] = {"retry_health": True}
    assert engine.recover_active(record) == {"restarted": []}
    assert engine.health_calls == ["web"]
    assert not any(args[:2] == ["container", "start"] for args in engine.calls)
