from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from awesome_deploy.intake_relay import IntakeRelay
from awesome_deploy.intake_server import create_app, load_relay


@pytest.mark.parametrize('field', ['database_path', 'platform_token_file', 'intake_token_file',
                                  'integrity_key_file', 'feishu_app_secret_file'])
@pytest.mark.parametrize('value', [None, 123, '', 'relative', '/tmp/SENSITIVE_VALUE\x00',
                                  '/tmp/SENSITIVE_VALUE\n', '/tmp/SENSITIVE_VALUE\u200b'])
def test_relay_rejects_invalid_paths_before_reading_credentials(tmp_path, monkeypatch, field, value):
    config = dict(database_path='/tmp/database.sqlite3', platform_origin='https://deploy.example.test',
                  platform_token_file='/tmp/platform-token', intake_token_file='/tmp/intake-token',
                  integrity_key_file='/tmp/integrity-key', feishu_app_id='cli_test',
                  feishu_app_secret_file='/tmp/app-secret', base_token='base1234567890123',
                  intake_table_id='tbl12345678')
    config[field] = value
    path = tmp_path / 'intake.json'
    path.write_text(json.dumps(config))

    def unexpected_secret_read(_path):
        raise AssertionError('invalid paths must be rejected before reading credentials')

    monkeypatch.setattr('awesome_deploy.intake_server._read_secret', unexpected_secret_read)
    with pytest.raises(ValueError) as error:
        load_relay(path)
    assert field in str(error.value)
    assert 'SENSITIVE_VALUE' not in str(error.value)


class Platform:
    def post(self, request_id, body):
        return {'id': 'job-1', 'appId': 'app-1', 'status': 'queued'}

    def get(self, job_id):
        return {'id': job_id, 'appId': 'app-1', 'status': 'succeeded'}


class Base:
    def upsert(self, request_id, fields):
        pass

    def list_records(self):
        return []


def test_private_intake_http_acknowledges_only_committed_request(tmp_path):
    relay = IntakeRelay(tmp_path / 'outbox.sqlite3', Platform(), Base(),
                        integrity_key=b'integrity-key-for-tests')
    app = create_app(relay, 'private-intake-token', start_worker=False)
    body = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': 'demo'}
    headers = {'Authorization': 'Bearer private-intake-token', 'Idempotency-Key': 'source-1'}
    with TestClient(app) as client:
        assert client.post('/requests', json=body, headers={'Idempotency-Key': 'source-1'}).status_code == 401
        accepted = client.post('/requests', json=body, headers=headers)
        assert accepted.status_code == 202 and accepted.json()['requestId'] == 'source-1'
        assert client.post('/requests', json=body, headers=headers).json() == accepted.json()
        assert client.get('/requests/source-1', headers=headers).json()['status'] == 'pending'
        assert client.post('/requests', json={**body, 'subdomain': 'other'}, headers=headers).status_code == 409
        assert client.post('/requests', content=b'x' * 9000, headers=headers).status_code == 413


@pytest.mark.parametrize('method,path', [('POST', '/requests'), ('GET', '/requests/source-1')])
@pytest.mark.parametrize('headers', [
    [(b'authorization', b'Bearer \xff')],
    [('Authorization', 'Bearer private-intake-token'), ('Authorization', 'Bearer private-intake-token')],
    [('Authorization', 'Bearer private-intake-token'), ('Authorization', 'Bearer incorrect')],
    [('Authorization', 'Bearer incorrect'), ('Authorization', 'Bearer private-intake-token')],
    [('Authorization', 'Bearer private-intake-token'), ('Origin', 'http://testserver')],
    [('Authorization', 'Bearer private-intake-token'), ('Sec-Fetch-Site', 'same-origin')],
])
def test_private_intake_rejects_ambiguous_or_browser_auth_before_relay(tmp_path, method, path, headers):
    relay = IntakeRelay(tmp_path / 'outbox.sqlite3', Platform(), Base(),
                        integrity_key=b'integrity-key-for-tests')
    app = create_app(relay, 'private-intake-token', start_worker=False)
    body = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': 'demo'}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method, path, json=body,
                                  headers=[*headers, ('Idempotency-Key', 'source-1')])
    assert response.status_code == 401
    assert relay.get('source-1') is None


def test_private_intake_status_remains_available_while_accept_syncs_base(tmp_path):
    entered, release = Event(), Event()

    class BlockingBase(Base):
        def upsert(self, request_id, fields):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError('test did not release Base synchronization')

    relay = IntakeRelay(tmp_path / 'outbox.sqlite3', Platform(), BlockingBase(),
                        integrity_key=b'integrity-key-for-tests')
    app = create_app(relay, 'private-intake-token', start_worker=False)
    body = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': 'demo'}
    headers = {'Authorization': 'Bearer private-intake-token', 'Idempotency-Key': 'source-1'}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as callers:
        submitted = callers.submit(client.post, '/requests', json=body, headers=headers)
        try:
            assert entered.wait(timeout=2), 'request did not reach Base synchronization'
            polled = callers.submit(client.get, '/requests/source-1', headers=headers).result(timeout=2)
            assert polled.status_code == 200
            assert polled.json()['status'] == 'pending'
            assert not submitted.done(), 'submission must still be waiting on Base'
        finally:
            release.set()
        assert submitted.result(timeout=2).status_code == 202
