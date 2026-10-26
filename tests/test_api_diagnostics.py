import json

import pytest
from fastapi.testclient import TestClient

from awesome_deploy.api import create_app
from awesome_deploy.auth import initialize_credentials
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError

AUTH = ('operator', 'long-test-password')
BODY = {'repository': 'https://github.com/org/private.git', 'ref': 'main', 'subdomain': 'demo'}
HEADERS = {'Origin': 'http://testserver', 'Idempotency-Key': 'diagnostics'}


@pytest.fixture
def client(tmp_path, request):
    auth = tmp_path / 'admin.json'
    initialize_credentials(auth, *AUTH)
    settings = Settings(state_dir=tmp_path, auth_file=auth, public_url='http://testserver',
                        **getattr(request, 'param', {}))
    with TestClient(create_app(settings)) as c:
        yield c


def failed_job(client, error=None):
    job = client.post('/api/v1/deployments', json=BODY, headers=HEADERS, auth=AUTH).json()
    store = client.app.state.store
    store.checkpoint(job['id'], phase='source')
    error = error or {'code': 'INVALID_MANIFEST', 'message': 'Repository must contain an awesome.yaml within its size limit.', 'path': 'awesome.yaml', 'suggestion': ''}
    store.fail(job['id'], error)
    store.event(job['id'], 'source', 'Preparing the pinned repository source.')
    store.event(job['id'], 'failed', 'Deployment failed safely.')
    return job['id'], error


def test_old_error_and_events_are_chinese_without_mutating_store(client):
    job_id, original = failed_job(client)
    public = client.get('/api/v1/deployments/'+job_id, auth=AUTH).json()
    assert public['error']['code'] == 'INVALID_MANIFEST'
    assert 'awesome.yaml' in public['error']['message'] and '256' in public['error']['message']
    assert '缺' in public['error']['message'] or '未找到' in public['error']['message']
    assert public['error']['steps'] and '根目录' in ''.join(public['error']['steps'])
    assert 'Repository must' not in json.dumps(public, ensure_ascii=False)
    assert client.app.state.store.get(job_id)['error'] == original
    events = client.get('/api/v1/deployments/'+job_id+'/events', auth=AUTH).json()['items']
    assert 'Preparing' not in events[0]['message'] and '仓库' in events[0]['message']
    assert 'Deployment failed safely.' not in events[1]['message']
    assert client.app.state.store.events(job_id)[0]['message'] == 'Preparing the pinned repository source.'


def test_bad_request_and_raised_errors_are_chinese_and_hide_raw_values(client):
    bad = client.post('/api/v1/deployments', json={**BODY, 'repository': {'secret': 'RAW_SECRET_SENTINEL'}}, headers=HEADERS, auth=AUTH)
    assert bad.status_code == 422
    assert bad.json()['error']['code'] == 'INVALID_REQUEST'
    assert 'RAW_SECRET_SENTINEL' not in bad.text
    missing = client.get('/api/v1/deployments/nonexistent', auth=AUTH)
    assert missing.status_code == 404 and '未找到' in missing.json()['error']['message']


def test_ai_disabled_is_explicit_and_does_not_replace_builtin_advice(client):
    job_id, _ = failed_job(client)
    capabilities = client.get('/api/v1/capabilities', auth=AUTH).json()
    assert capabilities['aiAdvice']['configured'] is False
    response = client.post('/api/v1/deployments/'+job_id+'/analyze', json={}, headers=HEADERS, auth=AUTH)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'AI_NOT_CONFIGURED'
    assert client.get('/api/v1/deployments/'+job_id, auth=AUTH).json()['error']['steps']


