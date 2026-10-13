import copy
import importlib
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest

from awesome_deploy.errors import DeployError


SPEC = {"proto": "tcp4", "parentIP": "127.0.0.1", "parentPort": 20000,
        "childIP": "172.19.0.2", "childPort": 8080}
INFO = {"apiVersion": "1.1.3", "version": "3.2.0", "stateDir": "/run/user/1000/dockerd-rootless",
        "childPID": 123, "portDriver": {"driver": "builtin", "protos": ["tcp", "tcp4", "udp"]}}


def client(settings):
    try:
        module = importlib.import_module("awesome_deploy.port_forward")
    except ModuleNotFoundError:
        pytest.fail("The restricted RootlessKit client has not been implemented")
    return module.RootlessPortForwarder(settings)


@pytest.fixture
def api():
    directory = tempfile.TemporaryDirectory(prefix="aw-port-", dir="/tmp")
    location = Path(directory.name) / "dockerd-rootless" / "api.sock"
    location.parent.mkdir()
    state = SimpleNamespace(path=location, info=copy.deepcopy(INFO), ports=[], calls=[],
                            hook=None, next_id=1, delay=0, trickle=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, method):
            payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(payload) if payload else None
            state.calls.append((method, self.path, body))
            if state.delay:
                time.sleep(state.delay)
            custom = state.hook(method, self.path, body) if state.hook else None
            if custom is not None:
                status, result = custom
            elif method == "GET" and self.path == "/v1/info":
                status, result = 200, state.info
            elif method == "GET" and self.path == "/v1/ports":
                status, result = 200, state.ports
            elif method == "POST" and self.path == "/v1/ports":
                result = {"id": state.next_id, "spec": body}
                state.next_id += 1
                state.ports.append(copy.deepcopy(result))
                status = 201
            elif method == "DELETE" and self.path.startswith("/v1/ports/"):
                identifier = int(self.path.rsplit("/", 1)[1])
                state.ports[:] = [p for p in state.ports if p["id"] != identifier]
                status, result = 200, None
            else:
                status, result = 404, {"error": "unexpected request"}
            encoded = result if isinstance(result, bytes) else json.dumps(result).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if state.trickle:
                    for byte in encoded:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.02)
                else:
                    self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def do_DELETE(self):
            self.respond("DELETE")

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(location), Handler)
    os.chmod(location, 0o600)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    state.settings = SimpleNamespace(rootlesskit_socket=location, docker_host="unix:///not-used/docker.sock")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        directory.cleanup()


def test_check_and_default_socket_derived_from_docker_parent(api):
    api.settings.rootlesskit_socket = None
    api.settings.docker_host = "unix://" + str(api.path.parent.parent / "docker.sock")
    assert client(api.settings).check() == INFO


def test_ensure_creates_then_reuses_only_the_complete_spec(api):
    forwarder = client(api.settings)
    first = forwarder.ensure(SPEC)
    assert first == {"id": 1, "spec": SPEC}
    assert forwarder.ensure(SPEC) == first
    assert len([call for call in api.calls if call[0] == "POST"]) == 1
    assert SPEC["parentIP"] == "127.0.0.1"


def test_recovery_ensure_never_creates_an_absent_unconfirmed_mapping(api):
    with pytest.raises(DeployError) as caught:
        client(api.settings).ensure(SPEC, create=False)
    assert caught.value.code == "PORT_FORWARD_UNCONFIRMED"
    assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)


def test_recovery_ensure_can_adopt_the_exact_present_mapping(api):
    api.ports = [{"id": 17, "spec": dict(SPEC)}]
    assert client(api.settings).ensure(SPEC, create=False) == api.ports[0]
    assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)


def test_uncertain_cleanup_requires_a_present_mapping(api):
    with pytest.raises(DeployError) as caught:
        client(api.settings).remove(SPEC, require_present=True)
    assert caught.value.code == "PORT_FORWARD_UNCONFIRMED"
    assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)


def test_uncertain_cleanup_can_remove_the_exact_present_mapping(api):
    api.ports = [{"id": 23, "spec": dict(SPEC)}]
    assert client(api.settings).remove(SPEC, require_present=True) is None
    assert not api.ports
    assert ("DELETE", "/v1/ports/23", None) in api.calls


