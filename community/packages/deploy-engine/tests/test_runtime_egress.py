import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from awesome_deploy.cli import load_settings
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from test_runtime import backend, job, recovery_backend


POLICY = {"mode": "public-https-v1", "dns": ["223.5.5.5", "1.1.1.1"],
          "blocked_ips": ["118.196.114.85"]}
EXTERNAL = "aw-abc123-x-3-web"


def configured(settings):
    return replace(settings, runtime_network="public-https-v1",
                   runtime_blocked_ips=("118.196.114.85",),
                   egress_docker_pid_file=Path("/run/user/1000/docker.pid"))


def bridge(name):
    return ("awx-" if "-x-" in name else "awi-") + hashlib.sha256(name.encode()).hexdigest()[:11]


def identities(names):
    return {name: {"id": hashlib.sha256(name.encode()).hexdigest(),
                   "bridge": bridge(name), "internal": "-x-" not in name} for name in names}


def public_backend(tmp_path, *, recovering=False, running=True):
    if recovering:
        engine, record = recovery_backend(tmp_path, {"web": running})
    else:
        engine, record = backend(tmp_path), job(tmp_path)
    engine.settings = configured(engine.settings)
    record["snapshot"]["runtime_network"] = copy.deepcopy(POLICY)
    engine.guard_events = []
    engine.egress_guard = SimpleNamespace(
        ensure=lambda policy: engine.guard_events.append(("ensure", copy.deepcopy(policy))),
        check=lambda policy: engine.guard_events.append(("check", copy.deepcopy(policy))),
        lock_fd=None,
    )
    original = engine._docker

    def docker(args, **kwargs):
        if recovering and args[:2] == ["container", "ls"]:
            return "awesome-abc123-web"
        raw = original(args, **kwargs)
        if args[0] == "info":
            return json.dumps({**json.loads(raw), "LiveRestoreEnabled": False})
        if args[:2] == ["network", "inspect"]:
            rows = json.loads(raw)
            for row in rows:
                row.update(Internal="-x-" not in row["Name"], EnableIPv6=False,
                           Options={"com.docker.network.bridge.name": bridge(row["Name"])})
            return json.dumps(rows)
        if args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            for row in rows:
                row.setdefault("HostConfig", {})["Dns"] = POLICY["dns"]
                attached = row["NetworkSettings"]["Networks"]
                if recovering:
                    attached[EXTERNAL] = {"NetworkID": hashlib.sha256(EXTERNAL.encode()).hexdigest(),
                                          "IPAddress": "172.21.0.2"}
                for name, endpoint in attached.items():
                    endpoint["GwPriority"] = 1 if "-x-" in name else 0
            return json.dumps(rows)
        return raw

    engine._docker = docker
    if recovering:
        record["execution"]["networks"].append(EXTERNAL)
        record["execution"]["network_identities"] = identities(record["execution"]["networks"])
    return engine, record


def test_old_configuration_defaults_to_internal():
    settings = Settings()
    assert settings.runtime_network == "internal"
    assert settings.runtime_dns == ("223.5.5.5", "1.1.1.1")
    assert settings.runtime_blocked_ips == ()
    assert settings.egress_docker_pid_file is None


def test_public_configuration_parses_lists_and_preserves_pid_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"runtime_network": "public-https-v1",
                               "runtime_dns": POLICY["dns"],
                               "runtime_blocked_ips": POLICY["blocked_ips"],
                               "egress_docker_pid_file": "/run/user/1000/docker.pid"}))
    settings = load_settings(path)
    assert settings.runtime_dns == tuple(POLICY["dns"])
    assert settings.runtime_blocked_ips == tuple(POLICY["blocked_ips"])
    assert settings.egress_docker_pid_file == Path("/run/user/1000/docker.pid")


@pytest.mark.parametrize("patch", [
    {"runtime_network": "public"}, {"runtime_network": None},
    {"runtime_dns": []}, {"runtime_dns": "223.5.5.5"},
    {"runtime_dns": ["127.0.0.1"]}, {"runtime_dns": ["10.0.0.1"]},
    {"runtime_dns": ["223.5.5.5", "223.5.5.5"]},
    {"runtime_blocked_ips": []}, {"runtime_blocked_ips": ["10.0.0.1"]},
    {"runtime_blocked_ips": ["example.com"]},
    {"egress_docker_pid_file": None}, {"egress_docker_pid_file": "relative.pid"},
])
def test_public_configuration_rejects_unsafe_policy(tmp_path, patch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"runtime_network": "public-https-v1",
                               "runtime_blocked_ips": POLICY["blocked_ips"],
                               "egress_docker_pid_file": "/run/user/1000/docker.pid", **patch}))
    with pytest.raises(DeployError) as caught:
        load_settings(path)
    assert caught.value.code == "INVALID_CONFIG"


