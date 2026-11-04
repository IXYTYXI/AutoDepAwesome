import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from awesome_deploy.intake_remote import PlatformClient
from awesome_deploy.team_client import TeamPlatformClient
from awesome_deploy.team_server import load_team_outbox
from awesome_deploy.team_sessions import SharedSessions, load_shared_sessions


@pytest.mark.parametrize('client_type', [PlatformClient, TeamPlatformClient])
@pytest.mark.parametrize('origin', [
    'https://@example.test', 'https://example.test:abc', 'https://example.test:99999',
    'https://exa\nmple.test', 'https://example.test\\path', None, 123,
])
def test_platform_origins_reject_invalid_configuration_before_transport(client_type, origin):
    with pytest.raises(ValueError):
        client_type(origin, Path('/token'), client=object())


def test_local_http_is_only_allowed_for_the_team_adapter():
    for origin in ('http://127.0.0.1:8088', 'http://localhost:8088', 'http://[::1]:8088'):
        TeamPlatformClient(origin, Path('/token'), client=object())
        with pytest.raises(ValueError):
            PlatformClient(origin, Path('/token'), client=object())
    with pytest.raises(ValueError):
        TeamPlatformClient('http://remote.example.test', Path('/token'), client=object())


@pytest.mark.parametrize('host', ['bad\thost', '域名', '@example.test', 'bad..host', '-host', 123])
def test_team_host_is_validated_before_transport(host):
    with pytest.raises(ValueError):
        TeamPlatformClient('http://127.0.0.1:8088', Path('/token'), host=host, client=object())


@pytest.mark.parametrize('field,value', [
    ('app_id', 'cli_bad_id'), ('app_id', 'cli_' + 'x' * 81), ('app_id', None),
    ('tenant_key', ''), ('tenant_key', 'tenant space'), ('tenant_key', None),
    ('source', None), ('source', 'UPPER'), ('key', 'x' * 32),
])
def test_shared_session_namespace_matches_platform_configuration(field, value):
    args = dict(source='project-a', key=b'k' * 32, app_id='cli_test', tenant_key='tenant')
    args[field] = value
    with pytest.raises(ValueError):
        SharedSessions(object(), **args)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    created = []
    def from_url(url, **kwargs):
        client = SimpleNamespace(url=url, kwargs=kwargs)
        created.append(client)
        return client
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(
        Redis=SimpleNamespace(from_url=from_url)))
    password, signing = tmp_path / 'password', tmp_path / 'signing'
    for file in (password, signing):
        file.write_text('x' * 32)
        file.chmod(0o600)
    item = dict(source='project-a', redis_url='redis://127.0.0.1:6391/0',
                redis_username='awesome_b', redis_password_file=str(password),
                signing_key_file=str(signing), feishu_app_id='cli_test', tenant_key='tenant')
    path = tmp_path / 'sessions.json'
    def load(**changes):
        path.write_text(json.dumps([{**item, **changes}]))
        return load_shared_sessions(path)
    return load, created


@pytest.mark.parametrize('changes', [
    {'redis_username': None}, {'redis_username': ''}, {'redis_username': 123},
    {'redis_username': 'bad\tuser'}, {'redis_password_file': None}, {'signing_key_file': 123},
    {'redis_url': 'redis://@127.0.0.1:6391/0'}, {'redis_url': 'redis://127.0.0.1:99999/0'},
    {'redis_url': 'redis://127.0.0.\n1:6391/0'}, {'redis_url': None},
    {'redis_url': 'redis://127.0.0.1:6391/0?'}, {'redis_url': 'redis://127.0.0.1:6391/0#'},
    {'signing_key_file': '/tmp/signing\u200b'},
    {'feishu_app_id': 'cli_bad_id'}, {'tenant_key': ''},
])
def test_registry_rejects_invalid_values_before_constructing_redis(registry, changes):
    load, created = registry
    with pytest.raises(ValueError):
        load(**changes)
    assert not created


def test_registry_preserves_explicit_acl_and_remote_tls(registry):
    load, created = registry
    sessions = load(redis_url='rediss://redis.example.test:6380/0', redis_username='awesome_a')
    assert sessions['project-a'].app_id == 'cli_test'
    assert created[0].kwargs['username'] == 'awesome_a'
    assert created[0].kwargs['password'] == 'x' * 32
    with pytest.raises(ValueError):
        load(redis_url='redis://redis.example.test:6380/0')


@pytest.mark.parametrize('changes,message', [
    ({'shared_sessions_file': None}, '绝对路径'),
    ({'database_path': 123}, '绝对路径'),
    ({'platform_token_file': 'relative'}, '绝对路径'),
    ({'intake_token_file': '/tmp/token\x00'}, '绝对路径'),
    ({'intake_token_file': '/tmp/token\u200b'}, '绝对路径'),
    ({'source': 'missing'}, '来源'),
    ({'source': None}, '来源'),
])
def test_team_loader_reports_configuration_errors(tmp_path, monkeypatch, changes, message):
    cfg = dict(source='project-a', shared_sessions_file='/tmp/sessions.json',
               database_path='/tmp/requests.sqlite3', platform_origin='http://127.0.0.1:8088',
               platform_host='deploy.example.test', platform_token_file='/tmp/platform-token',
               intake_token_file='/tmp/intake-token')
    path = tmp_path / 'team.json'
    path.write_text(json.dumps({**cfg, **changes}))
    monkeypatch.setattr('awesome_deploy.team_server.load_shared_sessions', lambda path: {})
    with pytest.raises(ValueError, match=message):
        load_team_outbox(path)