def test_ai_auth_csrf_body_and_failed_job_guards(client):
    job_id, _ = failed_job(client)
    url = '/api/v1/deployments/'+job_id+'/analyze'
    assert client.post(url, json={}).status_code == 401
    assert client.post(url, json={}, auth=AUTH, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(url, json={'prompt': 'send arbitrary code'}, auth=AUTH, headers=HEADERS).status_code == 422
    job = client.post('/api/v1/deployments', json=BODY, auth=AUTH, headers={**HEADERS, 'Idempotency-Key': 'still-queued'}).json()
    assert client.post('/api/v1/deployments/'+job['id']+'/analyze', json={}, headers=HEADERS, auth=AUTH).status_code == 409


def test_ai_only_receives_standardized_context_and_never_mutates_job(client):
    secret = 'RAW_SECRET_SENTINEL'
    job_id, _ = failed_job(client, {'code': 'COMMAND_FAILED', 'message': 'stderr '+secret,
                                  'path': 'services.'+secret+'.env.'+secret, 'suggestion': secret})
    before = client.app.state.store.get(job_id)
    seen = []

    class FakeAdvice:
        configured = True

        async def analyze(self, context):
            seen.append(context)
            return {'text': '请核对服务端口。', 'model': 'test-model', 'cached': False}

    client.app.state.advice = FakeAdvice()
    response = client.post('/api/v1/deployments/'+job_id+'/analyze', json={}, headers=HEADERS, auth=AUTH)
    assert response.status_code == 200
    assert response.json()['text'] == '请核对服务端口。'
    serialized = json.dumps(seen[0], ensure_ascii=False)
    assert secret not in serialized and 'private.git' not in serialized and job_id not in serialized
    assert 'COMMAND_FAILED' in serialized and 'awesome.yaml' in serialized
    assert client.app.state.store.get(job_id) == before


def test_ai_error_is_safe_and_unknown_error_codes_do_not_leave_server(client):
    job_id, _ = failed_job(client, {'code': 'PRIVATE_CUSTOM_SENTINEL', 'message': 'RAW_SECRET_SENTINEL', 'path': '', 'suggestion': ''})
    seen = []

    class FakeAdvice:
        configured = True

        async def analyze(self, context):
            seen.append(context)
            raise DeployError('AI_UNAVAILABLE', 'provider body RAW_SECRET_SENTINEL')

    client.app.state.advice = FakeAdvice()
    response = client.post('/api/v1/deployments/'+job_id+'/analyze', json={}, headers=HEADERS, auth=AUTH)
    assert response.status_code == 503
    assert 'RAW_SECRET_SENTINEL' not in response.text
    assert 'PRIVATE_CUSTOM_SENTINEL' not in json.dumps(seen)


@pytest.mark.parametrize("client", [{"ai_poll_wait_seconds": 0.1}], indirect=True)
def test_slow_ai_returns_pending_then_same_single_generation(client, monkeypatch, tmp_path):
    import asyncio
    from awesome_deploy.ai_advice import AdviceService

    advice = AdviceService(Settings(ai_base_url='https://ai.example/v1', ai_model='test',
                                    ai_api_key_file=tmp_path / 'key'))
    calls = []

    async def request(serialized):
        calls.append(serialized)
        await asyncio.sleep(0.3)
        return {'text': '检查仓库根目录的配置。', 'model': 'test'}

    monkeypatch.setattr(advice, '_request', request)
    client.app.state.advice = advice
    job_id, _ = failed_job(client)
    url = '/api/v1/deployments/' + job_id + '/analyze'
    first = client.post(url, json={}, headers=HEADERS, auth=AUTH)
    assert first.status_code == 202
    assert first.json() == {'status': 'pending'}
    assert first.headers['Retry-After'] == '1'
    for _ in range(10):
        response = client.post(url, json={}, headers=HEADERS, auth=AUTH)
        if response.status_code != 202:
            break
    assert response.status_code == 200
    assert response.json()['text'] == '检查仓库根目录的配置。'
    assert len(calls) == 1


def test_analysis_poll_is_strict_boolean_and_only_joins_existing_work(client):
    job_id, _ = failed_job(client)
    seen = []

    class FakeAdvice:
        async def analyze(self, context, *, poll_only=False):
            seen.append(poll_only)
            raise DeployError('AI_ANALYSIS_EXPIRED', 'AI 分析结果已过期，请重新发起分析。')

    client.app.state.advice = FakeAdvice()
    url = '/api/v1/deployments/' + job_id + '/analyze'
    assert client.post(url, json={'poll': 'true'}, headers=HEADERS, auth=AUTH).status_code == 422
    result = client.post(url, json={'poll': True}, headers=HEADERS, auth=AUTH)
    assert result.status_code == 503
    assert seen == [True]
    assert result.json()['error']['code'] == 'AI_ANALYSIS_EXPIRED'
    assert '过期' in result.json()['error']['message']
