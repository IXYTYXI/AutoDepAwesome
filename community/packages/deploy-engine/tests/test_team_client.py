import importlib
import importlib.util
import json

import httpx
import pytest

from awesome_deploy import intake_remote
from awesome_deploy.intake_remote import PlatformClient, PlatformRejected
from awesome_deploy.team_server import TeamPlatformClient, TeamSearchError


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / 'source-token'
    path.write_text('source-token-one')
    path.chmod(0o600)
    return path


def test_team_server_preserves_client_imports_after_extraction():
    assert importlib.util.find_spec('awesome_deploy.team_client') is not None
    module = importlib.import_module('awesome_deploy.team_client')
    assert TeamPlatformClient is module.TeamPlatformClient
    assert TeamSearchError is module.TeamSearchError


@pytest.mark.parametrize('status', [401, 403, 500, 503])
def test_platform_http_failures_expose_status_without_changing_message(token_file, status):
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text='private upstream error'))) as client:
        remote = PlatformClient('https://platform.test', token_file, client=client)
        with pytest.raises(ConnectionError) as error:
            remote.post('req-1', {})
    assert getattr(error.value, 'status_code', 'missing') == status
    assert str(error.value) == f'平台 HTTP {status}'
    assert isinstance(error.value, intake_remote.PlatformUnavailable)


@pytest.mark.parametrize('mode,message', [
    ('network', '平台暂不可达或响应无效'),
    ('redirect', '平台发生意外跳转'),
    ('oversized', '平台响应过大'),
    ('invalid_json', '平台暂不可达或响应无效'),
    ('invalid_shape', '平台 HTTP 200'),
])
def test_non_http_failures_have_no_status_and_never_follow_redirects(token_file, mode, message):
    seen = []

    def handle(request):
        seen.append(request)
        if mode == 'network':
            raise httpx.ConnectError('private network detail', request=request)
        if mode == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://other.test/token-target'})
        if mode == 'oversized':
            return httpx.Response(200, content=b'x' * 131073)
        if mode == 'invalid_json':
            return httpx.Response(200, content=b'{invalid')
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        remote = TeamPlatformClient('https://platform.test', token_file, client=client)
        with pytest.raises(ConnectionError) as error:
            remote.post('req-1', {})
    assert getattr(error.value, 'status_code', 'missing') is None
    assert isinstance(error.value, intake_remote.PlatformUnavailable)
    assert str(error.value) == message
    assert len(seen) == 1
    assert seen[0].url.host == 'platform.test'
    assert seen[0].headers['Authorization'] == 'Bearer source-token-one'


@pytest.mark.parametrize('status,exception', [
    (401, ConnectionError), (403, PlatformRejected), (422, PlatformRejected),
    (500, ConnectionError), (503, ConnectionError),
])
def test_team_submission_distinguishes_retryable_and_permanent_failures(token_file, status, exception):
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={'detail': 'private'}))) as client:
        remote = TeamPlatformClient('http://127.0.0.1:8088', token_file, client=client)
        with pytest.raises(exception) as error:
            remote.post('req-1', {})
    assert getattr(error.value, 'status_code', 'missing') == status


@pytest.mark.parametrize('search_method', ['search', 'search_owners'])
@pytest.mark.parametrize('status,expected', [(401, 401), (403, 403), (422, 422), (500, 503), (503, 503)])
def test_team_search_preserves_actionable_http_statuses(token_file, status, expected, search_method):
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={'detail': 'private'}))) as client:
        remote = TeamPlatformClient('https://platform.test', token_file, client=client)
        with pytest.raises(TeamSearchError) as error:
            getattr(remote, search_method)({'sessionId': 'session'})
    assert error.value.status_code == expected
    assert str(error.value) == '项目查询未完成。'


@pytest.mark.parametrize('method,status,exception', [
    ('post', 403, PlatformRejected), ('search', 401, TeamSearchError), ('search', 403, TeamSearchError),
])
def test_team_failure_classification_is_independent_of_error_wording(token_file, monkeypatch,
                                                                    method, status, exception):
    monkeypatch.setattr(intake_remote.PlatformUnavailable, '__str__', lambda self: 'translated message')
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={}))) as client:
        remote = TeamPlatformClient('https://platform.test', token_file, client=client)
        with pytest.raises(exception) as error:
            if method == 'post':
                remote.post('req-1', {})
            else:
                remote.search({'sessionId': 'session'})
    assert error.value.status_code == status


@pytest.mark.parametrize('search_method', ['search', 'search_owners'])
@pytest.mark.parametrize('mode', ['network', 'redirect', 'oversized', 'invalid_json', 'invalid_shape'])
def test_team_search_maps_transport_and_invalid_response_failures_to_unavailable(token_file, mode, search_method):
    def handle(request):
        if mode == 'network':
            raise httpx.ReadTimeout('private timeout detail', request=request)
        if mode == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://other.test'})
        if mode == 'oversized':
            return httpx.Response(200, content=b'x' * 131073)
        if mode == 'invalid_json':
            return httpx.Response(200, content=b'not json')
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        remote = TeamPlatformClient('https://platform.test', token_file, client=client)
        with pytest.raises(TeamSearchError) as error:
            getattr(remote, search_method)({'sessionId': 'session'})
    assert error.value.status_code == 503


def test_team_transport_keeps_host_routes_and_rereads_rotated_token(token_file):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'id': 'job-1', 'appId': 'app-1', 'status': 'queued'})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        remote = TeamPlatformClient('http://127.0.0.1:8088', token_file,
                                    host='deploy.example.test', client=client)
        payload = {'deployment': {'subdomain': 'demo'}, 'receipt': {'signature': 'test'}}
        remote.post('req-1', payload)
        token_file.write_text('source-token-two')
        remote.get('job-1')
        remote.search({'sessionId': 'session'})
    assert [request.url.path for request in seen] == [
        '/api/v1/integrations/deployments', '/api/v1/integrations/deployments/job-1',
        '/api/v1/integrations/apps/search',
    ]
    assert [request.headers['Authorization'] for request in seen] == [
        'Bearer source-token-one', 'Bearer source-token-two', 'Bearer source-token-two',
    ]
    assert all(request.headers['Host'] == 'deploy.example.test' for request in seen)
    assert seen[0].headers['Idempotency-Key'] == 'req-1'
    assert json.loads(seen[0].content) == payload
    assert json.loads(seen[2].content) == {'sessionId': 'session'}


def test_owner_search_transport_keeps_identity_in_post_body(token_file):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'items': [{'ownerId': 'user-1', 'displayName': '张三'}],
                                        'total': 1, 'limit': 20, 'offset': 0, 'nextOffset': None})
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        remote = TeamPlatformClient('http://127.0.0.1:8088', token_file,
                                    host='deploy.example.test', client=transport)
        body = {'sessionId': 'operator-session', 'query': '张', 'limit': 20, 'offset': 0}
        assert remote.search_owners(body)['items'][0]['ownerId'] == 'user-1'
    assert len(seen) == 1
    request = seen[0]
    assert request.method == 'POST'
    assert request.url.path == '/api/v1/integrations/owners/search'
    assert request.headers['Authorization'] == 'Bearer source-token-one'
    assert request.headers['Host'] == 'deploy.example.test'
    assert json.loads(request.content) == body
