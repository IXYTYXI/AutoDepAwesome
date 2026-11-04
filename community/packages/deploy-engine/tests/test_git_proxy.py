"""Git proxy configuration and transport isolation, without external network access."""

import json
import socket
import subprocess
import threading

import pytest

from awesome_deploy.cli import load_settings
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.source import GitSource, _bounded_run
from test_source import FakeRunner, SHA, job


PROXY_ENV = "AWESOME_DEPLOY_GIT_PROXY_URL"
PROXY_ARG = f"--config-env=http.proxy={PROXY_ENV}"
PROXY = "http://127.0.0.1:7890"


def configured(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"git_proxy_url": value}))
    return load_settings(path)


def test_proxy_defaults_to_unconfigured(tmp_path):
    assert Settings().git_proxy_url is None
    assert load_settings(None).git_proxy_url is None
    assert configured(tmp_path, None).git_proxy_url is None


@pytest.mark.parametrize("url", [
    PROXY, PROXY + "/", "https://proxy.example:8443", "socks5h://localhost:1080",
    "http://[::1]:7890", "https://[2001:db8::1]:443/", "http://proxy-1.example:1",
    "http://proxy.example:65535", "http://PROXY.example:7890",
])
def test_config_accepts_explicit_unauthenticated_proxy_endpoints(tmp_path, url):
    assert configured(tmp_path, url).git_proxy_url == url


@pytest.mark.parametrize("url", [
    "", True, 123, [], {}, "http://", "http://:7890", "http://localhost", "localhost:7890",
    "ftp://localhost:7890", "socks5://localhost:7890", "file:///tmp/proxy",
    "http://localhost:0", "http://localhost:65536", "http://localhost:bad", "http://localhost:",
    "http://user:secret@localhost:7890", "http://secret@localhost:7890", "http://@localhost:7890",
    "http://localhost:7890?", "http://localhost:7890?secret=value", "http://localhost:7890#",
    "http://localhost:7890#secret", "http://localhost:7890/proxy", "http://localhost:7890//",
    "http://localhost:7890/..", "http://localhost:7890/%2e", "http://localhost:7890\\",
    " http://localhost:7890", "http://localhost:7890\n", "http://local\thost:7890",
    "http://local\rhost:7890", "http://localhost:7890\x00", "http://localhost:7890\x7f",
    "http://local\u200bhost:7890", "http://local host:7890", "http://代理:7890",
    "http://-proxy:7890", "http://proxy-:7890", "http://proxy..example:7890",
    "http://proxy_name:7890", "http://%6cocalhost:7890", "http://999.1.1.1:7890",
    "http://127.1:7890", "http://0177.0.0.1:7890", "http://2130706433:7890",
    "http://::1:7890", "http://[::1:7890", "http://[localhost]:7890", "http://[::1%lo]:7890",
    "http://" + "a" * 64 + ":7890", "http://" + "long." * 60 + "example:7890",
])
def test_config_rejects_unsafe_proxy_values_without_disclosing_them(tmp_path, url):
    with pytest.raises(DeployError) as raised:
        configured(tmp_path, url)
    assert raised.value.code == "INVALID_CONFIG"
    assert "secret" not in str(raised.value.as_dict())


def test_direct_settings_cannot_bypass_proxy_validation(tmp_path):
    runner = FakeRunner()
    with pytest.raises(DeployError) as raised:
        GitSource(Settings(git_proxy_url="http://secret@localhost:7890"), runner=runner)._git(
            ["ls-remote", "https://github.com/example/demo.git"], tmp_path / "git")
    assert raised.value.code == "INVALID_CONFIG"
    assert "secret" not in str(raised.value.as_dict())
    assert not runner.calls


@pytest.mark.parametrize("proxy", [None, PROXY])
def test_git_does_not_inherit_proxy_tls_or_trace_environment(tmp_path, monkeypatch, proxy):
    inherited = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
        "all_proxy", "no_proxy", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_SSL_NO_VERIFY",
        "GIT_PROXY_SSL_VERIFY", "GIT_TRACE", "GIT_TRACE_CURL", PROXY_ENV,
    )
    for name in inherited:
        monkeypatch.setenv(name, "untrusted-value")
    runner = FakeRunner()
    GitSource(Settings(git_proxy_url=proxy), runner=runner)._git(
        ["ls-remote", "https://github.com/example/demo.git", "main"], tmp_path / "git")
    args, options = runner.calls[0]
    assert not set(inherited).difference({PROXY_ENV}).intersection(options["env"])
    assert "untrusted-value" not in str(options)
    assert (PROXY_ARG in args) is (proxy is not None)
    assert options["env"].get(PROXY_ENV) == proxy
    assert "http.sslVerify=true" in args
    assert "http.followRedirects=false" in args
    assert "credential.helper=" in args


