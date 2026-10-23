import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.api_credentials import ApiCredentialStore
from awesome_deploy.config import Settings
from awesome_deploy.team_sessions import SharedSessions, SessionError
from awesome_deploy.team_outbox import TeamOutbox
from test_feishu_api import FakeProvider, login


class MemoryRedis:
    def __init__(self):
        self.values = {}
    def set(self, key, value, **kwargs):
        self.values[key] = value
        return True
    def get(self, key):
        return self.values.get(key)
    def delete(self, key):
        self.values.pop(key, None)


@pytest.fixture
def sessions():
    return SharedSessions(MemoryRedis(), source='project-a', key=b'k' * 32,
                          app_id='cli_test', tenant_key='tenant')


def person(open_id='ou_applicant'):
    return {'openId': open_id, 'name': '申请人', 'appId': 'cli_test', 'tenantKey': 'tenant'}


def payload(subdomain='demo'):
    return {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': subdomain}


def test_session_expiry_tamper_and_receipt_survives_logout(sessions):
    sid = sessions.issue(person(), ttl=60)
    assert sessions.read(sid)['openId'] == 'ou_applicant'
    receipt = sessions.admit(sid, 'req1', payload())
    sessions.logout(sid)
    with pytest.raises(SessionError):
        sessions.read(sid)
    assert sessions.verify_receipt(receipt, 'req1', payload())['openId'] == 'ou_applicant'
    with pytest.raises(SessionError):
        sessions.verify_receipt(receipt, 'req1', payload('changed'))
    receipt['identity']['openId'] = 'ou_admin'
    with pytest.raises(SessionError):
        sessions.verify_receipt(receipt, 'req1', payload())
    sid = sessions.issue(person(), ttl=1)
    sessions.clock = lambda: time.time() + 2
    with pytest.raises(SessionError):
        sessions.read(sid)


@pytest.fixture
def team(tmp_path, sessions):
    settings = Settings(state_dir=tmp_path / 'platform', public_url='https://testserver', auth_mode='feishu',
                        feishu_app_id='cli_test', feishu_tenant_key='tenant',
                        feishu_app_secret_file=tmp_path / 'secret', feishu_eligibility_mode='login')
    app = create_app(settings, identity_provider=FakeProvider())
    app.state.shared_sessions = {'project-a': sessions}
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as client:
        owner = login(client)['userId']
        store = ApiCredentialStore(settings)
        credential = store.issue(owner, 'project-a', kind='source', allow_any_app=True,
                                 delegation_source='project-a')
        yield client, app, credential, store


def call(client, token, body, key='req1'):
    return client.post('/api/v1/integrations/deployments', json=body,
                       headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': key})


def test_delegated_owner_idempotency_rotation_and_revocation(team, sessions):
    client, app, credential, credentials = team
    sid = sessions.issue(person())
    body = {'deployment': payload(), 'receipt': sessions.admit(sid, 'req1', payload())}
    sessions.logout(sid)
    accepted = call(client, credential['token'], body)
    assert accepted.status_code == 202, accepted.text
    job = accepted.json()
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        owner = db.execute('SELECT owner_id FROM app_access WHERE app_id=?', (job['appId'],)).fetchone()[0]
        assert owner != credential['ownerUserId']
        assert db.execute('SELECT open_id FROM auth_users WHERE id=?', (owner,)).fetchone()[0] == 'ou_applicant'
    assert call(client, credential['token'], body).json()['id'] == job['id']
    rotated = credentials.issue(credential['ownerUserId'], 'project-a-rotated', kind='source',
                                allow_any_app=True, delegation_source='project-a')
    assert call(client, rotated['token'], body).json()['id'] == job['id']
    assert client.get('/api/v1/integrations/deployments/' + job['id'],
                      headers={'Authorization': 'Bearer ' + rotated['token']}).status_code == 200
    from awesome_deploy.access import AccessStore
    assert AccessStore(app.state.store.settings).authorize_operation_local('job', job['id'])['id'] == owner
    credentials.revoke(credential['id'])
    assert call(client, credential['token'], body).status_code == 401


def test_direct_session_required_scope_and_tamper(team, sessions):
    client, app, credential, credentials = team
    sid = sessions.issue(person())
    body = {'deployment': payload(), 'sessionId': sid}
    ordinary = credentials.issue(credential['ownerUserId'], 'normal', kind='source', allow_any_app=True)
    assert call(client, ordinary['token'], body).status_code == 403
    assert call(client, credential['token'], body).status_code == 202
    sessions.logout(sid)
    assert call(client, credential['token'], body, 'new').status_code == 401
    receipt = sessions.admit(sessions.issue(person()), 'bad', payload())
    receipt['identity']['name'] = '改名'
    assert call(client, credential['token'], {'deployment': payload(), 'receipt': receipt}, 'bad').status_code == 401


def test_deployment_apis_default_omitted_ref_and_preserve_idempotency(team, sessions):
    client, _, credential, _ = team
    omitted = {key: value for key, value in payload().items() if key != 'ref'}
    headers = {'Authorization': 'Bearer ' + credential['token'], 'Idempotency-Key': 'automation-main'}
    accepted = client.post('/api/v1/automation/deployments', json=omitted, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['ref'] == 'main'
    repeated = client.post('/api/v1/automation/deployments', json=payload(), headers=headers)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()['id'] == accepted.json()['id']

    sid = sessions.issue(person())
    omitted['subdomain'] = 'team'
    accepted = call(client, credential['token'], {'deployment': omitted, 'sessionId': sid}, 'team-main')
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['ref'] == 'main'
    repeated = call(client, credential['token'], {'deployment': payload('team'), 'sessionId': sid}, 'team-main')
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()['id'] == accepted.json()['id']


def test_outbox_defaults_ref_before_persistence_signature_and_retry(tmp_path, sessions, team):
    client, _, credential, _ = team
    class Transport:
        def post(self, request_id, body):
            assert body['deployment']['ref'] == 'main'
            assert sessions.verify_receipt(body['receipt'], request_id, body['deployment']) == person()
            result = call(client, credential['token'], body, request_id)
            assert result.status_code == 202, result.text
            assert result.json()['ref'] == 'main'
            return result.json()

    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, Transport())
    sid = sessions.issue(person())
    omitted = {key: value for key, value in payload().items() if key != 'ref'}
    assert outbox.accept('default-main', omitted, sid)['status'] == 'pending'
    assert 'ref' not in omitted  # Do not change the caller's object.
    assert outbox.accept('default-main', payload(), sid)['status'] == 'pending'
    with pytest.raises(SessionError):
        outbox.accept('default-main', {**payload(), 'ref': 'release-v2'}, sid)
    sessions.logout(sid)
    assert outbox.run_once()
    assert outbox.get('default-main')['status'] == 'submitted'


def test_outbox_response_loss_and_restart(tmp_path, sessions, team):
    client, app, credential, credentials = team
    calls = []
    class Transport:
        def post(self, request_id, body):
            result = call(client, credential['token'], body, request_id)
            assert result.status_code == 202, result.text
            calls.append(result.json()['id'])
            if len(calls) == 1:
                raise ConnectionError('lost response')
            return result.json()
        def get(self, job_id):
            return {'id': job_id, 'status': 'succeeded', 'appId': 'app'}
    path = tmp_path / 'a' / 'requests.sqlite3'
    outbox = TeamOutbox(path, sessions, Transport())
    sid = sessions.issue(person())
    assert outbox.accept('req1', payload(), sid)['status'] == 'pending'
    sessions.logout(sid)
    outbox.run_once()
    assert outbox.get('req1')['status'] == 'pending'
    restarted = TeamOutbox(path, sessions, Transport(), clock=lambda: time.time() + 4000)
    restarted.run_once()
    assert restarted.get('req1')['jobId'] == calls[0] == calls[1]
    restarted.clock = lambda: time.time() + 4010
    restarted.run_once()
    assert restarted.get('req1')['status'] == 'succeeded'
    assert sid not in path.read_bytes().decode(errors='ignore')


def test_source_and_user_isolation(team, sessions):
    client, app, cred, credentials = team
    one = sessions.issue(person())
    accepted = call(client, cred['token'], {'deployment': payload(), 'sessionId': one}).json()
    two = sessions.issue(person('ou_other'))
    assert call(client, cred['token'], {'deployment': payload(), 'sessionId': two}).status_code == 403
    update = {**payload(), 'appId': accepted['appId']}
    assert call(client, cred['token'], {'deployment': update, 'sessionId': two}, 'update').status_code == 403
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        user_id = db.execute("SELECT id FROM auth_users WHERE open_id='ou_applicant'").fetchone()[0]
    app.state.identity.set_user(user_id, enabled=False)
    assert call(client, cred['token'], {'deployment': payload('second'), 'sessionId': one}, 'second').status_code == 403


def test_outbox_never_accepts_with_unavailable_redis(tmp_path, sessions):
    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, None)
    sid = sessions.issue(person())
    def failed(key):
        raise ConnectionError('Redis down')
    sessions.redis.get = failed
    from awesome_deploy.team_sessions import SessionUnavailable
    with pytest.raises(SessionUnavailable):
        outbox.accept('req1', payload(), sid)
    assert outbox.get('req1') is None


def test_team_http_intake_and_transport(tmp_path, sessions):
    from awesome_deploy.team_server import create_team_app
    class Platform:
        def post(self, request_id, body):
            return {'id': 'job', 'appId': 'app'}
    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, Platform())
    app = create_team_app(outbox, 'intake-secret', start_worker=False)
    with TestClient(app) as client:
        sid = sessions.issue(person())
        data = {'deployment': payload(), 'sessionId': sid}
        assert client.post('/requests', json=data).status_code == 401
        h = {'Authorization': 'Bearer intake-secret', 'Idempotency-Key': 'req1'}
        assert client.post('/requests', json=data, headers=h).status_code == 202
        assert client.get('/requests/req1', headers=h).json()['status'] == 'pending'
        assert client.post('/requests', json={**data, 'deployment': payload('changed')}, headers=h).status_code == 409


def test_backend_session_lifecycle(tmp_path, sessions):
    from awesome_deploy.team_server import create_team_app
    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, None)
    with TestClient(create_team_app(outbox, 'secret', start_worker=False)) as client:
        h = {'Authorization': 'Bearer secret'}
        assert client.post('/sessions', json={'identity': person()}).status_code == 401
        response = client.post('/sessions', json={'identity': person()}, headers=h)
        assert response.status_code == 201
        sid = response.json()['sessionId']
        assert sessions.read(sid)['openId'] == person()['openId']
        assert client.post('/sessions/revoke', json={'sessionId': sid}, headers=h).status_code == 200
        with pytest.raises(SessionError):
            sessions.read(sid)
