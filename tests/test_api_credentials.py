import importlib
import importlib.util
import sqlite3
import time

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


def test_user_can_list_and_revoke_only_own_person_credentials(setup):
    settings, owner, app_id, clock = setup
    service = credentials(settings, clock=clock)
    own = service.issue(owner['id'], 'my-ci', kind='person', app_id=app_id)
    source = service.issue(owner['id'], 'admin-source', kind='source', app_id=app_id)
    assert [item['id'] for item in service.list(owner_user_id=owner['id'])] == [own['id']]
    with pytest.raises(DeployError):
        service.revoke(source['id'], actor=owner)
    service.revoke(own['id'], actor=owner)
    assert service.authenticate(own['token']) is None


def test_source_can_have_unbounded_credentials_and_all_accessible_app_scope(setup):
    settings, owner, app_id, clock = setup
    clock[0] = time.time()
    service = credentials(settings, clock=clock)
    for index in range(101):
        issued = service.issue(owner['id'], f'source-{index}', kind='source', allow_any_app=True)
    assert issued['allowAnyApp'] is True
    with service._connection() as db:
        service.authorize_scope(db, owner, issued['id'], None)
        service.authorize_scope(db, owner, issued['id'], app_id)
    with pytest.raises(DeployError):
        service.issue(owner['id'], 'bad-scope', kind='source', app_id=app_id, allow_any_app=True)
    other = user(settings, name='Other')
    other_app = Store(settings).enqueue(payload(subdomain='other'), 'other-app', actor=other)['app_id']
    with pytest.raises(DeployError) as denied:
        Store(settings).enqueue(payload(app_id=other_app, subdomain='other'), 'foreign-update',
                                actor=owner, credential_id=issued['id'])
    assert denied.value.code == 'ACCESS_DENIED'
