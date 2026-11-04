"""Bounded access to RootlessKit's existing, unprivileged builtin port driver.

Callers persist ownership intent before ensure() and hold the worker lock through
cleanup. RootlessKit IDs are transient; they are never accepted from stored state.
"""

import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time

from .errors import DeployError


_LIMIT = 64 * 1024
_SPEC_KEYS = {"proto", "parentIP", "parentPort", "childIP", "childPort"}
_PROTOS = {"tcp", "tcp4", "tcp6", "udp", "udp4", "udp6", "sctp", "sctp4", "sctp6"}
_PRIVATE = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _error(code, message):
    return DeployError(code, message, "port_forward",
                       "Check the approved RootlessKit socket and the recorded port reservation.")


def _port(value):
    return type(value) is int and 1 <= value <= 65535


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite JSON number")


class _ResponseSocket:
    """HTTPResponse facade with one deadline and byte budget including headers."""

    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline
        self.remaining_bytes = _LIMIT

    def makefile(self, mode):
        owner = self

        class Reader(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                remaining = owner.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("response deadline")
                owner.connection.settimeout(remaining)
                count = owner.connection.recv_into(
                    memoryview(buffer)[:min(len(buffer), owner.remaining_bytes + 1)]
                )
                owner.remaining_bytes -= count
                if owner.remaining_bytes < 0:
                    raise ValueError("response limit")
                return count

        return io.BufferedReader(Reader())


class _Session:
    def __init__(self, path, timeout):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise _error("PORT_FORWARD_INVALID", "Port forwarding requires a positive finite timeout.")
        self.path = path
        self.deadline = time.monotonic() + min(timeout, 5)
        self.identity = self._socket_identity()

    def _socket_identity(self):
        try:
            uid = os.geteuid()
            info = self.path.lstat()
            if (uid == 0 or uid != os.getuid() or info.st_uid != uid
                    or not stat.S_ISSOCK(info.st_mode) or info.st_mode & stat.S_IWOTH):
                raise ValueError("unsafe socket")
            return info.st_dev, info.st_ino, info.st_uid
        except (OSError, ValueError):
            raise _error("PORT_FORWARD_UNSAFE", "RootlessKit requires a same-user protected Unix socket.") from None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("operation deadline")
        return remaining

    def request(self, method, path, expected, payload=None):
        try:
            if self._socket_identity() != self.identity:
                raise ValueError("socket replaced")
            body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
            request = (f"{method} {path} HTTP/1.1\r\nHost: rootlesskit\r\n"
                       f"Connection: close\r\nContent-Type: application/json\r\n"
                       f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._remaining())
                connection.connect(str(self.path))
                if self._socket_identity() != self.identity:
                    raise ValueError("socket replaced")
                if hasattr(socket, "SO_PEERCRED"):
                    peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    if struct.unpack("3i", peer)[1] != os.geteuid():
                        raise ValueError("unexpected socket peer")
                connection.settimeout(self._remaining())
                connection.sendall(request)
                response = http.client.HTTPResponse(_ResponseSocket(connection, self.deadline), method=method)
                try:
                    response.begin()
                    if response.status != expected:
                        raise ValueError("unexpected response status")
                    if response.length is not None and response.length > _LIMIT:
                        raise ValueError("response limit")
                    content = response.read(_LIMIT + 1)
                    if len(content) > _LIMIT:
                        raise ValueError("response limit")
                finally:
                    response.close()
            self._remaining()
            if not content and method == "DELETE":
                return None
            return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
        except DeployError:
            raise
        except (OSError, ValueError, TypeError, http.client.HTTPException, RecursionError):
            # Neither server bodies nor transport exception strings belong in logs/API errors.
            raise _error("PORT_FORWARD_UNAVAILABLE", "RootlessKit port forwarding could not be verified.") from None


class RootlessPortForwarder:
    def __init__(self, settings):
        configured = getattr(settings, "rootlesskit_socket", None)
        if configured is None:
            endpoint = settings.docker_host
            if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
                raise _error("PORT_FORWARD_UNSAFE", "RootlessKit requires an explicit local Unix socket.")
            configured = Path(endpoint.removeprefix("unix://")).parent / "dockerd-rootless" / "api.sock"
        try:
            self.socket_path = Path(configured)
            if not self.socket_path.is_absolute() or any(c in str(self.socket_path) for c in "\x00\n\r?#"):
                raise ValueError("invalid socket path")
        except (TypeError, ValueError):
            raise _error("PORT_FORWARD_UNSAFE", "RootlessKit requires an absolute local Unix socket path.") from None

    @staticmethod
    def _spec(spec):
        valid = (isinstance(spec, dict) and set(spec) == _SPEC_KEYS and spec["proto"] == "tcp4"
                 and spec["parentIP"] == "127.0.0.1" and _port(spec["parentPort"])
                 and _port(spec["childPort"]) and isinstance(spec["childIP"], str))
        if valid:
            try:
                child = ipaddress.IPv4Address(spec["childIP"])
                valid = any(child in network for network in _PRIVATE)
            except ipaddress.AddressValueError:
                valid = False
        if not valid:
            raise _error("PORT_FORWARD_INVALID", "Only localhost TCP4 forwarding to a private IPv4 is allowed.")
        return dict(spec)

    @staticmethod
    def _status(value):
        invalid = _error("PORT_FORWARD_INVALID_RESPONSE", "RootlessKit returned invalid port metadata.")
        if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0:
            raise invalid
        spec = value.get("spec")
        if (not isinstance(spec, dict) or not isinstance(spec.get("proto"), str)
                or spec["proto"] not in _PROTOS or not _port(spec.get("parentPort"))
                or not _port(spec.get("childPort"))):
            raise invalid
        for key in ("parentIP", "childIP"):
            address = spec.get(key, "")
            if not isinstance(address, str):
                raise invalid
            if address:
                try:
                    if "%" in address:
                        raise ValueError("scoped address")
                    ipaddress.ip_address(address)
                except ValueError:
                    raise invalid from None
        return {"id": value["id"], "spec": dict(spec)}

    def _start(self, timeout):
        session = _Session(self.socket_path, timeout)
        info = session.request("GET", "/v1/info", 200)
        valid = isinstance(info, dict)
        if valid:
            driver = info.get("portDriver")
            valid = (isinstance(info.get("apiVersion"), str)
                     and re.fullmatch(r"1\.[1-9][0-9]*\.[0-9]+", info["apiVersion"])
                     and isinstance(info.get("version"), str) and bool(info["version"])
                     and isinstance(info.get("stateDir"), str) and info["stateDir"].startswith("/")
                     and type(info.get("childPID")) is int and info["childPID"] > 0
                     and isinstance(driver, dict) and driver.get("driver") == "builtin"
                     and isinstance(driver.get("protos"), list) and "tcp4" in driver["protos"]
                     and all(isinstance(proto, str) for proto in driver["protos"]))
        if not valid:
            raise _error("PORT_FORWARD_UNSUPPORTED", "RootlessKit builtin TCP4 port forwarding is required.")
        return session, info

    def _list(self, session):
        result = session.request("GET", "/v1/ports", 200)
        # The official router marshals builtin's empty nil slice as JSON null.
        if result is None:
            result = []
        if not isinstance(result, list):
            raise _error("PORT_FORWARD_INVALID_RESPONSE", "RootlessKit returned an invalid port list.")
        ports = [self._status(value) for value in result]
        if len({port["id"] for port in ports}) != len(ports):
            raise _error("PORT_FORWARD_INVALID_RESPONSE", "RootlessKit returned duplicate port identities.")
        return ports

    @staticmethod
    def _match(ports, spec):
        overlapping = [port for port in ports if port["spec"]["parentPort"] == spec["parentPort"]]
        if len(overlapping) > 1 or overlapping and overlapping[0]["spec"] != spec:
            raise _error("PORT_FORWARD_CONFLICT", "RootlessKit port reservation conflict requires attention.")
        return overlapping[0] if overlapping else None

    def check(self, timeout=5):
        return self._start(timeout)[1]

    def ensure(self, spec, timeout=5, *, create=True):
        """Adopt an exact mapping; only an explicitly permitted new intent may create one."""
        if type(create) is not bool:
            raise _error("PORT_FORWARD_INVALID", "Port forwarding creation policy must be explicit.")
        spec = self._spec(spec)
        session, _ = self._start(timeout)
        existing = self._match(self._list(session), spec)
        if existing is not None:
            return existing
        if not create:
            raise _error("PORT_FORWARD_UNCONFIRMED", "Previously requested forwarding is not yet confirmed.")
        created = self._status(session.request("POST", "/v1/ports", 201, spec))
        if created["spec"] != spec or self._match(self._list(session), spec) != created:
            raise _error("PORT_FORWARD_UNCONFIRMED", "RootlessKit did not confirm the requested forwarding.")
        return created

    def remove(self, spec, timeout=5, *, require_present=False):
        """Unknown creation outcomes require a present mapping before cleanup can be confirmed."""
        if type(require_present) is not bool:
            raise _error("PORT_FORWARD_INVALID", "Port forwarding removal policy must be explicit.")
        spec = self._spec(spec)
        session, _ = self._start(timeout)
        existing = self._match(self._list(session), spec)
        if existing is None:
            if require_present:
                raise _error("PORT_FORWARD_UNCONFIRMED", "Previously requested forwarding is not yet confirmed.")
            return
        # Re-read before mutation; never delete an ID whose current full spec changed.
        if self._match(self._list(session), spec) != existing:
            raise _error("PORT_FORWARD_CONFLICT", "RootlessKit forwarding identity changed before removal.")
        result = session.request("DELETE", f"/v1/ports/{existing['id']}", 200)
        if result is not None or self._match(self._list(session), spec) is not None:
            raise _error("PORT_FORWARD_UNCONFIRMED", "RootlessKit did not confirm forwarding removal.")
