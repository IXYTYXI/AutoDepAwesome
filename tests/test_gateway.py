import importlib.machinery
import importlib.util
import json
from pathlib import Path
import ssl
from types import SimpleNamespace

import pytest


def helper():
    path = Path(__file__).parents[1] / "ops" / "awesome-gateway"
    loader = importlib.machinery.SourceFileLoader("gateway_helper", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def request(op="publish", release="new"):
    return {
        "op": op,
        "base_domain": "example.com",
        "subdomain": "demo",
        "release": release,
        "previous_release": "old" if release != "old" else None,
        "ports": {"web": 20001},
        "routes": [{"path": "/abc", "match": "prefix", "service": "web", "stripPrefix": True}],
    }


def test_segment_boundary_and_acme_protection():
    module = helper()
    module.CONFIG = {
        "base_domain": "example.com",
        "email": "ops@example.com",
        "port_start": 20000,
        "port_end": 29999,
    }
    text = module.render(request(), tls=True)
    assert "location = /abc {" in text
    assert "location ^~ /abc/ {" in text
    assert "location /abc {" not in text
    assert "location ^~ /.well-known/acme-challenge/" in text
    assert "proxy_pass http://127.0.0.1:20001/;" in text


@pytest.mark.parametrize(
    "patch",
    [
        {"subdomain": "a;\n"},
        {"release": "../../x"},
        {"ports": {"web": "80;evil"}},
        {"routes": [{"path": "/a$uri", "service": "web", "match": "prefix", "stripPrefix": False}]},
        {
            "routes": [
                {
                    "path": "/.well-known/acme-challenge",
                    "service": "web",
                    "match": "prefix",
                    "stripPrefix": False,
                }
            ]
        },
        {"command": "touch /evil"},
    ],
)
def test_root_protocol_rejects_injection(patch):
    module = helper()
    module.CONFIG = {
        "base_domain": "example.com",
        "email": "ops@example.com",
        "port_start": 20000,
        "port_end": 29999,
    }
    value = request()
    value.update(patch)
    with pytest.raises(ValueError):
        module.validate(value)


def setup_state(tmp_path):
    module = helper()
    module.CONFIG = {
        "base_domain": "example.com",
        "email": "ops@example.com",
        "port_start": 20000,
        "port_end": 29999,
    }
    module.STATE = tmp_path / "state"
    module.NGINX_DIR = tmp_path / "nginx"
    module.STATE.mkdir()
    module.NGINX_DIR.mkdir()
    module.run = lambda args, **kwargs: ""
    # These journal/config tests stub host TLS; probe behavior has separate tests below.
    module.probe_routes = lambda req: None
    old = request(release="old")
    module.publish(old)
    return module


def test_failed_nginx_reload_restores_previous_route(tmp_path):
    module = setup_state(tmp_path)
    old = module.config_path("demo").read_text()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["-s", "reload"] and len([c for c in calls if c[-2:] == ["-s", "reload"]]) == 1:
            raise RuntimeError("reload failed")
        return ""

    module.run = run
    with pytest.raises(RuntimeError):
        module.publish(request())
    assert module.config_path("demo").read_text() == old
    module.rollback(request("rollback"))
    assert module.read_state("demo")["current"]["release"] == "old"


def test_probe_failure_rolls_back_durable_previous_route(tmp_path):
    module = setup_state(tmp_path)
    old = module.config_path("demo").read_text()
    module.publish(request())

    def probe(req):
        if req["release"] == "new":
            raise RuntimeError("wrong upstream")

    module.probe_routes = probe
    with pytest.raises(RuntimeError, match="wrong upstream"):
        module.verify(request("verify"))
    assert module.config_path("demo").read_text() == old
    assert module.read_state("demo")["current"]["release"] == "old"


def test_rollback_cannot_overwrite_newer_release(tmp_path):
    module = setup_state(tmp_path)
    module.publish(request())
    with pytest.raises(ValueError):
        module.rollback(request("rollback", "stale"))
    assert module.read_state("demo")["current"]["release"] == "new"


def test_readiness_protocol_has_no_path_or_command_parameters():
    module = helper()
    assert module.validate({"op": "check"}) == {"op": "check"}
    with pytest.raises(ValueError):
        module.validate({"op": "check", "path": "/tmp/evil"})


def test_fixed_reload_validates_nginx_before_reload():
    module = helper()
    calls = []
    module.run = lambda args, **kwargs: calls.append(args)
    module.reload_gateway()
    assert all(args[0] != module.CERTBOT for args in calls)
    assert calls[-2:] == [[module.NGINX, "-t"], [module.NGINX, "-s", "reload"]]


def test_numeric_subdomain_matches_manifest_contract():
    module = helper()
    module.CONFIG = {
        "base_domain": "example.com",
        "email": "ops@example.com",
        "port_start": 20000,
        "port_end": 29999,
    }
    req = request()
    for subdomain in ["1", "123abcd", "1-a", "abc1234"]:
        req["subdomain"] = subdomain
        module.validate(req)


def test_prefix_probe_avoids_exact_route_and_more_specific_routes():
    module = helper()
    req = request()
    req["routes"].append({"path": "/abc", "match": "exact", "service": "other", "stripPrefix": False})
    req["routes"].append(
        {"path": "/abc/__awesome_probe", "match": "prefix", "service": "other", "stripPrefix": False}
    )
    paths = module.probe_paths(req)
    assert paths[0].startswith("/abc/")
    assert not paths[0].startswith("/abc/__awesome_probe")
    assert paths[1] == "/abc"


def test_rollback_before_helper_publish_preserves_previous_release(tmp_path):
    module = setup_state(tmp_path)
    previous = module.config_path("demo").read_text()
    assert module.rollback(request("rollback"))["rolled_back"] is True
    assert module.config_path("demo").read_text() == previous
    assert module.read_state("demo")["current"]["release"] == "old"


def test_acme_webroot_is_outside_private_gateway_state():
    module = helper()
    assert module.WEBROOT == Path("/var/www/awesome-acme")
    assert not module.WEBROOT.is_relative_to(module.STATE)


def test_gateway_rejects_platform_domain_mismatch_before_mutation():
    module = helper()
    module.CONFIG = {"base_domain": "wrong.example", "port_start": 20000, "port_end": 29999}
    with pytest.raises(ValueError, match="domain"):
        module.validate(request())


def test_ensure_https_reloads_renewed_certificate_while_holding_lock(tmp_path, monkeypatch):
    module = setup_state(tmp_path)
    calls = []
    module.run = lambda args, **kwargs: calls.append(args)
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: True if str(path).endswith("/fullchain.pem") else original_exists(path)
    )
    module.ensure_https(request("ensure_https"))
    assert "--no-directory-hooks" in calls[0]
    assert calls[-2:] == [[module.NGINX, "-t"], [module.NGINX, "-s", "reload"]]


