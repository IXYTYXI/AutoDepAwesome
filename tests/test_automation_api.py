import sqlite3

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.api_credentials import ApiCredentialStore
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from test_feishu_api import FakeProvider, login


@pytest.fixture(params=['continuous', 'login'])
def web(tmp_path, request):
    settings = Settings(state_dir=tmp_path, public_url='https://testserver', auth_mode='feishu',
                        feishu_app_id='cli_test', feishu_tenant_key='tenant',
                        feishu_app_secret_file=tmp_path / 'key', feishu_eligibility_mode=request.param)
    provider = FakeProvider()
    if request.param == 'login':
        provider.check_eligible = lambda open_id: pytest.fail('login mode must not poll Feishu')
    app = create_app(settings, identity_provider=provider)
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as client:
        yield client, app, provider


def issue(app, user_id, *, app_id=None, allow_create=False, name='ci'):
    return ApiCredentialStore(app.state.store.settings).issue(
        user_id, name, kind='source', app_id=app_id, allow_create=allow_create)['token']


def body(*, app_id=None, subdomain='demo'):
    data = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': subdomain}
    if app_id:
        data['appId'] = app_id
    return data


def headers(token, key='first'):
    return {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'Idempotency-Key': key}


def test_source_credential_submits_and_reads_only_its_jobs(web):
    client, app, _ = web
    user_id = login(client)['userId']
    token = issue(app, user_id, allow_create=True)
    submitted = client.post('/api/v1/automation/deployments', json=body(), headers=headers(token))
    assert submitted.status_code == 202
    job = submitted.json()
    assert job['status'] == 'queued' and job['appId']
    retry = client.post('/api/v1/automation/deployments', json=body(), headers=headers(token))
    assert retry.status_code == 202 and retry.json()['id'] == job['id']
    assert client.get('/api/v1/automation/deployments/' + job['id'],
                      headers={'Authorization': 'Bearer ' + token}).status_code == 200
    other = issue(app, user_id, allow_create=True, name='other')
    assert client.get('/api/v1/automation/deployments/' + job['id'],
                      headers={'Authorization': 'Bearer ' + other}).status_code == 403
    independent = client.post('/api/v1/automation/deployments', json=body(subdomain='other'),
                              headers=headers(other))
    assert independent.status_code == 202 and independent.json()['id'] != job['id']


def test_browser_cookie_and_basic_cannot_enter_machine_routes(web):
    client, app, _ = web
    session = login(client)
    assert client.post('/api/v1/automation/deployments', json=body(),
                       headers={'Idempotency-Key': 'one'}).status_code == 401
    assert client.post('/api/v1/automation/deployments', json=body(),
                       auth=('operator', 'password'), headers={'Idempotency-Key': 'one'}).status_code == 401
    token = issue(app, session['userId'], allow_create=True)
    with TestClient(app, base_url='https://testserver') as anonymous:
        assert anonymous.get('/api/v1/apps', headers={'Authorization': 'Bearer ' + token}).status_code == 401
        assert anonymous.post('/api/v1/deployments', json=body(), headers=headers(token)).status_code == 401


def test_scoped_credential_cannot_create_or_deploy_other_application(web):
    client, app, _ = web
    session = login(client)
    browser = {'Origin': 'https://testserver', 'X-CSRF-Token': session['csrfToken'],
               'Idempotency-Key': 'browser', 'Content-Type': 'application/json'}
    existing = client.post('/api/v1/deployments', json=body(), headers=browser).json()
    token = issue(app, session['userId'], app_id=existing['appId'])
    assert client.post('/api/v1/automation/deployments', json=body(), headers=headers(token)).status_code == 403
    updated = client.post('/api/v1/automation/deployments', json=body(app_id=existing['appId']),
                          headers=headers(token))
    assert updated.status_code == 202
    assert client.post('/api/v1/automation/deployments', json=body(app_id='other'),
                       headers=headers(token, 'other')).status_code == 403


def test_revocation_or_user_disable_prevents_new_calls_and_queued_execution(web):
    client, app, _ = web
    session = login(client)
    credentials = ApiCredentialStore(app.state.store.settings)
    issued = credentials.issue(session['userId'], 'ci', kind='source', allow_create=True)
    job = client.post('/api/v1/automation/deployments', json=body(),
                      headers=headers(issued['token'])).json()
    actor, credential = credentials.authenticate(issued['token'])
    credentials.revoke(issued['id'])
    from awesome_deploy.access import AccessStore
    with pytest.raises(DeployError) as late_read:
        AccessStore(app.state.store.settings).require_credential_job(
            actor, credential['id'], app.state.store.get(job['id']))
    assert late_read.value.code == 'ACCESS_DENIED'
    assert client.post('/api/v1/automation/deployments', json=body(),
                       headers=headers(issued['token'], 'second')).status_code == 401
    with pytest.raises(DeployError) as error:
        AccessStore(app.state.store.settings).authorize_operation_local('job', job['id'])
    assert error.value.code == 'ACCESS_DENIED'
    second = credentials.issue(session['userId'], 'other', kind='person', allow_create=True)
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        db.execute('UPDATE auth_users SET enabled=0,revision=revision+1 WHERE id=?', (session['userId'],))
    assert client.post('/api/v1/automation/deployments', json=body(),
                       headers=headers(second['token'], 'third')).status_code == 401


