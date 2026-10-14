import copy
import json
from types import SimpleNamespace

import pytest

from test_gateway import request, setup_state


def offline_request(release="old"):
    return request("deactivate", release)


def offline_gateway(tmp_path):
    module = setup_state(tmp_path)
    module.probe_deactivated = lambda req, token, **kwargs: None
    return module


def test_deactivate_protocol_is_the_existing_bounded_release_request(tmp_path):
    module = setup_state(tmp_path)
    assert module.validate(offline_request())["op"] == "deactivate"


def test_deactivate_journals_before_tombstone_and_only_commits_after_probe(tmp_path):
    module = offline_gateway(tmp_path)
    observed = []

    def probe(req, token, **kwargs):
        state = module.read_state("demo")
        assert state["current"]["release"] == "old"
        assert state["deactivation"]["completed"] is False
        text = module.config_path("demo").read_text()
        assert "proxy_pass" not in text
        assert text.count("server_name demo.example.com;") == 2
        assert "listen 80;" in text and "listen 443 ssl;" in text
        assert text.count("return 410;") == 2
        assert text.count("location ^~ /.well-known/acme-challenge/") == 2
        assert "/etc/letsencrypt/live/demo.example.com/fullchain.pem" in text
        assert f'X-Awesome-Offline "old:{token}" always;' in text
        observed.append(token)

    module.probe_deactivated = probe
    assert module.deactivate(offline_request()) == {"deactivated": True, "release": "old"}
    state = module.read_state("demo")
    assert state["current"] is state["pending"] is None
    assert state["deactivation"]["completed"] is True
    assert len(observed) == 1


@pytest.mark.parametrize("patch", [
    {"release": "stale"}, {"ports": {"web": 20002}},
    {"routes": [{"path": "/else", "match": "exact", "service": "web", "stripPrefix": False}]},
])
def test_deactivate_rejects_stale_release_ports_or_routes_before_mutation(tmp_path, patch):
    module = offline_gateway(tmp_path)
    previous = module.config_path("demo").read_text(), module.read_state("demo")
    req = offline_request()
    req.update(patch)
    with pytest.raises(ValueError):
        module.deactivate(req)
    assert (module.config_path("demo").read_text(), module.read_state("demo")) == previous


def test_deactivate_refuses_unknown_missing_current_and_different_pending_publish(tmp_path):
    module = offline_gateway(tmp_path)
    state = module.read_state("demo")
    state["pending"]["release"] = "newer"
    module.save_state("demo", state)
    with pytest.raises(ValueError):
        module.deactivate(offline_request())
    module.save_state("demo", {"current": None, "pending": None})
    with pytest.raises(ValueError):
        module.deactivate(offline_request())


def test_deactivate_failed_intent_checkpoint_does_not_touch_config(tmp_path):
    module = offline_gateway(tmp_path)
    text = module.config_path("demo").read_text()
    module.save_state = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        module.deactivate(offline_request())
    assert module.config_path("demo").read_text() == text


@pytest.mark.parametrize("crash_point", ["reload", "completed_state"])
def test_deactivate_crash_replays_durable_intent_with_same_token(tmp_path, crash_point):
    module = offline_gateway(tmp_path)
    save = module.save_state
    if crash_point == "reload":
        module.run = lambda args, **kw: (_ for _ in ()).throw(SystemExit()) if "reload" in args else ""
    else:
        def crash_save(name, state):
            if state.get("deactivation", {}).get("completed"):
                raise SystemExit()
            save(name, state)
        module.save_state = crash_save
    with pytest.raises(SystemExit):
        module.deactivate(offline_request())
    state = module.read_state("demo")
    token = state["deactivation"]["token"]
    assert state["deactivation"]["completed"] is False
    assert "return 410;" in module.config_path("demo").read_text()
    module.run = lambda args, **kw: ""
    module.save_state = save
    assert module.deactivate(offline_request())["deactivated"] is True
    assert module.read_state("demo")["deactivation"]["token"] == token


