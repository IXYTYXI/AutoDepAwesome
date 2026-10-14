"""Fixed egress policy and transactional namespace firewall contract."""
import copy
import fnmatch
import importlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace

import pytest

from awesome_deploy.errors import DeployError
from awesome_deploy.processes import CommandResult


PUBLIC = {"mode": "public-https-v1", "dns": ["223.5.5.5", "119.29.29.29"],
          "blocked_ips": ["8.8.4.4"]}


def module():
    try:
        return importlib.import_module("awesome_deploy.egress")
    except ModuleNotFoundError:
        pytest.fail("The fixed rootless egress guard has not been implemented")


def settings(**changes):
    values = dict(runtime_network="public-https-v1", runtime_dns=tuple(PUBLIC["dns"]),
                  runtime_blocked_ips=tuple(PUBLIC["blocked_ips"]),
                  docker_host="unix:///run/user/1000/docker.sock", egress_docker_pid_file=None)
    values.update(changes)
    return SimpleNamespace(**values)


def test_legacy_absence_is_internal_and_bridge_names_are_stable():
    eg = module()
    assert eg.validate_policy({"snapshot": {}}) is None
    assert eg.policy(SimpleNamespace(runtime_network="internal")) is None
    internal, external = eg.bridge_name("aw-job-s-web", False), eg.bridge_name("aw-job-s-web", True)
    assert internal.startswith("awi-") and external.startswith("awx-")
    assert len(internal) == len(external) == 15
    assert internal[4:] == external[4:]
    assert eg.bridge_name("aw-other-s-web", False) != internal


@pytest.mark.parametrize("value", [None, {}, {"mode": "internal"}, {**PUBLIC, "unknown": True},
    {**PUBLIC, "dns": []}, {**PUBLIC, "dns": ["127.0.0.1"]},
    {**PUBLIC, "dns": ["1.1.1.1", "1.1.1.1"]}, {**PUBLIC, "dns": ["::1"]},
    {**PUBLIC, "blocked_ips": []}, {**PUBLIC, "blocked_ips": ["192.168.1.1"]},
    {**PUBLIC, "blocked_ips": ["8.8.4.4/32"]}, {**PUBLIC, "dns": ["223.5.5.5; flush ruleset"]}])
def test_explicit_malformed_policy_never_becomes_legacy(value):
    with pytest.raises(DeployError) as error:
        module().validate_policy({"snapshot": {"runtime_network": value}})
    assert error.value.code == "EGRESS_POLICY_INVALID"


def test_frozen_policy_is_detached_and_does_not_inherit_settings():
    eg = module()
    frozen = copy.deepcopy(PUBLIC)
    result = eg.validate_policy({"snapshot": {"runtime_network": frozen}})
    result["dns"].append("1.1.1.1")
    assert frozen == PUBLIC
    assert eg.policy(settings()) == PUBLIC


@pytest.fixture
def kernel(monkeypatch):
    eg = module()
    state = SimpleNamespace(objects=None, calls=[], changed=False, fail_apply=False)
    monkeypatch.setattr(eg.EgressGuard, "_namespace", lambda self: ("user", "net", 123))
    monkeypatch.setattr(eg.EgressGuard, "_close_namespace", lambda self, value: None)
    monkeypatch.setattr(eg.EgressGuard, "_rootless_driver", lambda self: None, raising=False)

    def run(argv, **kwargs):
        state.calls.append((argv, kwargs))
        if "list" in argv:
            objects = state.objects
            if argv[-1] == "tables":
                objects = [] if objects is None else [objects[0]]
            return CommandResult(json.dumps({"nftables": [{"metainfo": {"version": "fixture"}},
                                                          *(objects or [])]}), "", 0)
        assert kwargs.get("input_text") is not None
        if state.fail_apply:
            raise DeployError("COMMAND_FAILED", "untrusted stderr secret-token")
        commands = json.loads(kwargs["input_text"])["nftables"]
        installed = []
        for command in commands:
            assert set(command) == {"add"}
            installed.append(copy.deepcopy(command["add"]))
        for index, item in enumerate(installed):
            next(iter(item.values()))["handle"] = index + 1
        state.objects = installed
        if state.changed:
            state.objects[-1]["rule"]["expr"][-1] = {"accept": None}
        return CommandResult("", "", 0)

    monkeypatch.setattr(eg, "run_command", run)
    return eg, state


