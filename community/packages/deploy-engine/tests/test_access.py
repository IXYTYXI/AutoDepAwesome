import sqlite3
import time
import uuid

import pytest

from awesome_deploy.access import AccessStore
from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store


def user(settings, *, admin=False, name='Member', can_deploy=True):
    actor = dict(id=str(uuid.uuid4()), app_id='cli_test', tenant_key='tenant', open_id='ou_' + uuid.uuid4().hex,
                 name=name, enabled=1, can_deploy=int(can_deploy), is_admin=int(admin), revision=1,
                 checked_at=time.time(), eligible=1)
    with sqlite3.connect(settings.database_path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS auth_users (
            id TEXT PRIMARY KEY, app_id TEXT, tenant_key TEXT, open_id TEXT, name TEXT,
            enabled INTEGER, can_deploy INTEGER, is_admin INTEGER, revision INTEGER,
            checked_at REAL, eligible INTEGER)''')
        fields = ','.join(actor)
        db.execute(f'INSERT INTO auth_users ({fields}) VALUES ({",".join("?" for _ in actor)})', tuple(actor.values()))
    return actor


def payload(app_id=None, subdomain='demo'):
    value = dict(repository='https://github.com/acme/demo.git', ref='main', subdomain=subdomain)
    if app_id:
        value['appId'] = app_id
    return value


@pytest.fixture
def setup(tmp_path):
    settings = Settings(state_dir=tmp_path)
    store = Store(settings)
    return settings, store, AccessStore(settings)


def test_submission_creates_owned_draft_without_claiming_domain(setup):
    settings, store, access = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    app = store.apps()[0]
    assert app['id'] == job['app_id'] and app['subdomain'] is None
    assert access.require_app(actor, app['id'], 'deploy') == 'owner'
    assert access.repository_credentials_allowed(app['id']) is False


def test_actor_scoped_idempotency_and_current_authorization(setup):
    settings, store, access = setup
    first, second = user(settings), user(settings)
    one = store.enqueue(payload(), 'same', actor=first)
    two = store.enqueue(payload(subdomain='other'), 'same', actor=second)
    assert one['id'] != two['id']
    assert store.enqueue(payload(), 'same', actor=first)['id'] == one['id']
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET enabled=0, revision=revision+1 WHERE id=?', (first['id'],))
    with pytest.raises(DeployError, match='permission'):
        store.enqueue(payload(), 'same', actor=first)


def test_cannot_guess_application_or_reuse_unbound_secret_namespace(setup):
    settings, store, access = setup
    owner, intruder = user(settings), user(settings)
    job = store.enqueue(payload(), 'owner', actor=owner)
    for value in (payload(job['app_id']), payload()):
        with pytest.raises(DeployError) as error:
            store.enqueue(value, uuid.uuid4().hex, actor=intruder)
        assert error.value.code == 'ACCESS_DENIED'
    assert access.can_view(intruder, job['app_id']) is False


def test_viewer_projection_and_owner_only_membership(setup):
    settings, store, access = setup
    owner, viewer, maintainer = user(settings), user(settings), user(settings)
    job = store.enqueue(payload(), 'first', actor=owner)
    access.set_member(owner, job['app_id'], viewer['id'], 'viewer')
    access.set_member(owner, job['app_id'], maintainer['id'], 'maintainer')
    result = access.project_app(viewer, store.apps()[0])
    assert result['role'] == 'viewer' and 'repository' not in result
    for action in ('detail', 'deploy', 'secrets', 'offline', 'members'):
        with pytest.raises(DeployError):
            access.require_app(viewer, job['app_id'], action)
    assert access.require_app(maintainer, job['app_id'], 'deploy') == 'maintainer'
    with pytest.raises(DeployError):
        access.remove_member(maintainer, job['app_id'], viewer['id'])
    with pytest.raises(DeployError):
        access.remove_member(owner, job['app_id'], owner['id'])


def test_membership_revoke_regrant_never_resurrects_accepted_work(setup):
    settings, store, access = setup
    owner, member = user(settings), user(settings)
    first = store.enqueue(payload(), 'first', actor=owner)
    access.set_member(owner, first['app_id'], member['id'], 'maintainer')
    second = store.enqueue(payload(first['app_id']), 'second', actor=member)
    access.remove_member(owner, first['app_id'], member['id'])
    access.set_member(owner, first['app_id'], member['id'], 'maintainer')
    with pytest.raises(DeployError):
        access.authorize_operation_local('job', second['id'])


def test_transfer_changes_owner_and_invalidates_old_authorizations(setup):
    settings, store, access = setup
    owner, member = user(settings), user(settings)
    job = store.enqueue(payload(), 'first', actor=owner)
    access.transfer_owner(owner, job['app_id'], member['id'])
    assert access.require_app(member, job['app_id'], 'members') == 'owner'
    assert access.require_app(owner, job['app_id'], 'deploy') == 'maintainer'
    with pytest.raises(DeployError):
        access.authorize_operation_local('job', job['id'])
    assert {x['role'] for x in access.list_members(member, job['app_id'])} == {'owner', 'maintainer'}


def test_admin_must_explicitly_assign_legacy_app_and_deploy_can_be_revoked(setup):
    settings, store, access = setup
    admin, member = user(settings, admin=True), user(settings)
    with store._connection(write=True) as db:
        db.execute('INSERT INTO apps VALUES (?,?,?,?,?,?)', ('a'*32, payload()['repository'], None, None, 0, 0))
    with pytest.raises(DeployError):
        access.require_app(member, 'a'*32, 'deploy')
    access.assign_owner(admin, 'a'*32, member['id'], credentials_approved=True)
    assert access.repository_credentials_allowed('a'*32)
    assert access.require_app(member, 'a'*32, 'deploy') == 'owner'
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET can_deploy=0 WHERE id=?', (admin['id'],))
    with pytest.raises(DeployError):
        access.require_app(admin, 'a'*32, 'deploy')
    assert access.require_app(admin, 'a'*32, 'members') == 'admin'


def test_new_draft_can_generate_master_key_but_data_generation_cannot(setup):
    settings, store, access = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    store.fail(job['id'], DeployError('SECRET_NOT_FOUND', 'Missing').as_dict())
    with store.secret_configuration_scope(job['id']) as (_, can_generate):
        assert can_generate is True
    store.checkpoint(job['id'], execution={'storage_volumes': {'web': {'id': 'retained'}}})
    with store.secret_configuration_scope(job['id']) as (_, can_generate):
        assert can_generate is False


def test_revoke_between_secret_api_check_and_transaction_is_rejected(setup):
    settings, store, access = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    store.fail(job['id'], DeployError('SECRET_NOT_FOUND', 'Missing').as_dict())
    access.require_job(actor, store.get(job['id']), 'secrets')
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET can_deploy=0,revision=revision+1 WHERE id=?', (actor['id'],))
    with pytest.raises(DeployError) as error:
        with store.secret_configuration_scope(job['id'], write=True, actor=actor):
            pytest.fail('Revoked secret writer must never enter the write context')
    assert error.value.code == 'ACCESS_DENIED'


def test_member_target_must_be_an_eligible_user_in_same_tenant(setup):
    settings, store, access = setup
    actor, foreign = user(settings), user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE auth_users SET tenant_key='foreign' WHERE id=?", (foreign['id'],))
    with pytest.raises(DeployError):
        access.set_member(actor, job['app_id'], foreign['id'], 'maintainer')


def test_credentials_approval_epoch_invalidates_previously_public_only_job(setup):
    settings, store, access = setup
    actor, admin = user(settings), user(settings, admin=True)
    job = store.enqueue(payload(), 'first', actor=actor)
    access.set_credentials_approved(admin, job['app_id'], True)
    assert access.repository_credentials_allowed(job['app_id'])
    with pytest.raises(DeployError):
        access.authorize_operation_local('job', job['id'])


def test_job_list_filters_before_limit_and_never_includes_viewer_details(setup):
    settings, store, access = setup
    owner, other, viewer = user(settings), user(settings), user(settings)
    mine = store.enqueue(payload(), 'first', actor=owner)
    access.set_member(owner, mine['app_id'], viewer['id'], 'viewer')
    theirs = store.enqueue(payload(subdomain='other'), 'other', actor=other)
    assert store.list_jobs(limit=1, actor=owner)[0]['id'] == mine['id']
    assert store.list_jobs(limit=1, actor=other)[0]['id'] == theirs['id']
    assert store.list_jobs(actor=viewer) == []


@pytest.mark.parametrize('existing_draft', [False, True])
def test_member_cannot_claim_preseeded_operator_secret_namespace(setup, existing_draft):
    from awesome_deploy.secrets_store import SecretStore
    settings, store, access = setup
    settings.state_dir.chmod(0o700)
    actor = user(settings)
    app_id = None
    if existing_draft:
        original = store.enqueue(payload(subdomain='mine'), 'original', actor=actor)
        app_id = original['app_id']
    SecretStore(settings).create_missing(payload()['repository'], 'demo', 'APP_SECRET', 'synthetic-only')
    with pytest.raises(DeployError) as error:
        store.enqueue(payload(app_id), 'take-preseeded', actor=actor)
    assert error.value.code == 'ACCESS_DENIED'


def test_admin_explicitly_binds_preseeded_scope_then_owner_can_use_it(setup):
    from awesome_deploy.secrets_store import SecretStore
    settings, store, access = setup
    settings.state_dir.chmod(0o700)
    admin, owner = user(settings, admin=True), user(settings)
    SecretStore(settings).create_missing(payload()['repository'], 'demo', 'APP_SECRET', 'synthetic-only')
    job = store.enqueue(payload(), 'approved', actor=admin)
    access.transfer_owner(admin, job['app_id'], owner['id'])
    retry = store.enqueue(payload(job['app_id']), 'owner-deploy', actor=owner)
    assert retry['app_id'] == job['app_id']


def test_existing_app_cannot_rebind_another_apps_unbound_secret_scope(setup):
    settings, store, access = setup
    first, second = user(settings), user(settings)
    original = store.enqueue(payload(), 'original', actor=first)
    mine = store.enqueue(payload(subdomain='mine'), 'mine', actor=second)
    store.fail(original['id'], DeployError('SOURCE', 'synthetic').as_dict())
    with pytest.raises(DeployError):
        store.enqueue(payload(mine['app_id']), 'reuse-other-scope', actor=second)