def test_deactivate_probe_failure_retains_intent_and_blocks_old_publish_rollback_verify(tmp_path):
    module = offline_gateway(tmp_path)
    module.probe_deactivated = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("old nginx worker"))
    with pytest.raises(RuntimeError, match="old nginx worker"):
        module.deactivate(offline_request())
    state = module.read_state("demo")
    assert state["current"]["release"] == "old"
    assert state["deactivation"]["completed"] is False
    for operation in (module.publish, module.rollback, module.verify):
        with pytest.raises(ValueError):
            operation(request(operation.__name__, "old"))
    assert module.read_state("demo") == state
    module.probe_deactivated = lambda *args, **kwargs: None
    assert module.deactivate(offline_request())["deactivated"] is True


def test_completed_deactivate_replay_reprobes_and_never_restores_former_release(tmp_path):
    module = offline_gateway(tmp_path)
    module.publish(request())
    module.deactivate(offline_request("new"))
    tombstone = module.config_path("demo").read_text()
    tokens = []
    module.probe_deactivated = lambda req, token, **kwargs: tokens.append(token)
    assert module.deactivate(offline_request("new"))["deactivated"] is True
    assert len(tokens) == 1
    with pytest.raises(ValueError):
        module.rollback(request("rollback"))
    assert module.config_path("demo").read_text() == tombstone


