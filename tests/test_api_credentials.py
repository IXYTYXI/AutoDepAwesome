import importlib
import importlib.util
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.identity import IdentityStore
from awesome_deploy.store import Store
from test_access import payload, user


def credentials(settings, *, clock):
    assert importlib.util.find_spec('awesome_deploy.api_credentials') is not None
    module = importlib.import_module('awesome_deploy.api_credentials')
    return module.ApiCredentialStore(settings, clock=lambda: clock[0])


@pytest.fixture
def setup(tmp_path):
    settings = Settings(state_dir=tmp_path, auth_mode='feishu', feishu_app_id='cli_test',
                        feishu_tenant_key='tenant')
    store = Store(settings)
    IdentityStore(settings, provider=object())
    owner = user(settings)
    app_id = store.enqueue(payload(), 'browser-first', actor=owner)['app_id']
    return settings, owner, app_id, [1_800_000_000]


def test_issue_persists_only_digest_and_revoke_blocks_use(setup):
    settings, owner, app_id, clock = setup
    service = credentials(settings, clock=clock)
    issued = service.issue(owner['id'], 'build-server', kind='source', app_id=app_id, expires_days=30)
    assert issued['token'].startswith('adp_')
    with sqlite3.connect(settings.database_path) as db:
        stored = db.execute('SELECT token_hash FROM api_credentials WHERE id=?', (issued['id'],)).fetchone()[0]
    assert issued['token'] not in stored
    actor, credential = service.authenticate(issued['token'])
    assert actor['id'] == owner['id'] and credential['id'] == issued['id']
    assert service.list()[0]['name'] == 'build-server'
    assert 'token' not in service.list()[0]
    service.revoke(issued['id'])
    assert service.authenticate(issued['token']) is None


def test_expiry_and_user_revocation_block_use(setup):
    settings, owner, app_id, clock = setup
    service = credentials(settings, clock=clock)
    issued = service.issue(owner['id'], 'operator', kind='person', app_id=app_id, expires_days=1)
    clock[0] += 86400
    assert service.authenticate(issued['token']) is None
    clock[0] -= 86400
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET enabled=0,revision=revision+1 WHERE id=?', (owner['id'],))
    assert service.authenticate(issued['token']) is None


def test_only_explicit_narrow_scopes_can_be_issued(setup):
    settings, owner, app_id, clock = setup
    service = credentials(settings, clock=clock)
    for options in ({'app_id': app_id, 'allow_create': True}, {}, {'app_id': app_id, 'expires_days': 91}):
        with pytest.raises(DeployError):
            service.issue(owner['id'], 'bad', kind='source', **options)
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET can_deploy=0 WHERE id=?', (owner['id'],))
    with pytest.raises(DeployError):
        service.issue(owner['id'], 'no-rights', kind='person', app_id=app_id)