def test_member_self_service_token_creation_and_source_separation(web):
    client, app, _ = web
    session = login(client)
    assert client.get('/api-access').status_code == 200
    assert client.get('/assets/api-credentials.js').status_code == 200
    csrf = {'Origin': 'https://testserver', 'Content-Type': 'application/json',
            'X-CSRF-Token': session['csrfToken']}
    assert client.post('/api/v1/api-credentials', json={'name': 'my-ci', 'allowAnyApp': True},
                       headers={'Origin': 'https://testserver'}).status_code == 403
    response = client.post('/api/v1/api-credentials', json={'name': 'my-ci', 'allowAnyApp': True},
                           headers=csrf)
    assert response.status_code == 201
    issued = response.json()
    assert issued['kind'] == 'person' and issued['token'].startswith('adp_')
    listing = client.get('/api/v1/api-credentials').json()['items']
    assert [row['id'] for row in listing] == [issued['id']]
    assert 'token' not in listing[0]
    source = ApiCredentialStore(app.state.store.settings).issue(
        session['userId'], 'source', kind='source', allow_any_app=True)
    designated_person = ApiCredentialStore(app.state.store.settings).issue(
        session['userId'], 'designated-person', kind='person', allow_any_app=True)
    assert [row['id'] for row in client.get('/api/v1/api-credentials').json()['items']] == [issued['id']]
    assert client.delete('/api/v1/api-credentials/' + source['id'], headers=csrf).status_code == 403
    assert client.delete('/api/v1/api-credentials/' + designated_person['id'], headers=csrf).status_code == 403
    new_job = client.post('/api/v1/automation/deployments', json=body(), headers=headers(issued['token']))
    assert new_job.status_code == 202
    app_id = new_job.json()['appId']
    update = client.post('/api/v1/automation/deployments', json=body(app_id=app_id),
                         headers=headers(issued['token'], 'update'))
    assert update.status_code == 202
    assert client.delete('/api/v1/api-credentials/' + issued['id'], headers=csrf).status_code == 200
    assert client.post('/api/v1/automation/deployments', json=body(app_id=app_id),
                       headers=headers(issued['token'], 'later')).status_code == 401


def test_other_member_cannot_list_or_revoke_personal_credential(web):
    client, app, provider = web
    owner = login(client)
    issued = ApiCredentialStore(app.state.store.settings).issue(
        owner['userId'], 'owner-only', kind='person', allow_any_app=True)
    provider.person = 'ou_other'
    other = login(client)
    assert other['userId'] != owner['userId']
    assert client.get('/api/v1/api-credentials').json()['items'] == []
    csrf = {'Origin': 'https://testserver', 'Content-Type': 'application/json',
            'X-CSRF-Token': other['csrfToken']}
    assert client.delete('/api/v1/api-credentials/' + issued['id'], headers=csrf).status_code == 403
    assert ApiCredentialStore(app.state.store.settings).authenticate(issued['token']) is not None


def test_designated_source_can_submit_multiple_applications_and_updates(web):
    client, app, _ = web
    session = login(client)
    token = ApiCredentialStore(app.state.store.settings).issue(
        session['userId'], 'trusted-build-source', kind='source', allow_any_app=True)['token']
    first = client.post('/api/v1/automation/deployments', json=body(subdomain='one'),
                        headers=headers(token, 'one'))
    second = client.post('/api/v1/automation/deployments', json=body(subdomain='two'),
                         headers=headers(token, 'two'))
    assert first.status_code == second.status_code == 202
    assert first.json()['appId'] != second.json()['appId']
    updated = client.post('/api/v1/automation/deployments',
                          json=body(app_id=first.json()['appId'], subdomain='one'),
                          headers=headers(token, 'one-update'))
    assert updated.status_code == 202
    assert updated.json()['appId'] == first.json()['appId']


@pytest.mark.parametrize('is_admin', [False, True])
def test_delegation_source_cannot_submit_as_owner_without_applicant(web, is_admin):
    client, app, _ = web
    owner = login(client)['userId']
    if is_admin:
        app.state.identity.set_user(owner, is_admin=True)
    issued = ApiCredentialStore(app.state.store.settings).issue(
        owner, 'team-source', kind='source', allow_any_app=True, delegation_source='project-a')
    response = client.post('/api/v1/automation/deployments', json=body(), headers=headers(issued['token']))
    assert response.status_code == 403
    with app.state.store._connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_delegation_source_cannot_read_legacy_owner_job_via_automation(web):
    client, app, _ = web
    owner = login(client)['userId']
    credentials = ApiCredentialStore(app.state.store.settings)
    issued = credentials.issue(owner, 'team-source', kind='source', allow_any_app=True)
    job = client.post('/api/v1/automation/deployments', json=body(), headers=headers(issued['token'])).json()
    # Simulate an owner-bound job admitted before delegated sources were isolated.
    with credentials._connection(write=True) as db:
        db.execute('UPDATE api_credentials SET delegation_source=? WHERE id=?', ('project-a', issued['id']))
    response = client.get('/api/v1/automation/deployments/' + job['id'], headers=headers(issued['token']))
    assert response.status_code == 403