def test_completed_deactivate_rejects_changed_routes_and_unknown_config(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    state = module.read_state("demo")
    with pytest.raises(ValueError):
        module.deactivate({**offline_request(), "ports": {"web": 20002}})
    module.config_path("demo").write_text("# unexpected newer configuration\n")
    with pytest.raises(ValueError):
        module.deactivate(offline_request())
    assert module.read_state("demo") == state
    assert module.config_path("demo").read_text() == "# unexpected newer configuration\n"


def test_deactivate_reload_failure_restores_prior_config_but_retains_offline_intent(tmp_path):
    module = offline_gateway(tmp_path)
    before = module.config_path("demo").read_text()
    calls = []

    def run(args, **kwargs):
        if "reload" in args:
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("reload failed")
        return ""

    module.run = run
    with pytest.raises(RuntimeError, match="reload failed"):
        module.deactivate(offline_request())
    assert module.config_path("demo").read_text() == before
    assert module.read_state("demo")["deactivation"]["completed"] is False
    assert module.deactivate(offline_request())["deactivated"] is True


def test_new_publish_after_deactivate_rolls_back_to_verified_tombstone(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    tombstone = module.config_path("demo").read_text()
    marker = module.read_state("demo")["deactivation"]
    req = request()
    req["previous_release"] = None
    module.publish(req)
    state = module.read_state("demo")
    assert state.get("deactivation") is None
    assert state["pending"]["previous_deactivation"] == marker
    with pytest.raises(ValueError):
        module.deactivate(offline_request())
    probes = []
    module.probe_deactivated = lambda req, token, **kwargs: probes.append(token)
    module.probe_routes = lambda req: (_ for _ in ()).throw(RuntimeError("candidate fails"))
    with pytest.raises(RuntimeError, match="candidate fails"):
        module.verify({**req, "op": "verify"})
    state = module.read_state("demo")
    assert state["current"] is state["pending"] is None
    assert state["deactivation"] == marker
    assert module.config_path("demo").read_text() == tombstone
    assert probes == [marker["token"]]


def test_publish_cannot_reuse_deactivated_release_or_wrong_previous(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    for req in (request(release="old"), request()):
        with pytest.raises(ValueError):
            module.publish(req)


def test_crashed_publish_after_offline_cannot_be_overwritten_by_old_deactivate(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    req = {**request(), "previous_release": None}
    save = module.save_state

    def crash_save(name, state):
        if state["current"]:
            raise SystemExit()
        save(name, state)

    module.save_state = crash_save
    with pytest.raises(SystemExit):
        module.publish(req)
    text = module.config_path("demo").read_text()
    module.save_state = save
    for invalid in (offline_request(),):
        with pytest.raises(ValueError):
            module.deactivate(invalid)
    with pytest.raises(ValueError):
        module.publish({**req, "release": "different"})
    assert module.config_path("demo").read_text() == text
    assert module.publish(req)["release"] == "new"


def test_failed_tombstone_rollback_probe_keeps_new_publish_journal(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    req = {**request(), "previous_release": None}
    module.publish(req)
    module.probe_deactivated = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not offline yet"))
    with pytest.raises(RuntimeError, match="not offline yet"):
        module.rollback({**req, "op": "rollback"})
    state = module.read_state("demo")
    assert state["current"]["release"] == state["pending"]["release"] == "new"
    module.probe_deactivated = lambda *args, **kwargs: None
    assert module.rollback({**req, "op": "rollback"})["rolled_back"] is True
    assert module.read_state("demo")["deactivation"]["completed"] is True


def test_rollback_before_new_publish_checks_offline_route_without_restoring_anything(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    state, text = module.read_state("demo"), module.config_path("demo").read_text()
    probes = []
    module.probe_deactivated = lambda req, token, **kwargs: probes.append(token)
    assert module.rollback({**request("rollback"), "previous_release": None})["rolled_back"] is True
    assert probes == [state["deactivation"]["token"]]
    assert module.read_state("demo") == state
    assert module.config_path("demo").read_text() == text


def probe_gateway(tmp_path, outcomes):
    module = setup_state(tmp_path)
    module.PROBE_LOG = tmp_path / "probe.log"
    clock = SimpleNamespace(now=0)
    module.time = SimpleNamespace(monotonic=lambda: clock.now,
                                  sleep=lambda n: setattr(clock, "now", clock.now + n))
    attempts = []

    class Connection:
        def __init__(self, host, timeout, context=None):
            self.host, self.timeout = host, timeout
            self.tls = context is not None
            self.closed = False
            attempts.append(self)

        def request(self, method, path, headers):
            self.path, self.headers = path, headers
            assert method == "GET" and headers["Connection"] == "close"

        def getresponse(self):
            clock.now += min(self.timeout, 0.05)
            outcome = outcomes[min(len(attempts) - 1, len(outcomes) - 1)]
            if callable(outcome):
                outcome = outcome(self)
            if isinstance(outcome, Exception):
                raise outcome
            status, marker = outcome
            nonce = self.headers.get("X-Awesome-Probe")
            if nonce:
                with module.PROBE_LOG.open("a") as stream:
                    stream.write(json.dumps({"probe": nonce, "host": self.host, "uri": self.path.split("?")[0],
                        "upstream": "-", "status": status, "pid": 2}) + "\n")
            return SimpleNamespace(status=status, getheader=lambda key:
                {"X-Awesome-Offline": marker, "X-Awesome-Gateway-Worker": "2"}.get(key))

        def close(self):
            self.closed = True

    module.LocalHTTPS = module.LocalHTTP = Connection
    return module, clock, attempts


def test_offline_probe_fresh_connections_verify_both_protocols_and_all_paths(tmp_path):
    token = "a" * 64
    module, clock, attempts = probe_gateway(tmp_path, [(200, None), (410, "old:" + token)])
    req = offline_request()
    req["routes"].append({"path": "/api", "match": "exact", "service": "web", "stripPrefix": False})
    module.probe_deactivated(req, token)
    assert len(attempts) >= 7
    assert {row.tls for row in attempts} == {True, False}
    assert all(row.closed for row in attempts)
    assert any(row.path.startswith("/api?") for row in attempts)
    assert len({row.path for row in attempts}) == len(attempts)
    assert clock.now < 30


def test_http_success_cannot_replace_verified_https_offline_response(tmp_path):
    token = "a" * 64
    module, clock, attempts = probe_gateway(tmp_path, [
        lambda connection: (200, None) if connection.tls else (410, "old:" + token),
    ])
    with pytest.raises(RuntimeError, match="deadline"):
        module.probe_deactivated(offline_request(), token)
    assert any(row.tls for row in attempts) and any(not row.tls for row in attempts)
    assert 30 <= clock.now < 30.1


@pytest.mark.parametrize("outcome", [(410, "other:token"), (200, "old:" + "a" * 64),
                                     ConnectionRefusedError("reload")])
def test_offline_probe_rejects_wrong_header_status_and_connection_failure_at_total_deadline(tmp_path, outcome):
    module, clock, attempts = probe_gateway(tmp_path, [outcome])
    req = copy.deepcopy(offline_request())
    req["routes"] *= 10
    with pytest.raises(RuntimeError, match="deadline"):
        module.probe_deactivated(req, "a" * 64)
    assert 30 <= clock.now < 30.1
    assert all(row.closed for row in attempts)
