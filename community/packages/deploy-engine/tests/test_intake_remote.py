import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from awesome_deploy.intake_remote import BaseIntake, PlatformClient, PlatformRejected


@pytest.mark.parametrize('field', ['base_token', 'table_id'])
@pytest.mark.parametrize('value', [None, 123, True, [], {}])
def test_base_intake_identifiers_reject_non_strings_before_transport(field, value):
    args = {'base_token': 'base1234567890123', 'table_id': 'tbl12345678'}
    args[field] = value
    with pytest.raises(ValueError, match='多维表格标识无效'):
        BaseIntake(object(), **args, client=object())


def test_platform_client_uses_one_key_for_submission_and_reads_status(tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('adp_example')
    token_file.chmod(0o600)
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == 'POST':
            return httpx.Response(202, json={'id': 'j1', 'appId': 'a1', 'status': 'queued'})
        return httpx.Response(200, json={'id': 'j1', 'appId': 'a1', 'status': 'succeeded'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    remote = PlatformClient('https://deploy.example.test', token_file, client=client)
    body = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': 'demo'}
    assert remote.post('source-1', body)['id'] == 'j1'
    assert remote.get('j1')['status'] == 'succeeded'
    assert seen[0].headers['idempotency-key'] == 'source-1'
    assert seen[0].headers['authorization'] == 'Bearer adp_example'
    assert seen[0].url.path == '/api/v1/automation/deployments'
    assert seen[1].url.path.endswith('/j1')


def test_platform_client_reports_invalid_submission_as_permanent_rejection(tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('adp_example')
    token_file.chmod(0o600)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(422, json={'detail': 'invalid repository'})))
    remote = PlatformClient('https://deploy.example.test', token_file, client=client)
    with pytest.raises(PlatformRejected) as error:
        remote.post('source-1', {'repository': 'https://github.com/org/repo.git'})
    assert error.value.status_code == 422


def test_base_intake_upserts_and_lists_full_request(monkeypatch):
    calls = []
    fields = {'请求 ID': 'source-1', '请求内容': '{"ref":"main"}',
              '完整性签名': 'signature', '状态': 'pending', '部署 ID': '', '应用 ID': '',
              '提交时间': 1000, '更新时间': 1000}

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/oauth/v3/token'):
            return httpx.Response(200, json={'code': 0, 'access_token': 'oauth', 'expires_in': 7200})
        if request.url.path.endswith('/search') and len(calls) == 2:
            return httpx.Response(200, json={'code': 0, 'data': {'items': []}})
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'code': 0, 'data': {'items': [
                {'record_id': 'rec1', 'fields': {**fields, '请求内容': [{'text': fields['请求内容']}]}},
            ], 'has_more': False}})
        return httpx.Response(200, json={'code': 0, 'data': {'record': {'record_id': 'rec1'}}})

    monkeypatch.setattr('awesome_deploy.base_sync._read_secret', lambda path: 'app-secret')
    settings = SimpleNamespace(feishu_app_id='cli_test', feishu_app_secret_file=Path('/secret'))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    remote = BaseIntake(settings, 'base123456789012', 'tbl123456789', client=client)
    remote.upsert('source-1', fields)
    assert json.loads(calls[2].content)['fields'] == fields
    assert remote.list_records()[0]['请求内容'] == fields['请求内容']
