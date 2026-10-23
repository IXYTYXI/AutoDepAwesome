from fastapi.testclient import TestClient

from awesome_deploy.intake_relay import IntakeRelay
from awesome_deploy.intake_server import create_app


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
