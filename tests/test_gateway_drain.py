"""A successful fresh route is not proof that old Nginx workers have drained."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_gateway import request, setup_state
from test_gateway_offline import offline_gateway, offline_request


MASTER = (100, 10, 1, 2)
OLD = (101, 11, 100, 1, 2)
NEW = (102, 12, 100, 1, 2)


def drain_gateway(tmp_path, *, workers=None, response=None, log=None):
    module = setup_state(tmp_path)
    module.PROBE_LOG = tmp_path / 'probe.log'
    module.config_path('demo').write_text(module.render(request(release='old'), tls=True))
    clock = SimpleNamespace(now=0)
    module.time = SimpleNamespace(monotonic=lambda: clock.now,
                                  sleep=lambda n: setattr(clock, 'now', clock.now + n))
    attempts = []

    def snapshot(expected_master=None):
        assert expected_master in (None, MASTER)
        return MASTER, {row[0]: row for row in (workers(clock.now) if workers else [NEW])}

    module.nginx_process_snapshot = snapshot

    class Connection:
        def __init__(self, host, timeout, context=None):
            self.host, self.timeout, self.tls = host, timeout, context is not None
            self.closed = False
            attempts.append(self)

        def request(self, method, path, headers):
            self.path, self.headers = path, headers
            assert method == 'GET' and headers['Connection'] == 'close'

        def getresponse(self):
            clock.now += min(0.05, self.timeout)
            status, marker, pid = response(self, clock.now) if response else (410, 'old:' + 'a' * 64, NEW[0])
            nonce = self.headers.get('X-Awesome-Probe')
            if nonce:
                row = {'probe': nonce, 'host': self.host, 'uri': self.path.split('?')[0],
                       'upstream': '-', 'status': status, 'pid': pid}
                if log:
                    row = log(row, self)
                if row is not None:
                    with module.PROBE_LOG.open('a') as stream:
                        stream.write(json.dumps(row) + '\n')
            return SimpleNamespace(status=status, getheader=lambda key:
                {'X-Awesome-Offline': marker, 'X-Awesome-Gateway-Worker': str(pid)}.get(key))

        def close(self):
            self.closed = True

    module.LocalHTTPS = module.LocalHTTP = Connection
    return module, clock, attempts


def test_fresh_tombstone_does_not_complete_while_old_worker_is_alive(tmp_path):
    module, clock, attempts = drain_gateway(tmp_path, workers=lambda now: [OLD, NEW])
    with pytest.raises(RuntimeError, match='deadline'):
        module.probe_deactivated(offline_request(), 'a' * 64)
    assert 30 <= clock.now < 30.1
    assert all(item.closed for item in attempts)


def test_waits_for_old_worker_natural_exit_without_signalling_it(tmp_path):
    module, clock, _ = drain_gateway(tmp_path, workers=lambda now: [OLD, NEW] if now < 1 else [NEW])
    module.probe_deactivated(offline_request(), 'a' * 64)
    assert 1 <= clock.now < 30


@pytest.mark.parametrize('bad_log', [
    lambda row, conn: None,
    lambda row, conn: {**row, 'pid': OLD[0]},
    lambda row, conn: {**row, 'upstream': '127.0.0.1:20001'},
    lambda row, conn: {**row, 'probe': 'b' * 64},
    lambda row, conn: {**row, 'status': 200},
])
def test_spoofed_worker_header_is_not_a_drain_proof(tmp_path, bad_log):
    module, clock, _ = drain_gateway(tmp_path, log=bad_log)
    with pytest.raises(RuntimeError, match='deadline'):
        module.probe_deactivated(offline_request(), 'a' * 64)
    assert 30 <= clock.now < 30.1


def test_respawn_between_snapshot_and_reload_is_included_in_final_gate(tmp_path):
    # The old-cycle child was absent from the initial pre-reload snapshot.
    module, clock, _ = drain_gateway(tmp_path, workers=lambda now: [NEW] if now == 0 else [OLD, NEW])
    with pytest.raises(RuntimeError, match='deadline'):
        module.probe_deactivated(offline_request(), 'a' * 64)
    assert 30 <= clock.now < 30.1


def test_pid_reuse_during_response_cannot_credit_the_new_process(tmp_path):
    def workers(now):
        return [(NEW[0], int(now * 1000), 100, 1, 2)]
    module, clock, _ = drain_gateway(tmp_path, workers=workers)
    with pytest.raises(RuntimeError, match='deadline'):
        module.probe_deactivated(offline_request(), 'a' * 64)
    assert 30 <= clock.now < 30.1


def test_each_live_worker_needs_its_own_authenticated_log_proof(tmp_path):
    other = (103, 13, 100, 1, 2)
    module, clock, _ = drain_gateway(tmp_path, workers=lambda now: [NEW, other],
        response=lambda conn, now: (410, 'old:' + 'a' * 64, NEW[0] if now < 1 else other[0]))
    module.probe_deactivated(offline_request(), 'a' * 64)
    assert 1 <= clock.now < 30


def test_worker_cannot_be_proved_only_by_http(tmp_path):
    module, clock, _ = drain_gateway(tmp_path,
        response=lambda conn, now: (410, 'old:' + 'a' * 64, OLD[0] if conn.tls else NEW[0]))
    with pytest.raises(RuntimeError, match='deadline'):
        module.probe_deactivated(offline_request(), 'a' * 64)
    assert 30 <= clock.now < 30.1


def test_tombstone_emits_nginx_pid_and_controlled_probe_log(tmp_path):
    module = offline_gateway(tmp_path)
    text = module.render_deactivated(offline_request(), 'a' * 64)
    assert text.count('add_header X-Awesome-Gateway-Worker $pid always;') == 2
    assert text.count(f'access_log {module.PROBE_LOG} awesome_probe if=$awesome_is_probe;') == 2
    assert '"pid":$pid' in module.PROBE_LOGGING


def test_other_application_reuses_domain_and_old_operations_cannot_overwrite_it(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    b = {**request(release='app_b'), 'previous_release': None, 'ports': {'api': 20001},
         'routes': [{'path': '/', 'match': 'prefix', 'service': 'api', 'stripPrefix': False}]}
    module.publish(b)
    module.verify({**b, 'op': 'verify'})
    expected = module.config_path('demo').read_text(), copy.deepcopy(module.read_state('demo'))
    for op in ('deactivate', 'rollback', 'publish', 'verify'):
        with pytest.raises(ValueError):
            getattr(module, op)({**offline_request(), 'op': op})
        assert (module.config_path('demo').read_text(), module.read_state('demo')) == expected
    module.deactivate({**b, 'op': 'deactivate'})
    c = {**b, 'release': 'app_c', 'ports': {'api': 20002}}
    module.publish(c)
    with pytest.raises(ValueError):
        module.rollback({**b, 'op': 'rollback'})
    assert module.read_state('demo')['current']['release'] == 'app_c'


def fake_proc_gateway(tmp_path, monkeypatch):
    import os
    from test_gateway import helper
    module = helper()
    module.PROC = tmp_path / 'proc'
    module.PROC.mkdir()
    host_pid_namespace = tmp_path / 'host-pid-namespace'
    host_pid_namespace.write_bytes(b'host pid namespace fixture')
    (module.PROC / 'self/ns').mkdir(parents=True)
    (module.PROC / 'self/ns/pid').symlink_to(host_pid_namespace)
    binary = tmp_path / 'nginx'
    binary.write_bytes(b'trusted nginx executable fixture')
    module.NGINX = str(binary)
    module.NGINX_PID = tmp_path / 'nginx.pid'
    module.NGINX_PID.write_text('100\n')
    module.trusted = lambda *args, **kwargs: None  # Linux root file ownership is supplied below.
    def root_fstat(fd):
        value = os.fstat(fd)
        return os.stat_result((*value[:4], 0, *value[5:]))
    module.os = SimpleNamespace(**{key: getattr(os, key) for key in dir(os) if not key.startswith('__')})
    module.os.fstat = root_fstat

    def add(pid, ppid, start, role='worker process', uid=33, exe=None, pid_namespace=None):
        folder = module.PROC / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / 'ns').mkdir(exist_ok=True)
        (folder / 'ns/pid').unlink(missing_ok=True)
        (folder / 'ns/pid').symlink_to(pid_namespace or host_pid_namespace)
        # Fields 3..22: state, ppid, ..., starttime.
        (folder / 'stat').write_text(f'{pid} (nginx) S {ppid} ' + '0 ' * 17 + str(start) + '\n')
        (folder / 'cmdline').write_bytes(('nginx: ' + role).encode() + b'\0')
        (folder / 'status').write_text(f'Name:\tnginx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        (folder / 'exe').unlink(missing_ok=True)
        (folder / 'exe').symlink_to(exe or binary)
    add(100, 1, 10, 'master process ' + str(binary), uid=0)
    add(101, 100, 11)
    return module, add


def test_proc_inventory_binds_master_and_all_worker_generations(tmp_path, monkeypatch):
    module, add = fake_proc_gateway(tmp_path, monkeypatch)
    add(102, 100, 12, 'worker process is shutting down')
    master, workers = module.nginx_process_snapshot()
    assert set(workers) == {101, 102}
    assert workers[101][:3] == (101, 11, 100)
    assert module.nginx_process_snapshot(master) == (master, workers)


@pytest.mark.parametrize('change', ['multi_master', 'oldbin', 'pid_symlink', 'pid_mode', 'wrong_ppid',
                                    'wrong_binary', 'master_not_root', 'pid_replaced'])
def test_proc_inventory_refuses_untrusted_or_ambiguous_nginx(tmp_path, monkeypatch, change):
    module, add = fake_proc_gateway(tmp_path, monkeypatch)
    baseline, _ = module.nginx_process_snapshot()
    if change == 'multi_master':
        add(200, 1, 20, 'master process ' + module.NGINX, uid=0)
    elif change == 'oldbin':
        module.NGINX_PID.with_name('nginx.pid.oldbin').write_text('99\n')
    elif change == 'pid_symlink':
        target = tmp_path / 'another.pid'
        target.write_text('100\n')
        module.NGINX_PID.unlink()
        module.NGINX_PID.symlink_to(target)
    elif change == 'pid_mode':
        module.NGINX_PID.chmod(0o666)
    elif change == 'wrong_ppid':
        add(101, 999, 11)
    elif change == 'wrong_binary':
        wrong = tmp_path / 'other' / 'nginx'
        wrong.parent.mkdir()
        wrong.write_bytes(b'untrusted')
        add(101, 100, 11, exe=wrong)
    elif change == 'master_not_root':
        add(100, 1, 10, 'master process ' + module.NGINX, uid=33)
    elif change == 'pid_replaced':
        replacement = tmp_path / 'replacement.pid'
        replacement.write_text('100\n')
        replacement.replace(module.NGINX_PID)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        module.nginx_process_snapshot(baseline)


def test_proc_inventory_rejects_master_starttime_change(tmp_path, monkeypatch):
    module, add = fake_proc_gateway(tmp_path, monkeypatch)
    baseline, _ = module.nginx_process_snapshot()
    add(100, 1, 999, 'master process ' + module.NGINX, uid=0)
    with pytest.raises(RuntimeError, match='master'):
        module.nginx_process_snapshot(baseline)


def test_legacy_tombstone_upgrade_keeps_identity_and_requires_fresh_drain(tmp_path):
    module = offline_gateway(tmp_path)
    module.deactivate(offline_request())
    record = copy.deepcopy(module.read_state('demo')['deactivation'])
    legacy = module.render_deactivated(offline_request(), record['token'], legacy=True)
    assert 'X-Awesome-Gateway-Worker' not in legacy and 'access_log' not in legacy
    module.config_path('demo').write_text(legacy)
    seen = []

    def prove(req, token, **kwargs):
        assert token == record['token']
        assert kwargs['master'] == (1, 1)
        assert 'X-Awesome-Gateway-Worker' in module.config_path('demo').read_text()
        seen.append(token)

    module.probe_deactivated = prove
    assert module.deactivate(offline_request()) == {'deactivated': True, 'release': 'old'}
    assert module.read_state('demo')['deactivation'] == record
    assert seen == [record['token']]


def test_master_change_after_reload_cannot_commit_deactivation(tmp_path):
    module = setup_state(tmp_path)
    module.nginx_process_snapshot = lambda expected=None: (
        (_ for _ in ()).throw(RuntimeError('master changed')) if expected is not None else (MASTER, {}))
    with pytest.raises(RuntimeError, match='master changed'):
        module.deactivate(offline_request())
    state = module.read_state('demo')
    assert state['deactivation']['completed'] is False
    assert state['current']['release'] == 'old'
    assert 'return 410;' in module.config_path('demo').read_text()


def test_drain_failure_keeps_offline_intent_and_retry_reproves_all_workers(tmp_path):
    module, clock, _ = drain_gateway(tmp_path, workers=lambda now: [OLD, NEW])
    # Use the real deactivation write-ahead path with its generated marker.
    sequence = iter(range(100000))

    def token_hex(n):
        count = next(sequence)
        return 'a' * 64 if count == 0 else format(count, '0' + str(n * 2) + 'x')

    module.secrets = SimpleNamespace(token_hex=token_hex)
    with pytest.raises(RuntimeError, match='deadline'):
        module.deactivate(offline_request())
    state = module.read_state('demo')
    assert state['deactivation']['completed'] is False and state['current']['release'] == 'old'
    module.nginx_process_snapshot = lambda expected=None: (MASTER, {NEW[0]: NEW})
    assert module.deactivate(offline_request())['deactivated'] is True
    assert module.read_state('demo')['deactivation']['completed'] is True
    assert 30 < clock.now < 31


def test_empty_upstream_log_value_is_also_a_valid_no_upstream_proof(tmp_path):
    module, clock, _ = drain_gateway(tmp_path, log=lambda row, conn: {**row, 'upstream': ''})
    module.probe_deactivated(offline_request(), 'a' * 64)
    assert clock.now < 1


def test_container_nginx_proc_tree_does_not_count_as_host_masters(tmp_path, monkeypatch):
    module, add = fake_proc_gateway(tmp_path, monkeypatch)
    container_binary = tmp_path / 'container/usr/sbin/nginx'
    container_binary.parent.mkdir(parents=True)
    container_binary.write_bytes(b'separate container nginx binary inode')
    container_pid_namespace = tmp_path / 'container-pid-namespace'
    container_pid_namespace.write_bytes(b'container pid namespace fixture')
    # Host /proc contains host-visible PIDs of both container init and its worker.
    add(200, 50, 20, 'master process /usr/sbin/nginx -g daemon off;', uid=1000,
        exe=container_binary, pid_namespace=container_pid_namespace)
    add(201, 200, 21, exe=container_binary, pid_namespace=container_pid_namespace)
    master, workers = module.nginx_process_snapshot()
    assert master[0] == 100 and set(workers) == {101}
    # A second master sharing the host PID namespace must still be rejected.
    add(202, 1, 22, 'master process ' + module.NGINX, uid=0)
    with pytest.raises(RuntimeError, match='single trusted'):
        module.nginx_process_snapshot(master)
