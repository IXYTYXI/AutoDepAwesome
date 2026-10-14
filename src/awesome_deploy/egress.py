"""Fixed IPv4 HTTPS egress guard inside the rootless Docker network namespace.

The wildcard bridge rules must exist before any managed public-mode network.
An accept here still traverses Docker's later filter chains: in particular,
awi-to-awi isolation depends on Docker's internal bridge isolation remaining
enabled. This table never changes Docker's chains or grants TLS/domain trust.
"""

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct

from .errors import DeployError
from .port_forward import RootlessPortForwarder
from .processes import run_command

PROC = Path("/proc")
MODE = "public-https-v1"
TABLE = "aw_egress_v1"
# Conservatively exclude all special-use IPv4 ranges, including documentation,
# benchmarking, carrier NAT, multicast and link-local/cloud metadata addresses.
NONPUBLIC = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


def _error(code):
    return DeployError(code, code)


def _public(address):
    return address.is_global and not address.is_multicast and not any(
        address in ipaddress.IPv4Network(network) for network in NONPUBLIC
    )


def validate_runtime_policy(value):
    """Validate and detach the exact immutable public policy representation."""
    if not isinstance(value, dict) or set(value) != {"mode", "dns", "blocked_ips"} or value.get("mode") != MODE:
        raise _error("EGRESS_POLICY_INVALID")
    result = {"mode": MODE}
    for key in ("dns", "blocked_ips"):
        values = value[key]
        if not isinstance(values, list) or not values or len(values) > 64:
            raise _error("EGRESS_POLICY_INVALID")
        addresses = []
        for text in values:
            try:
                address = ipaddress.IPv4Address(text) if isinstance(text, str) else None
            except ValueError:
                address = None
            if address is None or str(address) != text or (key == "dns" and not _public(address)):
                raise _error("EGRESS_POLICY_INVALID")
            addresses.append(address)
        if len(set(values)) != len(values) or (key == "blocked_ips" and not any(_public(a) for a in addresses)):
            raise _error("EGRESS_POLICY_INVALID")
        result[key] = list(values)
    if set(result["dns"]) & set(result["blocked_ips"]):
        raise _error("EGRESS_POLICY_INVALID")
    return result


def policy(settings):
    mode = getattr(settings, "runtime_network", "internal")
    if mode == "internal":
        return None
    if mode != MODE:
        raise _error("EGRESS_POLICY_INVALID")
    try:
        return validate_runtime_policy({"mode": mode, "dns": list(settings.runtime_dns),
                                        "blocked_ips": list(settings.runtime_blocked_ips)})
    except (AttributeError, TypeError):
        raise _error("EGRESS_POLICY_INVALID") from None


def validate_policy(job):
    snapshot = job.get("snapshot") if isinstance(job, dict) else None
    if not isinstance(snapshot, dict):
        raise _error("EGRESS_POLICY_INVALID")
    if "runtime_network" not in snapshot:
        return None
    return validate_runtime_policy(snapshot["runtime_network"])


def bridge_name(network, external):
    if not isinstance(network, str) or not network or type(external) is not bool:
        raise _error("EGRESS_POLICY_INVALID")
    return ("awx-" if external else "awi-") + hashlib.sha256(network.encode()).hexdigest()[:11]


def _match(left, right, op="=="):
    return {"match": {"op": op, "left": left, "right": right}}


def _meta(key, value):
    return _match({"meta": {"key": key}}, value)


def _ct(key, value):
    return _match({"ct": {"key": key}}, value, "in" if key == "state" else "==")


def _payload(protocol, field, value):
    return _match({"payload": {"protocol": protocol, "field": field}}, value)