@pytest.mark.parametrize("args", [
    ["fetch", "--no-tags", "https://github.com/example/demo.git", "main"],
    ["--git-dir", "/tmp/repository", "fetch", "--depth=1", "https://github.com/example/demo.git", SHA],
    ["--git-dir=/tmp/repository", "fetch", "https://github.com/example/demo.git", "main"],
    ["ls-remote", "https://github.com/example/demo.git", "main"],
])
def test_only_network_commands_receive_operator_proxy(tmp_path, args):
    runner = FakeRunner()
    GitSource(Settings(git_proxy_url=PROXY), runner=runner)._git(args, tmp_path / "git")
    command, options = runner.calls[0]
    assert PROXY_ARG in command
    assert options["env"][PROXY_ENV] == PROXY
    assert PROXY not in str(command)
    assert command[-len(args):] == args


@pytest.mark.parametrize("args", [
    ["init", "--bare", "/tmp/fetch"],
    ["--git-dir", "/tmp/fetch", "cat-file", "-e", SHA + "^{commit}"],
    ["--git-dir", "/tmp/ls-remote", "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
    ["--git-dir=/tmp/fetch", "ls-tree", "-r", "-z", SHA],
    ["archive", "--format=tar", "fetch"],
])
def test_local_commands_do_not_receive_proxy_even_with_matching_arguments(tmp_path, args):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    GitSource(Settings(git_proxy_url=PROXY), runner=run)._git(args, tmp_path / "git")
    assert PROXY_ARG not in calls[0][0]
    assert PROXY_ENV not in calls[0][1]["env"]


def test_proxy_does_not_change_source_snapshot_requested_ref_or_askpass(tmp_path):
    runner = FakeRunner()
    checkpoints = []
    inputs = job()
    inputs["input"]["ref"] = "codex/model-studio"
    settings = Settings(state_dir=tmp_path, git_proxy_url=PROXY, git_askpass="/opt/operator-askpass")
    snapshot = GitSource(settings, runner=runner).prepare(inputs, checkpoints.append)
    assert snapshot["commit"] == SHA
    assert checkpoints == [{"commit": SHA}]
    assert snapshot["repository"] == "https://github.com/example/demo.git"
    assert PROXY not in str(snapshot)
    assert "git_proxy_url" not in snapshot
    network_calls = [(args, options) for args, options in runner.calls if PROXY_ARG in args]
    assert len(network_calls) == 1
    args, options = network_calls[0]
    assert args[-1] == "codex/model-studio"
    assert options["env"]["GIT_ASKPASS"] == "/opt/operator-askpass"
    assert not any(PROXY in path.read_text(errors="replace") for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.parametrize("failure", ["exit", "oserror", "timeout", "output-limit"])
def test_proxy_failure_never_retries_direct_or_exposes_raw_errors(tmp_path, failure):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if failure == "oserror":
            raise OSError("secret provider diagnostic " + PROXY)
        if failure in {"timeout", "output-limit"}:
            code = "GIT_TIMEOUT" if failure == "timeout" else "GIT_OUTPUT_LIMIT"
            raise DeployError(code, "Bounded Git runner stopped the request.")
        return subprocess.CompletedProcess(args, 128, b"secret stdout", b"secret stderr " + PROXY.encode())

    with pytest.raises(DeployError) as raised:
        GitSource(Settings(git_proxy_url=PROXY), runner=run)._git(
            ["ls-remote", "https://github.com/example/demo.git"], tmp_path / "git")
    expected = {"timeout": "GIT_TIMEOUT", "output-limit": "GIT_OUTPUT_LIMIT"}.get(failure, "GIT_FAILED")
    assert raised.value.code == expected
    assert "secret" not in str(raised.value.as_dict())
    assert PROXY not in str(raised.value.as_dict())
    assert len(calls) == 1
    assert calls[0][1]["env"][PROXY_ENV] == PROXY


def test_real_git_connects_only_to_configured_local_proxy_and_fails_closed(tmp_path):
    # Construct first so an absent feature fails before any socket is opened.
    settings = Settings(git_timeout=3, git_proxy_url=PROXY)
    requests = []
    failures = []
    stopped = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(0.1)
        proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"

        def serve():
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    with connection:
                        connection.settimeout(1)
                        raw = b""
                        while b"\r\n\r\n" not in raw and len(raw) < 16384:
                            part = connection.recv(1024)
                            if not part:
                                break
                            raw += part
                        requests.append(raw.split(b"\r\n", 1)[0])
                        connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                                           b"Connection: close\r\n\r\n")
                except OSError as error:
                    failures.append(type(error).__name__)

        def guarded_runner(args, **kwargs):
            # Never allow this test to access GitHub if proxy injection regresses.
            assert PROXY_ARG in args
            assert kwargs["env"].get(PROXY_ENV) == proxy
            return _bounded_run(args, **kwargs)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            settings = Settings(git_timeout=settings.git_timeout, git_proxy_url=proxy)
            with pytest.raises(DeployError) as raised:
                GitSource(settings, runner=guarded_runner)._git(
                    ["ls-remote", "https://github.com/example/demo.git"], tmp_path / "git")
            assert raised.value.code == "GIT_FAILED"
            assert proxy not in str(raised.value.as_dict())
        finally:
            stopped.set()
            thread.join(timeout=2)
        assert not thread.is_alive()
    assert not failures
    assert requests == [b"CONNECT github.com:443 HTTP/1.1"]
