import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from awesome_deploy.team_outbox import TeamOutbox
from awesome_deploy.team_server import TeamPlatformClient, create_team_app
from awesome_deploy.team_sessions import SharedSessions
from test_team_integration import MemoryRedis, person, call, payload
import test_team_integration as team_fixtures


team = team_fixtures.team
sessions = team_fixtures.sessions


def adapter(tmp_path):
    sessions = SharedSessions(MemoryRedis(), source='project-a', key=b'k' * 32,
                              app_id='cli_test', tenant_key='tenant')
    class Platform:
        calls = []
        def search(self, body):
            self.calls.append(body)
            return {'items': [{'appId': 'existing-app'}], 'total': 1, 'offset': 0,
                    'limit': 20, 'nextOffset': None}
    platform = Platform()
    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, platform)
    client = TestClient(create_team_app(outbox, 'internal-token', start_worker=False))
    return client, outbox, platform, sessions


HEADERS = {'Authorization': 'Bearer internal-token'}


def test_adapter_search_authenticates_user_and_never_creates_a_request(tmp_path):
    client, outbox, platform, sessions = adapter(tmp_path)
    sid = sessions.issue(person())
    body = {'sessionId': sid, 'query': 'demo'}
    assert client.post('/apps/search', json=body).status_code == 401
    assert client.post('/apps/search', json=body, headers={**HEADERS, 'Origin': 'https://a'}).status_code == 401
    found = client.post('/apps/search', json=body, headers=HEADERS)
    assert found.status_code == 200, found.text
    assert found.json()['items'][0]['appId'] == 'existing-app'
    assert platform.calls == [{**body, 'limit': 20, 'offset': 0}]
    with outbox._db() as db:
        assert db.execute('SELECT count(*) FROM team_requests').fetchone()[0] == 0
    sessions.logout(sid)
    assert client.post('/apps/search', json=body, headers=HEADERS).status_code == 401
    assert len(platform.calls) == 1


@pytest.mark.parametrize('extra', [{'limit': True}, {'limit': 21}, {'offset': -1},
                                  {'query': 'x' * 201}, {'unknown': 'field'}])
def test_adapter_rejects_invalid_search_fields_without_forwarding(tmp_path, extra):
    client, _, platform, sessions = adapter(tmp_path)
    response = client.post('/apps/search', json={'sessionId': sessions.issue(person()), **extra}, headers=HEADERS)
    assert response.status_code == 422
    assert platform.calls == []


def test_search_transport_uses_post_body_and_protected_backend_credential(tmp_path):
    token = tmp_path / 'source-token'
    token.write_text('source-credential')
    token.chmod(0o600)
    received = []
    def handle(request):
        received.append(request)
        return httpx.Response(200, json={'items': [], 'total': 0, 'limit': 20,
                                        'offset': 0, 'nextOffset': None})
    client = TeamPlatformClient('http://127.0.0.1:8088', token, host='awesome.test',
                              client=httpx.Client(transport=httpx.MockTransport(handle)))
    body = {'sessionId': 'private-session', 'query': 'demo', 'limit': 20, 'offset': 0}
    assert client.search(body)['items'] == []
    request = received[0]
    assert request.method == 'POST'
    assert str(request.url) == 'http://127.0.0.1:8088/api/v1/integrations/apps/search'
    assert request.headers['Authorization'] == 'Bearer source-credential'
    assert request.headers['Host'] == 'awesome.test'
    assert json.loads(request.content) == body


@pytest.mark.parametrize('upstream,expected', [(401, 401), (403, 403), (422, 422), (500, 503)])
def test_adapter_preserves_actionable_errors_without_upstream_details(tmp_path, upstream, expected):
    client, outbox, _, sessions = adapter(tmp_path)
    token = tmp_path / 'source-token'
    token.write_text('source-credential')
    token.chmod(0o600)
    outbox.platform = TeamPlatformClient('http://127.0.0.1:8088', token,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(upstream, json={'detail': 'private-upstream-detail'}))))
    response = client.post('/apps/search', json={'sessionId': sessions.issue(person())}, headers=HEADERS)
    assert response.status_code == expected
    assert 'private-upstream-detail' not in response.text
    assert 'source-credential' not in response.text