@pytest.mark.parametrize("flag", [None, 0, 1, "false"])
def test_mutation_policy_flags_require_booleans(api, flag):
    forwarder = client(api.settings)
    with pytest.raises(DeployError):
        forwarder.ensure(SPEC, create=flag)
    with pytest.raises(DeployError):
        forwarder.remove(SPEC, require_present=flag)
    assert not api.calls


def test_foreign_mapping_with_optional_ips_omitted_is_accepted(api):
    api.ports = [{"id": 88, "spec": {"proto": "tcp", "parentPort": 19999, "childPort": 9999}}]
    assert client(api.settings).ensure(SPEC)["spec"] == SPEC


def test_official_empty_list_and_delete_body_are_supported(api):
    def official_empty(method, path, body):
        if method == "GET" and path == "/v1/ports" and not api.ports:
            return 200, None  # builtin's nil slice is JSON null in the official router.
        if method == "DELETE":
            identifier = int(path.rsplit("/", 1)[1])
            api.ports[:] = [p for p in api.ports if p["id"] != identifier]
            return 200, b""  # The official DELETE handler writes only status 200.

    api.hook = official_empty
    forwarder = client(api.settings)
    assert forwarder.ensure(SPEC)["spec"] == SPEC
    forwarder.remove(SPEC)
    assert api.ports == []


def test_socket_replacement_during_operation_prevents_mutation(api):
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def replace_socket(method, path, body):
        if path == "/v1/info":
            api.path.rename(api.path.with_suffix(".old"))
            replacement.bind(str(api.path))
            api.path.chmod(0o600)
            replacement.listen()

    api.hook = replace_socket
    try:
        with pytest.raises(DeployError):
            client(api.settings).ensure(SPEC)
        assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)
    finally:
        replacement.close()


@pytest.mark.parametrize("change", [{"childIP": "172.19.0.3"}, {"childPort": 9000},
                                   {"parentIP": "0.0.0.0"}, {"proto": "tcp"}])
def test_same_parent_port_conflict_is_never_overwritten(api, change):
    api.ports = [{"id": 7, "spec": SPEC | change}]
    with pytest.raises(DeployError, match="conflict"):
        client(api.settings).ensure(SPEC)
    assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)


def test_remove_is_idempotent_and_never_uses_a_stale_numeric_id(api):
    forwarder = client(api.settings)
    forwarder.ensure(SPEC)
    api.ports = [{"id": 1, "spec": SPEC | {"parentPort": 21000, "childIP": "172.20.0.3"}}]
    forwarder.remove(SPEC)
    assert api.ports[0]["id"] == 1
    assert not any(call[0] == "DELETE" for call in api.calls)
    api.ports.append({"id": 9, "spec": dict(SPEC)})
    forwarder.remove(SPEC)
    assert [p["id"] for p in api.ports] == [1]
    assert ("DELETE", "/v1/ports/9", None) in api.calls
    forwarder.remove(SPEC)


def test_remove_refuses_id_reassignment_before_delete(api):
    api.ports = [{"id": 7, "spec": dict(SPEC)}]
    reads = 0

    def reassign(method, path, body):
        nonlocal reads
        if method == "GET" and path == "/v1/ports":
            reads += 1
            if reads == 2:
                api.ports = [{"id": 7, "spec": SPEC | {"childIP": "172.20.0.3"}}]

    api.hook = reassign
    with pytest.raises(DeployError):
        client(api.settings).remove(SPEC)
    assert not any(call[0] == "DELETE" for call in api.calls)


def test_delete_must_be_confirmed_before_success(api):
    api.ports = [{"id": 7, "spec": dict(SPEC)}]
    api.hook = lambda method, path, body: (200, None) if method == "DELETE" else None
    with pytest.raises(DeployError):
        client(api.settings).remove(SPEC)


@pytest.mark.parametrize("change", [
    {"proto": "tcp"}, {"proto": "udp"}, {"parentIP": "0.0.0.0"},
    {"parentIP": "::1"}, {"parentPort": 0}, {"parentPort": True}, {"parentPort": 65536},
    {"childPort": 0}, {"childPort": "8080"}, {"childIP": "127.0.0.1"},
    {"childIP": "0.0.0.0"}, {"childIP": "169.254.1.2"}, {"childIP": "8.8.8.8"},
    {"childIP": "::1"}, {"childIP": "172.19.0.2/16"}, {"unknown": "value"},
])
def test_invalid_forwarding_targets_are_rejected_before_request(api, change):
    with pytest.raises(DeployError):
        client(api.settings).ensure(SPEC | change)
    assert not api.calls


