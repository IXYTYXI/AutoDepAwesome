import importlib.machinery
import importlib.util
from pathlib import Path

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
    module.probe_routes = lambda req: (_ for _ in ()).throw(RuntimeError("wrong upstream"))
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