def test_literal_dots_and_non_acme_well_known_routes_are_supported():
    module = helper()
    module.CONFIG = {"base_domain": "example.com", "port_start": 20000, "port_end": 29999}
    req = request()
    for path in ["/v1.0/app.js", "/.well-known/security.txt"]:
        req["routes"][0]["path"] = path
        assert path in module.render(req, tls=True)
    req["routes"][0]["path"] = "/a/../admin"
    with pytest.raises(ValueError):
        module.validate(req)


def test_acme_directory_remains_traversable_under_worker_umask(tmp_path, monkeypatch):
    import os
    import stat

    module = helper()
    module.STATE = tmp_path / "private-state"
    module.WEBROOT = tmp_path / "www" / "awesome-acme"
    module.WEBROOT.parent.mkdir(mode=0o755)
    module.WEBROOT.parent.chmod(0o755)
    monkeypatch.setattr(module, "trusted", lambda *args, **kwargs: None)
    previous = os.umask(0o077)
    try:
        module.prepare_directories()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(module.WEBROOT.stat().st_mode) == 0o755
    assert stat.S_IMODE(module.STATE.stat().st_mode) == 0o700


def probing_gateway(tmp_path, outcomes):
    """Replace only host TLS and clock; exercise actual log correlation/retry code."""
    module = helper()
    module.CONFIG = {"base_domain": "example.com"}
    module.PROBE_LOG = tmp_path / "probes.log"
    clock = SimpleNamespace(now=0.0)
    delayed_rows = []

    def write_row(row):
        with module.PROBE_LOG.open("a") as log:
            log.write(json.dumps(row) + "\n")

    def advance(seconds):
        clock.now += seconds
        for delivery, row in list(delayed_rows):
            if delivery <= clock.now:
                write_row(row)
                delayed_rows.remove((delivery, row))

    module.time = SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=advance,
    )
    attempts = []

    class Connection:
        def __init__(self, host, timeout, context):
            self.host, self.timeout = host, timeout
            self.closed = False
            attempts.append(self)

        def request(self, method, path, headers):
            assert method == "GET"
            assert headers["Connection"] == "close"
            self.path = path.split("?", 1)[0]
            self.token = headers["X-Awesome-Probe"]
            assert path.endswith("?__awesome_probe=" + self.token)

        def getresponse(self):
            advance(0.01)
            outcome = outcomes[min(len(attempts) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            row = {
                "probe": self.token,
                "host": self.host,
                "uri": self.path,
                "upstream": "127.0.0.1:20001",
                "status": 200,
            }
            if callable(outcome):
                row.update(outcome(self, attempts))
            else:
                row.update(outcome)
            delay = row.pop("log_delay", 0)
            if delay:
                delayed_rows.append((clock.now + delay, row))
            else:
                write_row(row)
            return SimpleNamespace(status=200)

        def close(self):
            self.closed = True

    module.LocalHTTPS = Connection
    return module, clock, attempts


def test_tls_probe_reconnects_after_old_worker_upstream(tmp_path):
    module, clock, attempts = probing_gateway(tmp_path, [{"upstream": "127.0.0.1:20000"}, {}])
    module.probe_routes(request())
    assert len(attempts) == 2
    assert len({attempt.token for attempt in attempts}) == 2
    assert all(attempt.closed for attempt in attempts)
    assert clock.now < 30


def test_tls_probe_accepts_delayed_current_invocation_log_after_reconnecting(tmp_path):
    module, clock, attempts = probing_gateway(tmp_path, [{"log_delay": 0.5}])
    module.probe_routes(request())
    assert len(attempts) >= 2
    assert 0.5 <= clock.now < 1
    assert len({attempt.token for attempt in attempts}) == len(attempts)
    assert all(attempt.closed for attempt in attempts)


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError("reloading"), TimeoutError("TLS handshake"), ssl.SSLError("reload handshake")],
)
def test_tls_probe_retries_transient_connection_failure(tmp_path, error):
    module, clock, attempts = probing_gateway(tmp_path, [error, {}])
    module.probe_routes(request())
    assert len(attempts) == 2
    assert attempts[0].token != attempts[1].token
    assert all(attempt.closed for attempt in attempts)
    assert clock.now < 30