def ruleset(frozen):
    """Return one atomic nft JSON creation batch, also used for strict readback."""
    frozen = validate_runtime_policy(frozen)
    objects = [{"table": {"family": "inet", "name": TABLE}}]
    for name, hook in (("input_guard", "input"), ("forward_guard", "forward"),
                       ("managed_input", None), ("external_out", None), ("external_in", None)):
        chain = {"family": "inet", "table": TABLE, "name": name}
        if hook:
            chain.update(type="filter", hook=hook, prio=-10, policy="accept")
        objects.append({"chain": chain})

    def rule(chain, *expressions):
        objects.append({"rule": {"family": "inet", "table": TABLE, "chain": chain,
                                  "expr": list(expressions)}})

    accept, drop = {"accept": None}, {"drop": None}
    for interface in ("awi-*", "awx-*"):
        rule("input_guard", _meta("iifname", interface), {"jump": {"target": "managed_input"}})
    rule("managed_input", _meta("nfproto", "ipv6"), drop)
    rule("managed_input", _meta("nfproto", "ipv4"), _ct("direction", "reply"), _ct("state", "established"), accept)
    rule("managed_input", drop)
    # Docker's later internal-network chains reject cross-bridge traffic.
    rule("forward_guard", _meta("iifname", "awi-*"), _meta("oifname", "awi-*"), _meta("nfproto", "ipv4"), accept)
    rule("forward_guard", _meta("iifname", "awi-*"), drop)
    rule("forward_guard", _meta("oifname", "awi-*"), drop)
    rule("forward_guard", _meta("iifname", "awx-*"), {"jump": {"target": "external_out"}})
    rule("forward_guard", _meta("oifname", "awx-*"), {"jump": {"target": "external_in"}})
    rule("external_out", _meta("nfproto", "ipv6"), drop)
    for cidr in NONPUBLIC:
        network = ipaddress.IPv4Network(cidr)
        rule("external_out", _payload("ip", "daddr", {"prefix": {"addr": str(network.network_address), "len": network.prefixlen}}), drop)
    for address in frozen["blocked_ips"]:
        rule("external_out", _payload("ip", "daddr", address), drop)
    for state in ("new", "established"):
        base = [_meta("oifname", "tap0"), _meta("nfproto", "ipv4"), _ct("direction", "original"), _ct("state", state)]
        rule("external_out", *base, _payload("tcp", "dport", 443), accept)
        for address in frozen["dns"]:
            for protocol in ("udp", "tcp"):
                rule("external_out", *base, _payload("ip", "daddr", address), _payload(protocol, "dport", 53), accept)
    rule("external_out", drop)
    rule("external_in", _meta("nfproto", "ipv6"), drop)
    rule("external_in", _meta("iifname", "tap0"), _meta("nfproto", "ipv4"), _ct("direction", "reply"), _ct("state", "established"), accept)
    rule("external_in", _meta("iifname", "tap0"), _meta("nfproto", "ipv4"), _ct("direction", "reply"), _ct("state", "related"), _meta("l4proto", "icmp"), accept)
    rule("external_in", drop)
    return {"nftables": [{"add": item} for item in objects]}


def _normalized(objects):
    """Ignore only kernel-assigned handles; preserve every rule and its order."""
    tables, chains, rules = [], {}, {}
    for item in objects:
        if not isinstance(item, dict) or len(item) != 1:
            raise _error("EGRESS_GUARD_CHANGED")
        kind, value = next(iter(item.items()))
        if kind == "metainfo":
            continue
        if kind not in {"table", "chain", "rule"} or not isinstance(value, dict):
            raise _error("EGRESS_GUARD_CHANGED")
        value = {key: copy.deepcopy(val) for key, val in value.items() if key != "handle"}
        if kind == "table":
            tables.append(value)
        elif kind == "chain":
            name = value.get("name")
            if not isinstance(name, str) or name in chains:
                raise _error("EGRESS_GUARD_CHANGED")
            chains[name] = value
        else:
            name = value.get("chain")
            if not isinstance(name, str):
                raise _error("EGRESS_GUARD_CHANGED")
            expressions = value.get("expr", [])
            # nft's readback omits a redundant family test only where a
            # positive IPv4 payload or IPv4 ICMP match already implies it.
            # Do not erase family checks on TCP-only/conntrack-only rules.
            implied_ipv4 = any(
                expression.get("match", {}).get("op") == "=="
                and expression.get("match", {}).get("left", {}).get("payload", {}).get("protocol") == "ip"
                or expression == _meta("l4proto", "icmp")
                for expression in expressions
            )
            if implied_ipv4:
                value["expr"] = [expression for expression in expressions if expression != _meta("nfproto", "ipv4")]
            rules.setdefault(name, []).append(value)
    return tables, chains, rules


