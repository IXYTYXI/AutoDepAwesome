"""Bounded, shell-free subprocesses. Never inherit the API server's credentials."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from .errors import DeployError


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def redact(value, secrets=()):
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)


def run_command(
    argv,
    *,
    timeout=120,
    env=None,
    input_text=None,
    secrets=(),
    cwd=None,
    output_limit=65536,
    check=True,
    pass_fds=(),
):
    if not argv or any(not isinstance(arg, (str, Path)) or "\x00" in str(arg) for arg in argv):
        raise DeployError("COMMAND_INVALID", "Command arguments must be literal strings")
    child_env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}
    child_env.update(env or {})
    # Temporary file avoids deadlocking on stdin when the child writes output first.
    import tempfile

    with tempfile.TemporaryFile() as stdin:
        if input_text is not None:
            stdin.write(input_text.encode())
        stdin.seek(0)
        try:
            process = subprocess.Popen(
                [str(a) for a in argv],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=child_env,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise DeployError(
                "COMMAND_UNAVAILABLE", f"Cannot execute {Path(str(argv[0])).name}: {exc.strerror}"
            ) from exc
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        capture_limit = output_limit + max((len(str(secret).encode()) for secret in secrets), default=0)
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), 16384)
                    if not data:
                        selector.unregister(key.fileobj)
                    elif len(buffers[key.data]) < capture_limit:
                        buffers[key.data].extend(data[: capture_limit - len(buffers[key.data])])
            try:
                process.wait(timeout=5 if timed_out else max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdout.close()
            process.stderr.close()
        if timed_out:
            raise DeployError("COMMAND_TIMEOUT", f"{Path(str(argv[0])).name} timed out after {timeout:g}s")
        stdout = redact(buffers["stdout"].decode("utf-8", errors="replace"), secrets)[:output_limit]
        stderr = redact(buffers["stderr"].decode("utf-8", errors="replace"), secrets)[:output_limit]
        if check and process.returncode:
            raise DeployError(
                "COMMAND_FAILED", f"{Path(str(argv[0])).name} exited {process.returncode}: {stderr or stdout}"
            )
        return CommandResult(stdout, stderr, process.returncode)


class DeadlineSocket:
    """A socket facade whose file reads share one absolute deadline (including slow headers)."""

    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP probe total deadline exceeded")
        self.sock.settimeout(remaining)

    def sendall(self, data):
        self._remaining()
        return self.sock.sendall(data)

    def makefile(self, mode):
        import io

        owner = self

        class Reader(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                owner._remaining()
                return owner.sock.recv_into(buffer)

        return io.BufferedReader(Reader())

    def close(self):
        self.sock.close()


def http_status(host, port, path, *, timeout, headers=None):
    import http.client

    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.connect()
        connection.sock = DeadlineSocket(connection.sock, deadline)
        connection.request("GET", path, headers=headers or {})
        # Status/headers suffice; streaming or enormous bodies are intentionally never read.
        return connection.getresponse().status
    finally:
        connection.close()
