"""Progress contracts remain useful without exposing internal execution details."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.api_credentials import ApiCredentialStore
from awesome_deploy.config import Settings
from awesome_deploy.team_outbox import TeamOutbox
from awesome_deploy.team_sessions import SharedSessions
from test_feishu_api import FakeProvider, login
from test_team_integration import MemoryRedis, payload, person


@pytest.fixture
def progress_api(tmp_path):
    settings = Settings(state_dir=tmp_path / 'platform', public_url='https://testserver',
                        auth_mode='feishu', feishu_app_id='cli_test', feishu_tenant_key='tenant',
                        feishu_app_secret_file=tmp_path / 'secret', feishu_eligibility_mode='login')
    app = create_app(settings, identity_provider=FakeProvider())
    sessions = SharedSessions(MemoryRedis(), source='project-a', key=b'k' * 32,
                              app_id='cli_test', tenant_key='tenant')
    app.state.shared_sessions = {'project-a': sessions}
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as client:
        user_id = login(client)['userId']
        credential = ApiCredentialStore(settings).issue(
            user_id, 'project-a', kind='source', allow_any_app=True, delegation_source='project-a')
        yield client, app, sessions, credential


def accepted_job(progress_api, endpoint):
    client, _, sessions, credential = progress_api
    body = payload()
    if endpoint == 'integrations':
        body = {'deployment': body, 'sessionId': sessions.issue(person())}
    headers = {'Authorization': 'Bearer ' + credential['token'], 'Idempotency-Key': 'progress'}
    response = client.post(f'/api/v1/{endpoint}/deployments', json=body, headers=headers)
    assert response.status_code == 202, response.text
    return response.json(), headers


@pytest.mark.parametrize('endpoint', ['automation', 'integrations'])
@pytest.mark.parametrize(('status', 'phase', 'label', 'phase_label', 'terminal'), [
    ('queued', 'queued', '已排队', '排队', False),
    ('running', 'build', '执行中', '构建镜像', False),
    ('succeeded', 'complete', '部署成功', '完成', True),
    ('failed', 'health', '部署失败', '健康检查', True),
    ('recovery_required', 'recovery', '需要处理恢复状态', '恢复运行状态', True),
    ('cancelled', 'cleanup', '已取消', '清理资源', True),
])
def test_machine_queries_return_status_phase_and_polling_contract(
        progress_api, endpoint, status, phase, label, phase_label, terminal):
    client, app, _, _ = progress_api
    job, headers = accepted_job(progress_api, endpoint)
    assert job['statusLabel'] == '已排队'
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        db.execute('UPDATE jobs SET status=?,phase=?,updated_at=? WHERE id=?',
                   (status, phase, 1234567890.5, job['id']))
    response = client.get(f'/api/v1/{endpoint}/deployments/{job["id"]}', headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == status and result['statusLabel'] == label
    assert result['phase'] == phase and result['phaseLabel'] == phase_label
    assert result['terminal'] is terminal
    assert result['pollAfterSeconds'] == (0 if terminal else 5)
    assert result['updatedAt'] == 1234567890.5
    assert result['id'] == job['id'] and result['appId'] == job['appId']
    assert result['createdAt'] == job['createdAt']
    assert result['url'] is None and result['active'] is False
    assert 'percent' not in result and 'eta' not in result


@pytest.mark.parametrize('endpoint', ['automation', 'integrations'])
def test_progress_has_safe_error_and_unknown_phase_fallback(progress_api, endpoint):
    client, app, _, _ = progress_api
    job, headers = accepted_job(progress_api, endpoint)
    raw_secret = 'secret-in-subprocess-output'
    error = {'code': 'COMMAND_FAILED', 'message': raw_secret, 'suggestion': raw_secret}
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        db.execute('UPDATE jobs SET status=?,phase=?,error=? WHERE id=?',
                   ('failed', 'new_future_phase', json.dumps(error), job['id']))
    response = client.get(f'/api/v1/{endpoint}/deployments/{job["id"]}', headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result['phaseLabel'] == '处理中'
    assert result['error']['code'] == 'COMMAND_FAILED'
    assert raw_secret not in response.text
    assert result['terminal'] is True and result['pollAfterSeconds'] == 0


def test_success_url_only_belongs_to_current_release(progress_api):
    client, app, _, _ = progress_api
    job, headers = accepted_job(progress_api, 'automation')
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        db.execute("UPDATE jobs SET status='succeeded',phase='complete' WHERE id=?", (job['id'],))
        db.execute('UPDATE apps SET active_release_id=? WHERE id=?', (job['id'], job['appId']))
    path = f'/api/v1/automation/deployments/{job["id"]}'
    active = client.get(path, headers=headers).json()
    assert active['active'] is True and active['url'].startswith('https://demo.')
    with sqlite3.connect(app.state.store.settings.database_path) as db:
        db.execute('UPDATE apps SET active_release_id=NULL WHERE id=?', (job['appId'],))
    historical = client.get(path, headers=headers).json()
    assert historical['status'] == 'succeeded' and historical['terminal'] is True
    assert historical['active'] is False and historical['url'] is None


def test_outbox_exposes_record_times_and_retains_last_snapshot_on_connection_failure(tmp_path):
    now = [time.time()]
    sessions = SharedSessions(MemoryRedis(), source='project-a', key=b'k' * 32,
                              app_id='cli_test', tenant_key='tenant', clock=lambda: now[0])
    snapshot = {'id': 'job', 'appId': 'app', 'status': 'running', 'phase': 'build',
                'phaseLabel': '构建镜像', 'terminal': False, 'updatedAt': now[0] - 20,
                'pollAfterSeconds': 5}

    class Transport:
        def post(self, request_id, body):
            return snapshot

        def get(self, job_id):
            raise ConnectionError('credentials-must-not-leak')

    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, Transport(), clock=lambda: now[0])
    request = outbox.accept('req1', payload(), sessions.issue(person()))
    assert request['createdAt'] == request['updatedAt'] == now[0]
    assert request['pollAfterSeconds'] == 5 and request['deployment'] is None
    outbox.run_once()
    now[0] += 10
    outbox.run_once()
    cached = outbox.get('req1')
    assert cached['status'] == 'submitted' and cached['pollAfterSeconds'] == 5
    assert cached['createdAt'] == request['createdAt'] and cached['updatedAt'] == now[0]
    assert cached['deployment'] == snapshot
    assert cached['deployment']['updatedAt'] < cached['updatedAt']
    assert cached['error'] and 'credentials-must-not-leak' not in json.dumps(cached)


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'recovery_required', 'cancelled', 'rejected'])
def test_outbox_terminal_results_stop_polling(tmp_path, status):
    sessions = SharedSessions(MemoryRedis(), source='project-a', key=b'k' * 32,
                              app_id='cli_test', tenant_key='tenant')
    outbox = TeamOutbox(tmp_path / 'a' / 'db', sessions, None)
    outbox.accept('req1', payload(), sessions.issue(person()))
    with outbox._db() as db:
        db.execute('UPDATE team_requests SET status=? WHERE request_id=?', (status, 'req1'))
    assert outbox.get('req1')['pollAfterSeconds'] == 0