def test_freeze_copies_configured_policy_without_mutating_source(tmp_path):
    engine, record = public_backend(tmp_path)
    record["snapshot"].pop("runtime_network")
    Path(record["snapshot"]["source_dir"]).mkdir()
    before = copy.deepcopy(record["snapshot"])
    frozen = engine.freeze(record["snapshot"], record, lambda patch: None)
    assert frozen["runtime_network"] == POLICY
    assert record["snapshot"] == before


def test_legacy_snapshot_does_not_acquire_exit_when_settings_change(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.settings = configured(engine.settings)
    engine.egress_guard = SimpleNamespace(ensure=lambda p: pytest.fail("legacy guard"))
    engine.start(record, lambda patch: None)
    created = next(args for args in engine.calls if args[:2] == ["container", "create"])
    assert "--dns=127.0.0.1" in created
    assert not any("-x-" in str(arg) for args in engine.calls for arg in args)


@pytest.mark.parametrize("value", [None, {}, {"mode": "unknown"}, {**POLICY, "extra": True}])
def test_invalid_snapshot_policy_fails_before_docker_mutation(tmp_path, value):
    engine, record = backend(tmp_path), job(tmp_path)
    record["snapshot"]["runtime_network"] = value
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert not any(args[:2] in (["network", "create"], ["container", "start"],
                                ["container", "create"], ["network", "connect"])
                   for args in engine.calls)


def test_public_start_has_private_ingress_and_separate_guarded_exit(tmp_path):
    engine, record = public_backend(tmp_path)
    saved = []
    engine.start(record, lambda patch: saved.append(copy.deepcopy(patch)))
    assert engine.guard_events[0] == ("ensure", POLICY)
    creates = [args for args in engine.calls if args[:2] == ["network", "create"]]
    assert len(creates) == 2
    for args in creates:
        external = args[-1] == EXTERNAL
        assert ("--internal" in args) is not external
        assert "com.docker.network.bridge.name=" + bridge(args[-1]) in args
        assert "--ipv6=false" in args
    create = next(args for args in engine.calls if args[:2] == ["container", "create"])
    assert create[create.index("--network") + 1] == "aw-abc123-s-3-web"
    assert "--dns=223.5.5.5" in create and "--dns=1.1.1.1" in create
    assert "--publish" not in create
    assert any(args[:2] == ["network", "connect"] and "--gw-priority=1" in args
               and args[-2] == EXTERNAL for args in engine.calls)
    assert record["execution"]["network_identities"] == identities(record["execution"]["networks"])
    assert any("network_identities" in patch for patch in saved)
    assert record["execution"]["port_forwards"]["web"]["network"]["name"] == "aw-abc123-s-3-web"


@pytest.mark.parametrize("change", ["internal", "driver", "bridge", "ipv6", "owner"])
def test_public_existing_network_identity_rejected(tmp_path, change):
    engine, record = public_backend(tmp_path)
    original = engine._docker

    def docker(args, **kwargs):
        if args[:2] == ["network", "ls"]:
            return EXTERNAL
        raw = original(args, **kwargs)
        if args[:2] == ["network", "inspect"]:
            rows = json.loads(raw)
            row = rows[0]
            if change == "internal":
                row["Internal"] = not row["Internal"]
            elif change == "driver":
                row["Driver"] = "host"
            elif change == "bridge":
                row["Options"]["com.docker.network.bridge.name"] = "wrong"
            elif change == "ipv6":
                row["EnableIPv6"] = True
            else:
                row["Labels"]["awesome.app"] = "foreign"
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine._ensure_network(record, EXTERNAL)
    assert not any(args[:2] == ["container", "start"] for args in engine.calls)


@pytest.mark.parametrize("change", ["dns", "priority", "network_id"])
def test_public_recovery_checks_frozen_network_identity(tmp_path, change):
    engine, record = public_backend(tmp_path, recovering=True)
    original = engine._docker

    def docker(args, **kwargs):
        raw = original(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            if change == "dns":
                rows[0]["HostConfig"]["Dns"] = ["8.8.8.8"]
            elif change == "priority":
                rows[0]["NetworkSettings"]["Networks"][EXTERNAL]["GwPriority"] = 0
            else:
                rows[0]["NetworkSettings"]["Networks"][EXTERNAL]["NetworkID"] = "f" * 64
            return json.dumps(rows)
        return raw

    engine._docker = docker
    stopped = []
    engine._stop_owned_containers = lambda target, **kwargs: stopped.append(target["id"])
    with pytest.raises(DeployError):
        engine.recover_active(record, lambda patch: None)
    assert not any(args[:2] == ["container", "start"] for args in engine.calls)
    assert not engine.health_calls
    assert stopped == [record["id"]]


def test_public_recovery_reuses_frozen_exit_without_recreating(tmp_path):
    engine, record = public_backend(tmp_path, recovering=True)
    assert engine.recover_active(record, lambda patch: None) == {"restarted": []}
    assert engine.guard_events[0] == ("check", POLICY)
    assert not any(args[:2] in (["network", "create"], ["network", "connect"],
                                ["container", "create"]) for args in engine.calls)


def test_failed_guard_stops_exact_existing_release_before_failure(tmp_path):
    engine, record = public_backend(tmp_path, recovering=True)
    events = []

    def failed(policy):
        events.append("guard")
        raise DeployError("EGRESS_UNAVAILABLE", "guard unavailable")

    engine.egress_guard.check = failed
    engine._stop_owned_containers = lambda target, **kwargs: events.append(("stop", target["id"]))
    with pytest.raises(DeployError, match="guard unavailable"):
        engine.recover_active(record, lambda patch: None)
    assert events == ["guard", ("stop", record["id"])]
    assert not engine.health_calls


def test_running_release_missing_guard_cannot_reinstall_before_stop(tmp_path):
    engine, record = public_backend(tmp_path, recovering=True)
    events = []

    def missing(frozen):
        events.append("check")
        raise DeployError("EGRESS_GUARD_MISSING", "guard missing")

    engine.egress_guard.check = missing
    engine.egress_guard.ensure = lambda frozen: events.append("install")
    engine._stop_owned_containers = lambda target, **kwargs: events.append("stop")
    with pytest.raises(DeployError):
        engine.recover_active(record, lambda patch: None)
    assert events == ["check", "stop"]


def test_public_cleanup_never_removes_global_guard(tmp_path):
    engine, record = public_backend(tmp_path)
    engine.start(record, lambda patch: None)
    engine.egress_guard = SimpleNamespace()
    engine.cleanup(record)


def test_public_readiness_checks_guard_without_installing(tmp_path):
    engine, _ = public_backend(tmp_path)
    engine._builder = lambda: None
    engine.gateway.check = lambda: None
    result = engine.check()
    assert engine.guard_events == [("check", POLICY)]
    assert result["runtime_network"] == "public-https-v1"


@pytest.mark.parametrize("value", [True, None])
def test_public_live_restore_must_be_explicitly_disabled(tmp_path, value):
    engine, record = public_backend(tmp_path)
    original = engine._docker

    def docker(args, **kwargs):
        raw = original(args, **kwargs)
        if args[0] == "info":
            return json.dumps({**json.loads(raw), "LiveRestoreEnabled": value})
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert not any(args[:2] in (["container", "create"], ["container", "start"],
                                ["network", "connect"], ["network", "create"])
                   for args in engine.calls)


def test_guard_is_rechecked_before_each_attach_and_start(tmp_path):
    engine, record = public_backend(tmp_path)
    events = []
    engine.egress_guard.check = lambda frozen: events.append("guard-check")
    original = engine._docker

    def docker(args, **kwargs):
        if args[:2] in (["network", "connect"], ["container", "start"]):
            assert events and events[-1] == "guard-check"
            events.append(tuple(args[:2]))
        return original(args, **kwargs)

    engine._docker = docker
    engine.start(record, lambda patch: None)
    assert ("network", "connect") in events and ("container", "start") in events


def test_failed_network_checkpoint_never_creates_a_container(tmp_path):
    engine, record = public_backend(tmp_path)

    def checkpoint(patch):
        if "network_identities" in patch:
            raise OSError("durability unavailable")

    with pytest.raises(OSError):
        engine.start(record, checkpoint)
    assert not any(args[:2] in (["container", "create"], ["container", "start"],
                                ["network", "connect"]) for args in engine.calls)
    assert "network_identities" not in record["execution"]


def test_stopped_public_release_can_install_guard_before_recovery(tmp_path):
    engine, record = public_backend(tmp_path, recovering=True, running=False)
    assert engine.recover_active(record, lambda patch: None) == {"restarted": ["web"]}
    assert engine.guard_events[0] == ("ensure", POLICY)
    assert ("check", POLICY) in engine.guard_events


def test_freeze_does_not_replace_a_previously_selected_policy(tmp_path):
    engine, record = public_backend(tmp_path)
    engine.settings = replace(engine.settings, runtime_dns=("1.1.1.1",))
    Path(record["snapshot"]["source_dir"]).mkdir()
    frozen = engine.freeze(record["snapshot"], record, lambda patch: None)
    assert frozen["runtime_network"] == POLICY


def test_existing_public_container_with_changed_dns_is_stopped_before_reconnect(tmp_path):
    engine, record = public_backend(tmp_path, recovering=True)
    original = engine._docker
    stopped = []
    engine._stop_owned_containers = lambda target, **kwargs: stopped.append(target["id"])

    def docker(args, **kwargs):
        if args[:2] == ["network", "ls"]:
            return args[args.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
        raw = original(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            rows[0]["HostConfig"]["Dns"] = ["8.8.8.8"]
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert stopped == [record["id"]]
    assert not any(args[:2] in (["network", "connect"], ["container", "start"])
                   for args in engine.calls)


def docker_created_endpoints(engine, *, status="created", running=False, change=None):
    """Docker 29.9 fixture: create/connect records intent before allocating endpoints."""
    original = engine._docker
    started = []

    def docker(args, **kwargs):
        if args[:2] == ["container", "start"]:
            started.append(args[-1])
        raw = original(args, **kwargs)
        if args[:2] == ["container", "inspect"] and not started:
            rows = json.loads(raw)
            for row in rows:
                row["State"].update(Status=status, Running=running)
                for endpoint in row["NetworkSettings"]["Networks"].values():
                    endpoint.update(NetworkID="", EndpointID="", IPAddress="", Gateway="",
                                    MacAddress="", IPPrefixLen=0, GlobalIPv6Address="")
                if change == "dns":
                    row["HostConfig"]["Dns"] = ["8.8.8.8"]
                elif change == "priority":
                    row["NetworkSettings"]["Networks"][EXTERNAL]["GwPriority"] = 0
                elif change == "wrong_id":
                    row["NetworkSettings"]["Networks"][EXTERNAL]["NetworkID"] = "f" * 64
                elif change == "extra":
                    row["NetworkSettings"]["Networks"]["unrecognized"] = {"NetworkID": ""}
            return json.dumps(rows)
        return raw

    engine._docker = docker
    return started


def test_created_empty_endpoint_ids_are_checked_by_network_then_started(tmp_path):
    engine, record = public_backend(tmp_path)
    started = docker_created_endpoints(engine)
    engine.start(record, lambda patch: None)
    assert started == ["awesome-abc123-web"]
    assert record["execution"]["port_forwards"]["web"]["network"]["id"] == identities(
        ["aw-abc123-s-3-web"])["aw-abc123-s-3-web"]["id"]


@pytest.mark.parametrize("status,running,change", [
    ("running", True, None), ("exited", False, None), ("created", True, None),
    ("created", False, "dns"), ("created", False, "priority"),
    ("created", False, "wrong_id"), ("created", False, "extra"),
])
def test_empty_endpoint_exception_is_only_for_pristine_created_intent(tmp_path, status, running, change):
    engine, record = public_backend(tmp_path)
    started = docker_created_endpoints(engine, status=status, running=running, change=change)
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert not started


def test_created_container_with_previous_started_checkpoint_cannot_drop_network_identity(tmp_path):
    engine, record = public_backend(tmp_path)
    record["execution"]["started"] = ["web"]
    started = docker_created_endpoints(engine)
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert not started


def test_created_empty_endpoint_rechecks_network_instance_before_start(tmp_path):
    engine, record = public_backend(tmp_path)
    started = docker_created_endpoints(engine)
    original = engine._docker
    seen_created = []

    def docker(args, **kwargs):
        raw = original(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            seen_created.append(True)
        elif args[:2] == ["network", "inspect"] and seen_created:
            rows = json.loads(raw)
            rows[0]["Id"] = "f" * 64
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert seen_created and not started


def test_started_public_container_still_requires_allocated_network_ids(tmp_path):
    engine, record = public_backend(tmp_path)
    started = docker_created_endpoints(engine)
    original = engine._docker
    stopped = []
    engine._stop_owned_containers = lambda target, **kwargs: stopped.append(target["id"])

    def docker(args, **kwargs):
        raw = original(args, **kwargs)
        if args[:2] == ["container", "inspect"] and started:
            rows = json.loads(raw)
            rows[0]["State"].update(Status="running", Running=True)
            rows[0]["NetworkSettings"]["Networks"][EXTERNAL]["NetworkID"] = ""
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.start(record, lambda patch: None)
    assert started and stopped == [record["id"]]
    assert "port_forwards" not in record["execution"]
