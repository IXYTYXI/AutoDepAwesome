#!/usr/bin/env python3
"""Operator-run Linux acceptance using disposable, isolated platform fixtures.

Run as the dedicated worker UID, with its config and an already available Python
image. Does not submit jobs, touch customer code, or publish application routes.
Requires public-https-v1 to be enabled in the supplied (possibly staging) config.
"""

import argparse
import copy
from dataclasses import replace
import fcntl
import http.client
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import time
import uuid

from awesome_deploy.cli import load_settings
from awesome_deploy.egress import EgressGuard, policy
from awesome_deploy.runtime import DeploymentBackend


SERVER = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200)
  self.send_header('Content-Type', 'text/event-stream' if self.path == '/stream' else 'text/plain')
  self.end_headers()
  self.wfile.write(b'data: first\\n\\n'); self.wfile.flush()
  if self.path == '/stream':
   time.sleep(2)
   self.wfile.write(b'data: second\\n\\n'); self.wfile.flush()
 def log_message(self, *args): pass
ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""


def verify_stream(root, upstream):
    """Exercise rendered gateway locations in a separate loopback-only Nginx."""
    loader = importlib.machinery.SourceFileLoader('acceptance_gateway', str(Path(__file__).with_name('awesome-gateway')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    gateway = importlib.util.module_from_spec(spec)
    loader.exec_module(gateway)
    gateway.CONFIG = {'base_domain': 'example.invalid', 'port_start': 1, 'port_end': 65535}
    gateway.PROBE_LOG = root / 'probe.log'
    cert, key = root / 'cert.pem', root / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=awnet.example.invalid', '-keyout', str(key), '-out', str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sockets = [socket.socket(), socket.socket()]
    for sock in sockets:
        sock.bind(('127.0.0.1', 0))
    http_port, https_port = [sock.getsockname()[1] for sock in sockets]
    for sock in sockets:
        sock.close()
    rendered = gateway.render({
        'op': 'publish', 'base_domain': 'example.invalid', 'subdomain': 'awnet',
        'release': 'acceptance', 'previous_release': None, 'ports': {'web': upstream},
        'routes': [{'path': '/', 'match': 'prefix', 'service': 'web', 'stripPrefix': False}],
    }, tls=True)
    rendered = rendered.replace('listen 80;', f'listen 127.0.0.1:{http_port};')
    rendered = rendered.replace('listen 443 ssl;', f'listen 127.0.0.1:{https_port} ssl;')
    rendered = rendered.replace('/etc/letsencrypt/live/awnet.example.invalid/fullchain.pem', str(cert))
    rendered = rendered.replace('/etc/letsencrypt/live/awnet.example.invalid/privkey.pem', str(key))
    conf = root / 'nginx.conf'
    conf.write_text(f'daemon off; pid {root}/nginx.pid; error_log {root}/nginx-error.log;\n'
                    'events {}\nhttp {\n'
                    f'client_body_temp_path {root}/body; proxy_temp_path {root}/proxy;\n'
                    f'fastcgi_temp_path {root}/fastcgi; uwsgi_temp_path {root}/uwsgi; scgi_temp_path {root}/scgi;\n'
                    'log_format awesome_probe "$request"; map $uri $awesome_is_probe { default 0; }\n'
                    f'access_log {root}/access.log;\n' + rendered + '\n}\n')
    process = subprocess.Popen(['/usr/sbin/nginx', '-p', str(root), '-c', str(conf)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError('Isolated Nginx failed: inspect fixture nginx-error.log')
            try:
                with socket.create_connection(('127.0.0.1', https_port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        # Only this temporary loopback certificate is untrusted. Public model TLS
        # uses the default trusted CA context in the independent container probe.
        conn = http.client.HTTPSConnection('127.0.0.1', https_port, timeout=5,
                                           context=ssl._create_unverified_context())
        started = time.monotonic()
        conn.request('GET', '/stream', headers={'Host': 'awnet.example.invalid'})
        response = conn.getresponse()
        first = response.readline()
        elapsed = time.monotonic() - started
        assert response.status == 200 and first == b'data: first\n' and elapsed < 1.5
        assert b'data: second' in response.read()
        conn.close()
        return {'first_chunk_seconds': round(elapsed, 3), 'upstream_chunk_gap_seconds': 2}
    finally:
        process.terminate()
        process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--image', default='python:3.12-slim')
    parser.add_argument('--https-host', default='api.m1nt.store')
    args = parser.parse_args()
    original = load_settings(args.config)
    assert original.runtime_network == 'public-https-v1'
    with (original.state_dir / 'worker.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_acceptance(args, original, lock)


def run_acceptance(args, original, lock):
    identity = uuid.uuid4().hex
    root = original.state_dir / ('network-acceptance-' + identity)
    root.mkdir(mode=0o700)
    settings = replace(original, state_dir=root)
    backend = DeploymentBackend(settings)
    backend.lock_fd = lock.fileno()
    image = backend._image(args.image)['Id']
    report = {'fixture': identity, 'checks': {}}
    service = {
        'image': image, 'port': 8080, 'env': {}, 'secretRefs': {}, 'dependsOn': {},
        'resources': {'cpu': 0.1, 'memoryMiB': 64},
        'healthcheck': {'type': 'http', 'path': '/', 'intervalSeconds': 1,
                        'timeoutSeconds': 2, 'retries': 8, 'startPeriodSeconds': 0},
        'entrypoint': ['/usr/local/bin/python3'], 'command': ['-u', '-c', SERVER],
    }
    services = {name: copy.deepcopy(service) for name in ('web', 'api', 'isolated')}
    services['web']['dependsOn'] = {'api': 'healthy'}
    # Stay inside the configured forwarding range and exclude durable reservations.
    # Hold the worker lock so a deployment cannot race this transient allocation.
    with sqlite3.connect(original.database_path.as_uri() + '?mode=ro', uri=True) as database:
        reserved = {row[0] for row in database.execute('SELECT port FROM reservations')}
    held = []
    for port in range(settings.port_end, settings.port_start - 1, -1):
        if port in reserved:
            continue
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            sock.close()
            continue
        held.append(sock)
        if len(held) == len(services):
            break
    assert len(held) == len(services), 'No free ports for isolated acceptance'
    ports = dict(zip(services, [sock.getsockname()[1] for sock in held]))
    for sock in held:
        sock.close()
    job = {
        'id': identity, 'app_id': identity, 'status': 'succeeded', 'input': {},
        'snapshot': {'repository': 'https://github.com/fixture/network-acceptance',
                     'runtime_network': policy(settings),
                     'manifest': {'schemaVersion': 1, 'app': {'subdomain': 'awnet'},
                                  'services': services, 'routes': []}},
        'execution': {'ports': ports, 'images': {name: image for name in services}},
    }

    def checkpoint(patch):
        job['execution'].update(patch)
        (root / 'job.json').write_text(json.dumps(job, indent=2))

    def inside(script):
        return json.loads(backend._docker(['container', 'exec', backend._container(job, 'web'),
                                          '/usr/local/bin/python3', '-c', script], timeout=60))

    try:
        backend.start(job, checkpoint)
        backend.health(job)
        report['checks']['inbound_health'] = True
        report['checks']['gateway_stream'] = verify_stream(root, ports['web'])
        guard = EgressGuard(settings)
        guard.check(policy(settings))
        report['checks']['guard_verified'] = True
        rows = {name: json.loads(backend._docker(['container', 'inspect', backend._container(job, name)]))[0]
                for name in services}
        isolated = next(iter(rows['isolated']['NetworkSettings']['Networks'].values()))['IPAddress']
        targets = [(host, 443) for host in settings.runtime_blocked_ips]
        targets += [('169.254.169.254', 443), ('10.0.2.2', 443), (isolated, 8080),
                    ('1.1.1.1', 80), ('8.8.8.8', 53)]
        checks = inside('''
import http.client, json, socket, time
checks = {}
t = time.monotonic()
conn = http.client.HTTPSConnection(HOST, timeout=15)
conn.request('HEAD', '/')
response = conn.getresponse()
checks['public_tls'] = {'status': response.status, 'seconds': round(time.monotonic()-t, 3)}
assert 200 <= response.status < 500
conn.close()
conn = http.client.HTTPConnection('api', 8080, timeout=3)
conn.request('GET', '/')
assert conn.getresponse().status == 200
conn.close()
checks['dependency_dns_http'] = True
for resolver in DNS:
 with socket.create_connection((resolver, 53), timeout=5): pass
checks['approved_tcp_dns'] = True
checks['blocked'] = []
for host, port in TARGETS:
 try:
  with socket.create_connection((host, port), timeout=2): pass
 except OSError:
  checks['blocked'].append([host, port])
 else:
  raise AssertionError('Forbidden connection succeeded: %s:%s' % (host, port))
try:
 with socket.create_connection(('2606:4700:4700::1111', 443), timeout=2): pass
except OSError: checks['ipv6_blocked'] = True
else: raise AssertionError('Unexpected IPv6 egress')
print(json.dumps(checks))
'''.replace('HOST', repr(args.https_host)).replace('DNS', repr(list(settings.runtime_dns)))
               .replace('TARGETS', repr(targets)))
        report['checks'].update(checks)
        # Exercise a real restart of only these disposable containers.
        for name in services:
            backend._docker(['container', 'stop', '--time', '3', backend._container(job, name)])
        recovered = backend.recover_active(job, checkpoint)
        report['checks']['recovery'] = recovered
        backend.health(job)
        report['checks']['health_after_recovery'] = True
    finally:
        backend.cleanup(job)
        leftovers = backend._docker(['container', 'ls', '-a', '--filter', 'label=awesome.job=' + identity,
                                     '--format', '{{.ID}}']).strip()
        networks = backend._docker(['network', 'ls', '--filter', 'label=awesome.job=' + identity,
                                    '--format', '{{.ID}}']).strip()
        assert not leftovers and not networks, 'Acceptance resources retained; inspect checkpoint'
        report['checks']['fixture_cleaned'] = True
        (root / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