@pytest.mark.parametrize("ports", [{}, [None], [{"id": True, "spec": SPEC}],
                                   [{"id": 0, "spec": SPEC}], [{"id": 1}],
                                   [{"id": 1, "spec": SPEC | {"childPort": False}}],
                                   [{"id": 1, "spec": SPEC | {"parentIP": "bad-ip"}}],
                                   [{"id": 1, "spec": SPEC}, {"id": 1, "spec": SPEC}]])
def test_malformed_list_is_rejected_without_mutation(api, ports):
    api.hook = lambda method, path, body: (200, ports) if path == "/v1/ports" else None
    with pytest.raises(DeployError):
        client(api.settings).ensure(SPEC)
    assert not any(call[0] in {"POST", "DELETE"} for call in api.calls)


@pytest.mark.parametrize("info", [None, {}, INFO | {"childPID": True},
                                  INFO | {"apiVersion": "2.0.0"},
                                  INFO | {"portDriver": {"driver": "slirp4netns", "protos": ["tcp4"]}},
                                  INFO | {"portDriver": {"driver": "builtin", "protos": ["tcp"]}}])
def test_unsupported_or_malformed_info_fails_closed(api, info):
    api.info = info
    with pytest.raises(DeployError):
        client(api.settings).check()


def test_post_must_return_the_exact_requested_mapping(api):
    api.hook = lambda method, path, body: (
        (201, {"id": 1, "spec": SPEC | {"childIP": "172.19.0.3"}}) if method == "POST" else None
    )
    with pytest.raises(DeployError):
        client(api.settings).ensure(SPEC)


@pytest.mark.parametrize("response", [b"not json secret-do-not-log", b"x" * 70000,
                                      b'{"id":1,"id":2}', b'{"secret":NaN}'],
                         ids=["not-json", "oversized", "duplicate-key", "non-finite"])
def test_invalid_or_oversized_responses_do_not_leak_data(api, response):
    api.hook = lambda method, path, body: (200, response)
    with pytest.raises(DeployError) as caught:
        client(api.settings).check()
    assert "secret" not in str(caught.value)
    assert "not json" not in str(caught.value)


def test_http_error_body_is_never_exposed(api):
    api.hook = lambda method, path, body: (503, {"password": "secret-do-not-log"})
    with pytest.raises(DeployError) as caught:
        client(api.settings).check()
    assert "secret-do-not-log" not in str(caught.value)
    assert "password" not in str(caught.value)


def test_operation_deadline_is_shared_across_requests(api):
    api.delay = .08
    started = time.monotonic()
    with pytest.raises(DeployError):
        client(api.settings).ensure(SPEC, timeout=.13)
    assert time.monotonic() - started < .4


def test_trickled_body_cannot_reset_total_deadline(api):
    api.trickle = True
    started = time.monotonic()
    with pytest.raises(DeployError):
        client(api.settings).check(timeout=.12)
    assert time.monotonic() - started < .4


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
def test_invalid_timeout_is_rejected_before_request(api, timeout):
    with pytest.raises(DeployError):
        client(api.settings).check(timeout=timeout)
    assert not api.calls


def test_world_writable_socket_is_rejected(api):
    api.path.chmod(0o666)
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls


def test_same_uid_group_writable_socket_is_supported(api):
    api.path.chmod(0o775)
    assert client(api.settings).check() == INFO


def test_socket_symlink_is_rejected(api):
    link = api.path.parent / "link.sock"
    link.symlink_to(api.path)
    api.settings.rootlesskit_socket = link
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls


def test_non_socket_path_is_rejected(api):
    regular = api.path.parent / "regular"
    regular.write_text("not a socket")
    api.settings.rootlesskit_socket = regular
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls


def test_other_uid_socket_is_rejected(api, monkeypatch):
    original = Path.lstat

    def foreign(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        if path == api.path:
            values = list(actual)
            values[4] = actual.st_uid + 1
            return os.stat_result(values)
        return actual

    monkeypatch.setattr(Path, "lstat", foreign)
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls


def test_root_caller_is_rejected(api, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls


def test_non_unix_docker_endpoint_cannot_supply_default_socket(api):
    api.settings.rootlesskit_socket = None
    api.settings.docker_host = "tcp://127.0.0.1:2375"
    with pytest.raises(DeployError):
        client(api.settings).check()
    assert not api.calls