def test_install_is_one_transaction_then_readback_and_check_is_readonly(kernel):
    eg, state = kernel
    guard = eg.EgressGuard(settings())
    guard.lock_fd = 77
    assert guard.ensure(PUBLIC) == PUBLIC
    writes = [(argv, kw) for argv, kw in state.calls if kw.get("input_text") is not None]
    assert len(writes) == 1
    argv, kw = writes[0]
    assert argv[0] == "nsenter" and "nft" in argv and "-j" in argv
    assert "77" not in argv and 77 in kw["pass_fds"]
    assert "flush" not in kw["input_text"] and "delete" not in kw["input_text"]
    state.calls.clear()
    assert guard.check(PUBLIC) == PUBLIC
    assert guard.ensure(PUBLIC) == PUBLIC
    assert not any(kw.get("input_text") for _, kw in state.calls)


def test_missing_check_never_installs(kernel):
    eg, state = kernel
    with pytest.raises(DeployError) as error:
        eg.EgressGuard(settings()).check(PUBLIC)
    assert error.value.code == "EGRESS_GUARD_MISSING"
    assert all(kw.get("input_text") is None for _, kw in state.calls)


def test_frozen_configuration_change_cannot_rewrite_global_policy(kernel):
    eg, state = kernel
    with pytest.raises(DeployError) as error:
        eg.EgressGuard(settings()).ensure({**PUBLIC, "dns": ["1.1.1.1"]})
    assert error.value.code == "EGRESS_POLICY_CHANGED"
    assert not state.calls


def test_changed_existing_table_is_never_replaced(kernel):
    eg, state = kernel
    guard = eg.EgressGuard(settings())
    guard.ensure(PUBLIC)
    state.objects[-1]["rule"]["expr"][-1] = {"accept": None}
    state.calls.clear()
    with pytest.raises(DeployError) as error:
        guard.ensure(PUBLIC)
    assert error.value.code == "EGRESS_GUARD_CHANGED"
    assert all(kw.get("input_text") is None for _, kw in state.calls)


def test_failed_apply_suppresses_untrusted_exception_and_readback_detects_change(kernel):
    eg, state = kernel
    state.fail_apply = True
    with pytest.raises(DeployError) as error:
        eg.EgressGuard(settings()).ensure(PUBLIC)
    assert error.value.code == "EGRESS_UNAVAILABLE" and "secret-token" not in str(error.value)
    state.fail_apply, state.changed = False, True
    with pytest.raises(DeployError) as error:
        eg.EgressGuard(settings()).ensure(PUBLIC)
    assert error.value.code == "EGRESS_GUARD_CHANGED"


def test_guard_rules_have_wildcard_default_drops_and_bound_directions(kernel):
    eg, state = kernel
    eg.EgressGuard(settings()).ensure(PUBLIC)
    rules = [row["rule"] for row in state.objects if "rule" in row]
    text = json.dumps(rules)
    for value in ("awi-*", "awx-*", "tap0", "reply", "ipv6", "223.5.5.5", "8.8.4.4"):
        assert value in text
    assert '"dport"' in text and '"right": 443' in text and '"right": 53' in text
    assert '"right": 80' not in text
    for name in ("managed_input", "external_out", "external_in"):
        assert [r for r in rules if r["chain"] == name][-1]["expr"] == [{"drop": None}]


@pytest.fixture
def proc_tree(tmp_path, monkeypatch):
    eg = module()
    uid = os.getuid() or 1000
    proc, runtime = tmp_path / "proc", tmp_path / "run" / "user" / str(uid)
    pid = proc / "123"
    for path in (pid / "ns", proc / "self" / "ns", runtime):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "docker.pid").write_text("123\n")
    (pid / "comm").write_text("dockerd\n")
    (pid / "uid_map").write_text(f"0 {uid} 1\n1 100000 65536\n")
    (pid / "stat").write_text("123 (dockerd) S " + " ".join(["0"] * 18 + ["456", "0"]))
    for path in (pid / "ns" / "user", proc / "self" / "ns" / "user", proc / "self" / "ns" / "net"):
        path.write_text("namespace fixture")
    net = pid / "root" / runtime.relative_to("/") / "dockerd-rootless" / "netns"
    net.parent.mkdir(parents=True)
    net.write_text("network namespace fixture")
    monkeypatch.setattr(eg, "PROC", proc)
    monkeypatch.setattr(eg.os, "getuid", lambda: uid)
    monkeypatch.setattr(eg.os, "geteuid", lambda: uid)
    monkeypatch.setattr(eg.EgressGuard, "_docker_peer", lambda self, pid, owner: None, raising=False)
    real_stat = Path.stat

    def owned(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.getuid() != result.st_uid:
            result = os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                                    uid, result.st_gid, result.st_size, result.st_atime,
                                    result.st_mtime, result.st_ctime))
        return result

    monkeypatch.setattr(Path, "stat", owned)
    config = settings()
    config.egress_docker_pid_file = runtime / "docker.pid"
    return eg, eg.EgressGuard(config), pid, net, proc