def test_tls_probe_stops_retrying_old_workers_at_total_deadline(tmp_path):
    module, clock, attempts = probing_gateway(tmp_path, [{"upstream": "127.0.0.1:20000"}])
    with pytest.raises(RuntimeError, match="TLS probe"):
        module.probe_routes(request())
    assert len(attempts) > 1
    assert 29 <= clock.now <= 30.1
    assert all(0 < attempt.timeout <= 10 for attempt in attempts)
    assert all(attempt.closed for attempt in attempts)


@pytest.mark.parametrize(
    "bad_row",
    [
        {"uri": "/another-route"},
        {"status": 503},
        {"status": 201},
        {"host": "other.example.com"},
    ],
)
def test_tls_probe_does_not_accept_wrong_request_log_fields(tmp_path, bad_row):
    module, clock, attempts = probing_gateway(tmp_path, [bad_row])
    with pytest.raises(RuntimeError, match="TLS probe"):
        module.probe_routes(request())
    assert len(attempts) > 1
    assert clock.now <= 30.1


def test_tls_probe_does_not_accept_prior_attempt_token(tmp_path):
    def stale_token(connection, attempts):
        return {"probe": attempts[0].token, "uri": attempts[0].path}

    module, clock, attempts = probing_gateway(tmp_path, [{"upstream": "127.0.0.1:20000"}, stale_token])
    with pytest.raises(RuntimeError, match="TLS probe"):
        module.probe_routes(request())
    assert len(attempts) > 1
    assert clock.now <= 30.1


