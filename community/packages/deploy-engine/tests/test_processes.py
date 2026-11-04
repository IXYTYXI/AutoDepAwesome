import sys
import time

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.processes import run_command


def test_exec_argv_literal_and_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET", "must-not-pass")
    result = run_command(
        [
            sys.executable,
            "-c",
            'import os,sys;print(sys.argv[1]);print(os.getenv("HOST_SECRET", "absent"))',
            "$(touch injected)",
        ],
        cwd=tmp_path,
    )
    assert result.stdout == "$(touch injected)\nabsent\n"
    assert not (tmp_path / "injected").exists()


def test_caps_and_redacts_both_streams():
    result = run_command(
        [sys.executable, "-c", 'import sys;print("sensitive"*20000);print("sensitive",file=sys.stderr)'],
        secrets=("sensitive",),
        output_limit=2048,
    )
    assert "sensitive" not in result.stdout + result.stderr
    assert len(result.stdout) <= 2200
    assert "[REDACTED]" in result.stderr


def test_timeout_kills_child_process_group(tmp_path):
    marker = tmp_path / "child-ran"
    script = (
        'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c", "import time,pathlib;time.sleep(1);pathlib.Path(%r).touch()"]);time.sleep(20)'
        % str(marker)
    )
    with pytest.raises(DeployError, match="timed out"):
        run_command([sys.executable, "-c", script], timeout=0.1)
    time.sleep(1.1)
    assert not marker.exists()


def test_nonzero_diagnostic_redaction():
    with pytest.raises(DeployError) as exc:
        run_command(
            [sys.executable, "-c", 'import sys;sys.stderr.write("secret-password");sys.exit(2)'],
            secrets=("secret-password",),
        )
    assert "secret-password" not in exc.value.message
    assert "[REDACTED]" in exc.value.message


def test_truncation_does_not_expose_partial_secret():
    result = run_command(
        [sys.executable, "-c", 'print("123456supersecretvalue")'],
        secrets=("supersecretvalue",),
        output_limit=12,
    )
    assert "super" not in result.stdout


def test_http_probe_deadline_applies_to_trickled_headers(tmp_path):
    import socket
    import threading
    from awesome_deploy.processes import http_status

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def slow_server():
        connection, _ = listener.accept()
        with connection:
            try:
                connection.recv(4096)
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    connection.send(bytes([byte]))
                    time.sleep(0.05)
            except OSError:
                pass

    thread = threading.Thread(target=slow_server, daemon=True)
    thread.start()
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            http_status("127.0.0.1", listener.getsockname()[1], "/", timeout=0.15)
        assert time.monotonic() - start < 0.8
    finally:
        listener.close()
        thread.join(1)


def test_total_process_timeout_still_applies_when_child_closes_output():
    start = time.monotonic()
    with pytest.raises(DeployError, match="timed out"):
        run_command(
            [sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(.5)"], timeout=0.05
        )
    assert time.monotonic() - start < 0.3