def test_namespace_uses_dockerd_root_bound_netns_not_process_netns(proc_tree):
    eg, guard, pid, net, proc = proc_tree
    handle = guard._namespace()
    try:
        assert len(handle) == 3
        assert all(isinstance(fd, int) for fd in handle[:2])
        assert os.fstat(handle[1]).st_ino == net.stat().st_ino
    finally:
        guard._close_namespace(handle)


@pytest.mark.parametrize("change", ["pid", "comm", "map", "host-net", "host-user"])
def test_namespace_identity_mismatch_is_refused(proc_tree, change):
    eg, guard, pid, net, proc = proc_tree
    if change == "pid":
        guard.settings.egress_docker_pid_file.write_text("123;whoami")
    elif change == "comm":
        (pid / "comm").write_text("python")
    elif change == "map":
        (pid / "uid_map").write_text("0 0 4294967295")
    else:
        source = proc / "self" / "ns" / ("net" if change == "host-net" else "user")
        destination = net if change == "host-net" else pid / "ns" / "user"
        destination.unlink()
        destination.hardlink_to(source)
    with pytest.raises(DeployError) as error:
        guard._namespace()
    assert error.value.code == "EGRESS_NAMESPACE_UNSAFE"


@pytest.mark.parametrize("change", ["valid", "another-daemon", "another-user", "socket-replaced", "unsafe-mode"])
def test_docker_socket_peer_must_match_namespace_daemon(tmp_path, monkeypatch, change):
    eg = module()
    path = tmp_path / "docker.sock"
    config = settings(docker_host="unix://" + str(path))
    guard = eg.EgressGuard(config)
    owner, peer = 1000, 123
    count = 0

    def identity(self):
        nonlocal count
        count += 1
        inode = 77 + int(change == "socket-replaced" and count > 1)
        mode = stat.S_IFSOCK | (0o666 if change == "unsafe-mode" else 0o660)
        return os.stat_result((mode, inode, 1, 1, owner, owner, 0, 0, 0, 0))

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, timeout):
            assert timeout <= 5

        def connect(self, target):
            assert target == str(path)

        def getsockopt(self, *args):
            return struct.pack("3i", peer + int(change == "another-daemon"),
                               owner + int(change == "another-user"), owner)

    monkeypatch.setattr(Path, "lstat", identity)
    monkeypatch.setattr(eg.socket, "socket", lambda *_: Socket())
    if change == "valid":
        guard._docker_peer(peer, owner)
        return
    with pytest.raises(DeployError) as error:
        guard._docker_peer(peer, owner)
    assert error.value.code == "EGRESS_NAMESPACE_UNSAFE"


def verdict(objects, chain, packet):
    """Evaluate only nft's documented match/jump/verdict subset used here.

    This tests decisions, not just that rule strings happen to exist. The real
    nft compiler and Docker's subsequent bridge isolation need Linux tests.
    """
    for item in objects:
        rule = item.get("rule")
        if not rule or rule["chain"] != chain:
            continue
        matched = True
        for expression in rule["expr"]:
            if "match" in expression:
                match = expression["match"]
                left, right = match["left"], match["right"]
                if "meta" in left:
                    value = packet.get(left["meta"]["key"])
                elif "ct" in left:
                    value = packet.get("ct_" + left["ct"]["key"])
                else:
                    payload = left["payload"]
                    if payload["protocol"] not in {packet.get("l4proto"), "ip" if packet.get("nfproto") == "ipv4" else "ip6"}:
                        matched = False
                        break
                    value = packet.get(payload["field"])
                if isinstance(right, dict) and "prefix" in right:
                    prefix = right["prefix"]
                    same = value is not None and ipaddress.ip_address(value) in ipaddress.ip_network(f'{prefix["addr"]}/{prefix["len"]}')
                elif isinstance(right, str) and "*" in right:
                    same = isinstance(value, str) and fnmatch.fnmatchcase(value, right)
                else:
                    same = value == right
                if not same:
                    matched = False
                    break
            elif "jump" in expression:
                decision = verdict(objects, expression["jump"]["target"], packet)
                if decision is not None:
                    return decision
            elif "accept" in expression:
                if matched:
                    return True
            elif "drop" in expression:
                if matched:
                    return False
            else:
                pytest.fail("unsupported rule in policy decision test")
    return None


