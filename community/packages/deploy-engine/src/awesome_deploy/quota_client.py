"""Bounded root-helper protocol with an inherited admission-session lifetime."""

from contextlib import contextmanager
import json
import os
import selectors
import subprocess
import time

from .errors import DeployError


LIMIT = 65536
RESPONSE_LIMIT = 4 * 1024 * 1024


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant")


class QuotaSession:
    def __init__(self, command, timeout):
        self.timeout = timeout
        try:
            self.process = subprocess.Popen(
                list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                bufsize=0,
            )
        except OSError as error:
            raise DeployError("QUOTA_UNAVAILABLE", "Storage helper is unavailable") from error
        self.fd = self.process.stdin.fileno()

    def call(self, op, **values):
        payload = json.dumps({"op": op, **values}, separators=(",", ":")).encode() + b"\n"
        if len(payload) > LIMIT:
            raise DeployError("QUOTA_PROTOCOL", "Storage helper request exceeds the protocol limit")
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.fd, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(payload):
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise DeployError("QUOTA_UNAVAILABLE", "Storage helper timed out")
                try:
                    offset += os.write(self.fd, payload[offset:offset + 4096])
                except OSError as error:
                    raise DeployError("QUOTA_UNAVAILABLE", "Storage helper closed the request") from error
            selector.unregister(self.fd)
            selector.register(self.process.stdout, selectors.EVENT_READ)
            output = bytearray()
            while b"\n" not in output:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise DeployError("QUOTA_UNAVAILABLE", "Storage helper timed out")
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise DeployError("QUOTA_PROTOCOL", "Storage helper returned an incomplete response")
                output.extend(chunk)
                if len(output) > RESPONSE_LIMIT:
                    raise DeployError("QUOTA_PROTOCOL", "Storage helper response exceeds the protocol limit")
        try:
            response = json.loads(output, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise DeployError("QUOTA_PROTOCOL", "Storage helper returned invalid JSON") from error
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise DeployError("QUOTA_PROTOCOL", "Storage helper returned an invalid envelope")
        if response["ok"] is False:
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise DeployError("QUOTA_PROTOCOL", "Storage helper returned an invalid error")
            raise DeployError(error["code"], str(error.get("message", "Storage helper rejected the operation")))
        if "result" not in response:
            raise DeployError("QUOTA_PROTOCOL", "Storage helper response lacks a result")
        return response["result"]

    def close(self):
        # Do not terminate the helper: an inherited writer FD may still belong to
        # an in-flight Docker command after worker interruption. EOF releases it.
        self.process.stdin.close()
        self.process.stdout.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


class QuotaClient:
    def __init__(self, command):
        self.command = tuple(command)

    def call(self, op, *, timeout=120, **values):
        session = QuotaSession(self.command, timeout)
        try:
            return session.call(op, **values)
        finally:
            session.close()

    @contextmanager
    def session(self, op, *, timeout=120, **values):
        session = QuotaSession(self.command, timeout)
        try:
            session.result = session.call(op, **values)
            yield session
            session.call("release")
        finally:
            session.close()