@pytest.mark.parametrize('mode', ['missing', 'permissions'])
def test_search_credential_maintenance_returns_safe_service_unavailable(tmp_path, mode):
    client, outbox, _, sessions = adapter(tmp_path)
    token = tmp_path / 'source-token'
    if mode == 'permissions':
        token.write_text('private-token')
        token.chmod(0o644)
    outbox.platform = TeamPlatformClient('http://127.0.0.1:8088', token)
    try:
        response = client.post('/apps/search', json={'sessionId': sessions.issue(person())}, headers=HEADERS)
        assert response.status_code == 503
        assert 'private-token' not in response.text and str(token) not in response.text
    finally:
        outbox.platform.close()


def test_non_ascii_internal_bearer_is_rejected_as_unauthorized(tmp_path):
    client, _, platform, _ = adapter(tmp_path)
    response = client.post('/apps/search', json={}, headers=[(b'authorization', b'Bearer \xff')])
    assert response.status_code == 401
    assert platform.calls == []


def test_search_update_and_poll_progress_through_adapter(tmp_path, team, sessions):
    b, platform_app, credential, _ = team
    sid = sessions.issue(person())
    first = call(b, credential['token'], {'deployment': payload('flow'), 'sessionId': sid}, 'initial').json()
    with platform_app.state.store._connection() as db:
        db.execute("UPDATE jobs SET status='failed',phase='source' WHERE id=?", (first['id'],))
    source_headers = {'Authorization': 'Bearer ' + credential['token']}

    class Bridge:
        def search(self, body):
            response = b.post('/api/v1/integrations/apps/search', json=body, headers=source_headers)
            assert response.status_code == 200
            return response.json()
        def post(self, key, body):
            response = call(b, credential['token'], body, key)
            assert response.status_code == 202, response.text
            return response.json()
        def get(self, job_id):
            response = b.get('/api/v1/integrations/deployments/' + job_id, headers=source_headers)
            assert response.status_code == 200
            return response.json()

    now = [time.time()]
    outbox = TeamOutbox(tmp_path / 'chain' / 'db', sessions, Bridge(), clock=lambda: now[0])
    with TestClient(create_team_app(outbox, 'internal-token', start_worker=False)) as a:
        found = a.post('/apps/search', json={'sessionId': sid, 'query': 'flow'}, headers=HEADERS).json()
        selected = found['items'][0]
        assert selected['appId'] == first['appId']
        accepted = a.post('/requests', json={'sessionId': sid, 'deployment': {
            'appId': selected['appId'], 'repository': selected['repository'], 'subdomain': 'flow'}},
            headers={**HEADERS, 'Idempotency-Key': 'update-flow'})
        assert accepted.status_code == 202 and accepted.json()['status'] == 'pending'
        outbox.run_once()
        view = a.get('/requests/update-flow', headers=HEADERS).json()
        assert view['appId'] == first['appId'] and view['jobId'] != first['id']
        assert view['deployment']['status'] == 'queued' and view['deployment']['ref'] == 'main'
        with platform_app.state.store._connection() as db:
            db.execute("UPDATE jobs SET status='running',phase='build' WHERE id=?", (view['jobId'],))
        now[0] += 6
        outbox.run_once()
        progress = a.get('/requests/update-flow', headers=HEADERS).json()
        assert progress['deployment']['phaseLabel'] == '构建镜像'
        assert progress['deployment']['terminal'] is False and progress['pollAfterSeconds'] == 5
        with platform_app.state.store._connection() as db:
            db.execute("UPDATE jobs SET status='succeeded',phase='complete' WHERE id=?", (view['jobId'],))
            db.execute('UPDATE apps SET active_release_id=?,subdomain=? WHERE id=?',
                       (view['jobId'], 'flow', first['appId']))
        now[0] += 6
        outbox.run_once()
        final = a.get('/requests/update-flow', headers=HEADERS).json()
        assert final['status'] == 'succeeded' and final['pollAfterSeconds'] == 0
        assert final['deployment']['terminal'] is True and final['deployment']['active'] is True
        assert final['deployment']['url'].startswith('https://flow.')