def test_tls_probe_total_deadline_is_shared_across_routes(tmp_path):
    module, clock, attempts = probing_gateway(tmp_path, [{"upstream": "127.0.0.1:20000"}])
    req = request()
    req["routes"].append({"path": "/other", "match": "exact", "service": "web", "stripPrefix": False})
    with pytest.raises(RuntimeError, match="TLS probe"):
        module.probe_routes(req)
    assert len({attempt.path for attempt in attempts}) == 2
    assert 29 <= clock.now <= 30.1


def test_tls_probe_confirms_each_routes_own_upstream(tmp_path):
    def outcome(connection, attempts):
        if connection.path == "/api":
            return {"upstream": "127.0.0.1:20002"}
        return {"upstream": "127.0.0.1:20000" if len(attempts) == 1 else "127.0.0.1:20001"}

    module, clock, attempts = probing_gateway(tmp_path, [outcome])
    req = request()
    req["ports"]["api"] = 20002
    req["routes"].append({"path": "/api", "match": "exact", "service": "api", "stripPrefix": False})
    module.probe_routes(req)
    assert len(attempts) == 3
    assert sum(attempt.path == "/api" for attempt in attempts) == 1
    assert all(attempt.closed for attempt in attempts)


def test_rollback_confirms_previous_upstream_before_clearing_journal(tmp_path):
    module = setup_state(tmp_path)
    previous_config = module.config_path("demo").read_text()
    module.publish(request())
    seen = []

    def probe(previous):
        assert module.config_path("demo").read_text() == previous_config
        state = module.read_state("demo")
        assert state["current"]["release"] == "new"
        assert state["pending"]["release"] == "new"
        seen.append(previous["release"])

    module.probe_routes = probe
    module.rollback(request("rollback"))
    assert seen == ["old"]
    assert module.read_state("demo")["pending"] is None


def test_failed_rollback_probe_retains_journal_for_retry(tmp_path):
    module = setup_state(tmp_path)
    previous_config = module.config_path("demo").read_text()
    module.publish(request())
    module.probe_routes = lambda req: (_ for _ in ()).throw(RuntimeError("old upstream not ready"))
    with pytest.raises(RuntimeError, match="old upstream not ready"):
        module.rollback(request("rollback"))
    state = module.read_state("demo")
    assert state["current"]["release"] == "new"
    assert state["pending"]["release"] == "new"
    assert module.config_path("demo").read_text() == previous_config
    module.probe_routes = lambda req: None
    assert module.rollback(request("rollback"))["rolled_back"] is True
    assert module.read_state("demo")["current"]["release"] == "old"


def test_idempotent_rollback_rechecks_existing_previous_upstream(tmp_path):
    module = setup_state(tmp_path)
    module.probe_routes = lambda req: (_ for _ in ()).throw(RuntimeError("previous upstream absent"))
    with pytest.raises(RuntimeError, match="previous upstream absent"):
        module.rollback(request("rollback"))
    assert module.read_state("demo")["current"]["release"] == "old"
