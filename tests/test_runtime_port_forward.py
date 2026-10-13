import copy
from dataclasses import replace
import json

import pytest

from awesome_deploy.errors import DeployError
from test_runtime import DIGEST, backend, job, recovery_backend


def test_started_dependency_recovery_starts_api_before_web_health(tmp_path):
    states = {"web": True, "api": False}
    engine, record = recovery_backend(tmp_path, states)
    record["snapshot"]["manifest"]["services"]["api"]["dependsOn"] = {"web": "started"}
    record["execution"]["networks"].append("aw-abc123-e-3-api-3-web")
    checked = []

    def health(job, name, deadline):
        if name == "web" and not states["api"]:
            raise DeployError("HEALTH_FAILED", "web requires the started API")
        checked.append(name)

    engine._health_service = health
    assert engine.recover_active(record, lambda delta: None) == {"restarted": ["api"]}
    assert checked == ["web", "api"]


def test_start_journals_unconfirmed_then_confirmed_around_mapping_creation(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    events = []

    def checkpoint(delta):
        if "port_forwards" in delta:
            intent = delta["port_forwards"]["web"]
            events.append(("checkpoint", intent["confirmed"]))

    def ensure(spec, timeout=5, *, create=True):
        events.append(("ensure", create))
        assert record["execution"]["port_forwards"]["web"]["confirmed"] is False
        return {"id": 1, "spec": spec}

    engine.port_forwarder.ensure = ensure
    engine.start(record, checkpoint)
    assert events == [("checkpoint", False), ("ensure", True), ("checkpoint", True)]
    assert record["execution"]["port_forwards"]["web"]["confirmed"] is True


def test_unconfirmed_missing_mapping_cleanup_keeps_all_resources(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    record["execution"]["port_forwards"]["web"]["confirmed"] = False
    calls = []

    def remove(spec, timeout=5, *, require_present=False):
        calls.append(require_present)
        if require_present:
            raise DeployError("PORT_FORWARD_UNCONFIRMED", "still uncertain")

    engine.port_forwarder.remove = remove
    engine.calls.clear()
    with pytest.raises(DeployError) as error:
        engine.cleanup(record)
    assert error.value.code == "PORT_FORWARD_UNCONFIRMED"
    assert calls == [True]
    assert not engine.calls


def test_start_persists_forward_before_ensure_and_dependency_health(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    services = record["snapshot"]["manifest"]["services"]
    services["api"] = copy.deepcopy(services["web"])
    services["web"]["dependsOn"] = {"api": "healthy"}
    record["execution"]["ports"]["api"] = 20001
    record["execution"]["images"]["api"] = DIGEST
    events = []

    def checkpoint(delta):
        if "port_forwards" in delta:
            assert record["execution"].get("port_forwards") != delta["port_forwards"]
            events.append(("checkpoint", copy.deepcopy(delta["port_forwards"])))

    def ensure(spec, timeout=5, **policy):
        assert spec in [value["spec"] for value in record["execution"]["port_forwards"].values()]
        events.append(("ensure", spec["parentPort"]))
        return {"id": 1, "spec": spec}

    engine.port_forwarder.ensure = ensure
    engine._health_service = lambda job, name, deadline: events.append(("health", name))
    engine.start(record, checkpoint)
    assert [event[0] for event in events] == [
        "checkpoint",
        "ensure",
        "checkpoint",
        "health",
        "checkpoint",
        "ensure",
        "checkpoint",
    ]
    saved = record["execution"]["port_forwards"]["web"]
    assert saved["spec"] == {
        "proto": "tcp4",
        "parentIP": "127.0.0.1",
        "parentPort": 20000,
        "childIP": "172.20.0.2",
        "childPort": 8080,
    }
    assert saved["network"]["name"] == "aw-abc123-s-3-web"
    assert len(saved["network"]["id"]) == len(saved["container_id"]) == 64


def test_failed_forward_checkpoint_never_creates_mapping_or_changes_local_intent(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    ensured = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)

    def checkpoint(delta):
        if "port_forwards" in delta:
            raise OSError("checkpoint unavailable")

    with pytest.raises(OSError):
        engine.start(record, checkpoint)
    assert not ensured
    assert not record["execution"].get("port_forwards")


@pytest.mark.parametrize(
    "change", ["public_ip", "loopback", "foreign_label", "unknown_network", "network_id"]
)
def test_start_rejects_untrusted_forward_target(tmp_path, change):
    engine, record = backend(tmp_path), job(tmp_path)
    baseline = engine._docker
    ensured = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)

    def docker(args, **kwargs):
        raw = baseline(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            networks = rows[0]["NetworkSettings"]["Networks"]
            own = networks["aw-abc123-s-3-web"]
            if change == "public_ip":
                own["IPAddress"] = "8.8.8.8"
            elif change == "loopback":
                own["IPAddress"] = "127.0.0.1"
            elif change == "foreign_label":
                rows[0]["Config"]["Labels"]["awesome.app"] = "foreign"
            elif change == "unknown_network":
                networks["foreign"] = own
            else:
                own["NetworkID"] = "f" * 64
            return json.dumps(rows)
        return raw

    engine._docker = docker
    with pytest.raises(DeployError):
        engine.start(record, lambda delta: None)
    assert not ensured


def test_cleanup_removes_exact_forward_before_any_docker_resource(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    saved = copy.deepcopy(record["execution"]["port_forwards"]["web"]["spec"])
    events = []
    engine.port_forwarder.remove = lambda spec, timeout=5, **policy: events.append(("remove", spec))
    engine._docker = lambda args, **kwargs: events.append(("docker", args)) or ""
    engine.cleanup(record)
    assert events[0] == ("remove", saved)


def test_cleanup_forward_failure_keeps_containers_networks_and_local_files(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    source = engine.settings.state_dir / "sources" / record["id"]
    source.mkdir(parents=True)
    engine.calls.clear()

    def failed(spec, timeout=5, **policy):
        raise DeployError("PORT_FORWARD_UNAVAILABLE", "not available")

    engine.port_forwarder.remove = failed
    with pytest.raises(DeployError):
        engine.cleanup(record)
    assert not engine.calls
    assert source.exists()


def test_runtime_preflight_checks_rootlesskit(tmp_path):
    engine = backend(tmp_path)
    checked = []
    engine.port_forwarder.check = lambda timeout=5: checked.append(timeout)
    engine.preflight()
    assert checked and 0 < checked[0] <= 5


def test_rootlesskit_is_the_only_host_port_publisher(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    created = next(args for args in engine.calls if args[:2] == ["container", "create"])
    assert "--publish" not in created


def test_recovery_reconciles_mapping_for_running_container_before_health(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": True})
    events = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: events.append(("ensure", spec))
    engine._health_service = lambda job, name, deadline: events.append(("health", name))
    engine.recover_active(record, lambda delta: None)
    assert [event[0] for event in events] == ["ensure", "health"]


@pytest.mark.parametrize("change", ["container_id", "ip", "network_id", "missing_forward"])
def test_recovery_refuses_changed_forward_identity(tmp_path, change):
    engine, record = recovery_backend(tmp_path, {"web": True})
    if change == "missing_forward":
        record["execution"].pop("port_forwards", None)
    else:
        baseline = engine._docker

        def docker(args, **kwargs):
            raw = baseline(args, **kwargs)
            if args[:2] == ["container", "inspect"]:
                rows = json.loads(raw)
                if change == "container_id":
                    rows[0]["Id"] = "f" * 64
                else:
                    network = rows[0]["NetworkSettings"]["Networks"]["aw-abc123-s-3-web"]
                    network["IPAddress" if change == "ip" else "NetworkID"] = (
                        "172.20.0.99" if change == "ip" else "f" * 64
                    )
                return json.dumps(rows)
            return raw

        engine._docker = docker
    ensured = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)
    with pytest.raises(DeployError) as failure:
        engine.recover_active(record, lambda delta: None)
    assert failure.value.code == "PORT_FORWARD_IDENTITY"
    assert not ensured


def test_recovery_starts_then_reinspects_before_forward_and_health(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": False})
    baseline = engine._docker
    events = []

    def docker(args, **kwargs):
        events.append(tuple(args[:2]))
        return baseline(args, **kwargs)

    engine._docker = docker
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: events.append(("ensure",))
    engine._health_service = lambda job, name, deadline: events.append(("health",))
    engine.recover_active(record, lambda delta: None)
    start = events.index(("container", "start"))
    inspect = events.index(("container", "inspect"), start)
    assert start < inspect < events.index(("ensure",)) < events.index(("health",))


def test_recovery_does_not_retarget_when_ip_changes_after_start(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": False})
    baseline = engine._docker
    original = copy.deepcopy(record["execution"]["port_forwards"])

    def docker(args, **kwargs):
        raw = baseline(args, **kwargs)
        if args[:2] == ["container", "inspect"]:
            rows = json.loads(raw)
            if rows[0]["State"]["Running"]:
                rows[0]["NetworkSettings"]["Networks"]["aw-abc123-s-3-web"]["IPAddress"] = "172.20.0.99"
            return json.dumps(rows)
        return raw

    ensured = []
    engine._docker = docker
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)
    with pytest.raises(DeployError) as failure:
        engine.recover_active(record, lambda delta: None)
    assert failure.value.code == "PORT_FORWARD_IDENTITY"
    assert not ensured and not engine.health_calls
    assert record["execution"]["port_forwards"] == original


def test_resume_start_does_not_overwrite_existing_target(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    record["execution"]["port_forwards"]["web"]["spec"]["childIP"] = "172.20.0.99"
    original = copy.deepcopy(record["execution"]["port_forwards"])
    ensured = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)
    with pytest.raises(DeployError) as failure:
        engine.start(record, lambda delta: None)
    assert failure.value.code == "PORT_FORWARD_IDENTITY"
    assert not ensured
    assert record["execution"]["port_forwards"] == original


def test_mapping_conflict_during_recovery_never_runs_health(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": True})

    def conflict(spec, timeout=5, **policy):
        raise DeployError("PORT_FORWARD_CONFLICT", "already bound")

    engine.port_forwarder.ensure = conflict
    with pytest.raises(DeployError):
        engine.recover_active(record, lambda delta: None)
    assert not engine.health_calls


@pytest.mark.parametrize("forward_state", [None, [], False])
def test_cleanup_rejects_corrupt_forward_map_before_any_docker_operation(tmp_path, forward_state):
    engine, record = backend(tmp_path), job(tmp_path)
    record["execution"]["port_forwards"] = forward_state
    with pytest.raises(DeployError) as failure:
        engine.cleanup(record)
    assert failure.value.code == "PORT_FORWARD_IDENTITY"
    assert not engine.calls


def test_cleanup_without_forward_intent_never_contacts_rootlesskit(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    record["execution"] = {}

    def unexpected(*args, **kwargs):
        raise AssertionError("early failures have no mapping")

    engine.port_forwarder.remove = unexpected
    engine.port_forwarder.check = unexpected
    engine.cleanup(record)


def test_second_forward_checkpoint_failure_retains_only_first_persisted_intent(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    services = record["snapshot"]["manifest"]["services"]
    services["api"] = copy.deepcopy(services["web"])
    record["execution"]["images"]["api"] = DIGEST
    record["execution"]["ports"]["api"] = 20001
    ensured = []
    engine.port_forwarder.ensure = lambda spec, timeout=5, **policy: ensured.append(spec)

    def checkpoint(delta):
        if len(delta.get("port_forwards", {})) == 2:
            raise OSError("checkpoint unavailable")

    with pytest.raises(OSError):
        engine.start(record, checkpoint)
    assert set(record["execution"]["port_forwards"]) == {"web"}
    assert [spec["parentPort"] for spec in ensured] == [20000]


def test_ensure_failure_leaves_persisted_intent_available_for_cleanup(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    saved = []

    def unavailable(spec, timeout=5, **policy):
        raise DeployError("PORT_FORWARD_UNAVAILABLE", "not available")

    engine.port_forwarder.ensure = unavailable
    with pytest.raises(DeployError):
        engine.start(record, lambda delta: saved.append(copy.deepcopy(delta)))
    intended = record["execution"]["port_forwards"]["web"]
    assert any(patch.get("port_forwards", {}).get("web") == intended for patch in saved)
    removed = []
    engine.port_forwarder.remove = lambda spec, timeout=5, **policy: removed.append(spec)
    engine.cleanup(record)
    assert removed == [intended["spec"]]


def test_cleanup_removes_all_forwards_before_any_container_even_when_second_fails(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    services = record["snapshot"]["manifest"]["services"]
    services["api"] = copy.deepcopy(services["web"])
    record["execution"]["images"]["api"] = DIGEST
    record["execution"]["ports"]["api"] = 20001
    engine.start(record, lambda delta: None)
    engine.calls.clear()
    removed = []

    def remove(spec, timeout=5, **policy):
        removed.append(spec)
        if len(removed) == 2:
            raise DeployError("PORT_FORWARD_UNAVAILABLE", "not available")

    engine.port_forwarder.remove = remove
    with pytest.raises(DeployError):
        engine.cleanup(record)
    assert len(removed) == 2
    assert not engine.calls


@pytest.mark.parametrize("confirmed", [False, True])
def test_recovery_missing_mapping_only_rebuilds_previously_confirmed_intent(tmp_path, confirmed):
    engine, record = recovery_backend(tmp_path, {"web": True})
    record["execution"]["port_forwards"]["web"]["confirmed"] = confirmed
    events = []

    def ensure(spec, timeout=5, *, create=True):
        events.append(("ensure", create))
        if not create:
            raise DeployError("PORT_FORWARD_UNCONFIRMED", "not present")
        assert record["execution"]["port_forwards"]["web"]["confirmed"] is False
        return {"id": 1, "spec": spec}

    def checkpoint(delta):
        events.append(("checkpoint", delta["port_forwards"]["web"]["confirmed"]))

    engine.port_forwarder.ensure = ensure
    if confirmed:
        engine.recover_active(record, checkpoint)
        assert events == [("ensure", False), ("checkpoint", False), ("ensure", True), ("checkpoint", True)]
        assert record["execution"]["port_forwards"]["web"]["confirmed"] is True
        assert engine.health_calls == ["web"]
    else:
        with pytest.raises(DeployError) as error:
            engine.recover_active(record, checkpoint)
        assert error.value.code == "PORT_FORWARD_UNCONFIRMED"
        assert events == [("ensure", False)]
        assert record["execution"]["port_forwards"]["web"]["confirmed"] is False
        assert not engine.health_calls


def test_recovery_adopts_unconfirmed_exact_mapping_without_post(tmp_path):
    engine, record = recovery_backend(tmp_path, {"web": True})
    record["execution"]["port_forwards"]["web"]["confirmed"] = False
    events = []

    def ensure(spec, timeout=5, *, create=True):
        assert create is False
        events.append(("ensure", create))
        return {"id": 1, "spec": spec}

    engine.port_forwarder.ensure = ensure
    engine.recover_active(
        record, lambda delta: events.append(("checkpoint", delta["port_forwards"]["web"]["confirmed"]))
    )
    assert events == [("ensure", False), ("checkpoint", True)]
    assert record["execution"]["port_forwards"]["web"]["confirmed"] is True


def test_resume_unconfirmed_start_does_not_repeat_post(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    engine.start(record, lambda delta: None)
    record["execution"]["port_forwards"]["web"]["confirmed"] = False
    created = []

    def ensure(spec, timeout=5, *, create=True):
        created.append(create)
        if not create:
            raise DeployError("PORT_FORWARD_UNCONFIRMED", "pending server handler")
        return {"id": 1, "spec": spec}

    engine.port_forwarder.ensure = ensure
    with pytest.raises(DeployError) as error:
        engine.start(record, lambda delta: None)
    assert error.value.code == "PORT_FORWARD_UNCONFIRMED"
    assert created == [False]


def test_confirmation_checkpoint_failure_retains_false_and_cannot_complete_start(tmp_path):
    engine, record = backend(tmp_path), job(tmp_path)
    patches = []

    def checkpoint(delta):
        patches.append(copy.deepcopy(delta))
        if delta.get("port_forwards", {}).get("web", {}).get("confirmed") is True:
            raise OSError("confirmation checkpoint unavailable")

    with pytest.raises(OSError):
        engine.start(record, checkpoint)
    assert record["execution"]["port_forwards"]["web"]["confirmed"] is False
    assert not any("started" in patch for patch in patches)


@pytest.mark.parametrize("confirmed", [None, 0, 1, "false", [], {}])
def test_forward_confirmed_flag_requires_strict_bool(tmp_path, confirmed):
    engine, record = recovery_backend(tmp_path, {"web": True})
    record["execution"]["port_forwards"]["web"]["confirmed"] = confirmed
    with pytest.raises(DeployError) as error:
        engine.recover_active(record, lambda delta: None)
    assert error.value.code == "PORT_FORWARD_IDENTITY"


def test_active_preflight_preserves_total_recovery_deadline(tmp_path, monkeypatch):
    engine, record = recovery_backend(tmp_path, {"web": True})
    engine.settings = replace(engine.settings, health_timeout=1)
    clock = [100.0]
    monkeypatch.setattr("awesome_deploy.runtime.time.monotonic", lambda: clock[0])
    previous = engine._docker

    def docker(args, **kwargs):
        if args[0] == "info":
            clock[0] += 0.75
        return previous(args, **kwargs)

    budgets = []
    engine._docker = docker
    engine.port_forwarder.check = lambda timeout=5: budgets.append(timeout)
    engine.recover_active(record, lambda delta: None)
    assert budgets == [0.25]