@pytest.mark.parametrize(("changes", "allowed"), [
    ({}, True), ({"dport": 80}, False), ({"l4proto": "udp"}, False),
    ({"daddr": "10.0.1.223"}, False), ({"daddr": "127.0.0.1"}, False),
    ({"daddr": "169.254.169.254"}, False), ({"daddr": "100.64.0.1"}, False),
    ({"daddr": "192.0.2.1"}, False), ({"daddr": "224.1.1.1"}, False),
    ({"daddr": "8.8.4.4"}, False), ({"nfproto": "ipv6", "daddr": "2606:4700::1111"}, False),
    ({"ct_state": "invalid"}, False), ({"ct_direction": "reply"}, False),
    ({"dport": 53, "daddr": "223.5.5.5", "l4proto": "udp"}, True),
    ({"dport": 53, "daddr": "119.29.29.29"}, True),
    ({"dport": 53, "daddr": "1.1.1.1", "l4proto": "udp"}, False),
    ({"oifname": "awx-other"}, False), ({"oifname": "awi-other"}, False),
    ({"iifname": "awi-one"}, False),
    ({"iifname": "awi-one", "oifname": "awi-two"}, True),
    ({"iifname": "tap0", "oifname": "awx-one", "ct_direction": "reply", "ct_state": "established"}, True),
    ({"iifname": "tap0", "oifname": "awx-one"}, False),
    ({"iifname": "another-uplink", "oifname": "awx-one", "ct_direction": "reply", "ct_state": "established"}, False),
])
def test_forward_packet_decisions(kernel, changes, allowed):
    eg, state = kernel
    eg.EgressGuard(settings()).ensure(PUBLIC)
    packet = {"iifname": "awx-one", "oifname": "tap0", "nfproto": "ipv4", "l4proto": "tcp",
              "dport": 443, "daddr": "1.1.1.1", "ct_direction": "original", "ct_state": "new"}
    packet.update(changes)
    assert verdict(state.objects, "forward_guard", packet) is allowed


@pytest.mark.parametrize(("direction", "state_name", "allowed"), [
    ("reply", "established", True), ("original", "established", False),
    ("original", "new", False), ("reply", "invalid", False),
])
def test_managed_host_input_only_accepts_rootlesskit_replies(kernel, direction, state_name, allowed):
    eg, state = kernel
    eg.EgressGuard(settings()).ensure(PUBLIC)
    assert verdict(state.objects, "input_guard", {"iifname": "awi-one", "nfproto": "ipv4",
                   "ct_direction": direction, "ct_state": state_name}) is allowed


def test_readback_accepts_only_implied_ipv4_family_elision(kernel):
    eg, state = kernel
    guard = eg.EgressGuard(settings())
    guard.ensure(PUBLIC)
    family = {"match": {"op": "==", "left": {"meta": {"key": "nfproto"}}, "right": "ipv4"}}
    count = 0
    for item in state.objects:
        expressions = item.get("rule", {}).get("expr", [])
        implied = any(expression.get("match", {}).get("left", {}).get("payload", {}).get("protocol") == "ip"
                      or expression == {"match": {"op": "==", "left": {"meta": {"key": "l4proto"}}, "right": "icmp"}}
                      for expression in expressions)
        if implied and family in expressions:
            expressions.remove(family)
            count += 1
    assert count == 9  # Eight DNS rules and one related IPv4 ICMP rule.
    guard.check(PUBLIC)
    for item in state.objects:
        expressions = item.get("rule", {}).get("expr", [])
        if item.get("rule", {}).get("chain") == "managed_input" and family in expressions:
            expressions.remove(family)
            break
    with pytest.raises(DeployError) as error:
        guard.check(PUBLIC)
    assert error.value.code == "EGRESS_GUARD_CHANGED"


@pytest.mark.parametrize("change", ["valid", "driver", "transparent", "unknown-transparent"])
def test_rootless_driver_must_be_slirp_and_nontransparent(monkeypatch, change):
    eg = module()
    info = {"networkDriver": {"driver": "slirp4netns"},
            "portDriver": {"extra": {"sourceIPTransparentBackend": "none"}}}
    if change == "driver":
        info["networkDriver"]["driver"] = "host"
    elif change == "transparent":
        info["portDriver"]["extra"]["sourceIPTransparentBackend"] = "bypass4netns"
    elif change == "unknown-transparent":
        info["portDriver"]["extra"] = {}
    monkeypatch.setattr(eg.RootlessPortForwarder, "check", lambda self, timeout=5: info)
    guard = eg.EgressGuard(settings())
    if change == "valid":
        guard._rootless_driver()
    else:
        with pytest.raises(DeployError) as error:
            guard._rootless_driver()
        assert error.value.code == "EGRESS_NAMESPACE_UNSAFE"