class EgressGuard:
    def __init__(self, settings):
        self.settings = settings
        self.lock_fd = None

    def _rootless_driver(self):
        try:
            info = RootlessPortForwarder(self.settings).check(timeout=5)
        except Exception:
            raise _error("EGRESS_UNAVAILABLE") from None
        network = info.get("networkDriver", {}) if isinstance(info, dict) else {}
        port = info.get("portDriver", {}) if isinstance(info, dict) else {}
        extra = port.get("extra", {}) if isinstance(port, dict) else {}
        if (not isinstance(network, dict) or network.get("driver") != "slirp4netns"
                or not isinstance(extra, dict) or extra.get("sourceIPTransparentBackend") != "none"):
            raise _error("EGRESS_NAMESPACE_UNSAFE")

    @staticmethod
    def _close_namespace(namespace):
        for fd in namespace[:2]:
            os.close(fd)

    def _docker_peer(self, pid, uid):
        """Bind the firewall namespace to the daemon serving the configured API."""
        try:
            host = self.settings.docker_host
            if not isinstance(host, str) or not host.startswith("unix:///"):
                raise ValueError
            path = Path(host.removeprefix("unix://"))
            if ".." in path.parts:
                raise ValueError
            before = path.lstat()
            if not stat.S_ISSOCK(before.st_mode) or before.st_uid != uid or before.st_mode & 0o002:
                raise ValueError
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(path))
                # Linux SO_PEERCRED; using its stable value keeps static/unit
                # validation available on non-Linux developer workstations.
                peer_pid, peer_uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, 17, 12))
                after = path.lstat()
                if peer_pid != pid or peer_uid != uid or before != after:
                    raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, struct.error):
            raise _error("EGRESS_NAMESPACE_UNSAFE") from None

    def _namespace(self):
        opened = []
        try:
            uid = os.getuid()
            if uid == 0 or os.geteuid() != uid:
                raise ValueError
            pidfile = getattr(self.settings, "egress_docker_pid_file", None)
            if pidfile is None:
                host = self.settings.docker_host
                if not host.startswith("unix:///"):
                    raise ValueError
                pidfile = Path(host.removeprefix("unix://")).parent / "docker.pid"
            pidfile = Path(pidfile)
            if not pidfile.is_absolute() or ".." in pidfile.parts:
                raise ValueError
            info = pidfile.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
                raise ValueError
            pidtext = pidfile.read_text().strip()
            if not re.fullmatch(r"[1-9][0-9]{0,9}", pidtext):
                raise ValueError
            process = PROC / pidtext

            def identity():
                if process.stat().st_uid != uid or (process / "comm").read_text().strip() != "dockerd":
                    raise ValueError
                mapping = [line.split() for line in (process / "uid_map").read_text().splitlines()]
                if not mapping or mapping[0] != ["0", str(uid), "1"]:
                    raise ValueError
                fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
                return fields[19]

            start = identity()
            self._docker_peer(int(pidtext), uid)
            paths = (process / "ns" / "user", process / "root" / pidfile.parent.relative_to("/") / "dockerd-rootless" / "netns")
            for target, kind in zip(paths, ("user", "net")):
                fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
                opened.append(fd)
                theirs, ours = os.fstat(fd), (PROC / "self" / "ns" / kind).stat()
                if (theirs.st_dev, theirs.st_ino) == (ours.st_dev, ours.st_ino):
                    raise ValueError
            if identity() != start:
                raise ValueError
            return *opened, int(pidtext)
        except (OSError, ValueError, TypeError, AttributeError, IndexError):
            for fd in opened:
                os.close(fd)
            raise _error("EGRESS_NAMESPACE_UNSAFE") from None

    def _run(self, namespace, arguments, input_text=None):
        user, net, _ = namespace
        fds = tuple(fd for fd in (user, net, self.lock_fd) if isinstance(fd, int))
        try:
            return run_command(["nsenter", f"--user=/proc/self/fd/{user}", f"--net=/proc/self/fd/{net}",
                                "--setuid=0", "--setgid=0", "--", "nft", "-j", *arguments],
                               timeout=10, input_text=input_text, pass_fds=fds, output_limit=262144)
        except Exception:
            raise _error("EGRESS_UNAVAILABLE") from None

    def _read(self, namespace, arguments):
        result = self._run(namespace, arguments)
        try:
            value = json.loads(result.stdout)
            if set(value) != {"nftables"} or not isinstance(value["nftables"], list):
                raise ValueError
            return value["nftables"]
        except (ValueError, TypeError):
            raise _error("EGRESS_GUARD_CHANGED") from None

    def _verify(self, frozen, install):
        frozen = validate_runtime_policy(frozen)
        if frozen != policy(self.settings):
            raise _error("EGRESS_POLICY_CHANGED")
        expected = ruleset(frozen)
        self._rootless_driver()
        namespace = self._namespace()
        try:
            tables = self._read(namespace, ["list", "tables"])
            exists = any(item.get("table", {}).get("family") == "inet" and item["table"].get("name") == TABLE for item in tables)
            if not exists:
                if not install:
                    raise _error("EGRESS_GUARD_MISSING")
                # One kernel transaction; never flush/delete an existing table.
                self._run(namespace, ["-f", "-"], json.dumps(expected, separators=(",", ":")))
            actual = self._read(namespace, ["list", "table", "inet", TABLE])
            if _normalized(actual) != _normalized([item["add"] for item in expected["nftables"]]):
                raise _error("EGRESS_GUARD_CHANGED")
            return frozen
        finally:
            self._close_namespace(namespace)

    def ensure(self, frozen):
        return self._verify(frozen, True)

    def check(self, frozen):
        return self._verify(frozen, False)
