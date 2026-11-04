import base64
from dataclasses import replace
import json
import os
import time
from types import SimpleNamespace

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secret_vault import SecretVault
from awesome_deploy.store import Store
from awesome_deploy.vault_format import set_format
from awesome_deploy.vault_runtime import VaultRuntime
from awesome_deploy.vault_service import VaultService
from awesome_deploy.worker import Worker
from test_access import user, payload
from test_worker import Backend, Source


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    key = tmp_path / 'keyring.json'
    key.write_text(json.dumps({'format': 1, 'active':'k1', 'keys':{'k1':base64.b64encode(os.urandom(32)).decode()}}))
    key.chmod(0o600)
    settings = replace(Settings(), state_dir=tmp_path/'state', auth_mode='feishu',
                       feishu_app_id='cli_test', feishu_tenant_key='tenant',
                       secret_backend='encrypted', secret_kek_file=key)
    initial = Store(settings, allow_vault_maintenance=True)
    with initial._connection(write=True) as db:
        set_format(db,settings,phase='active',migration_id='fixture')
    store=Store(settings)
    actor={**user(settings),'authenticated_at':time.time()}
    job=store.enqueue(payload(),'first',actor=actor)
    with store._connection(write=True) as db:
        version=SecretVault(settings).create_missing(db,job['app_id'],'TOKEN','fixture-test-secret',actor_id=actor['id'])
    snapshot={'manifest':{'services':{'web':{'secretRefs':{'TOKEN':'TOKEN'},'resources':{'cpu':0.5,'memoryMiB':128}}}},
              'vault_identity':VaultRuntime(settings).binding(job),'secret_versions':{'web':{'TOKEN':version}},'frozen_value':'durable'}
    job=store.checkpoint(job['id'],snapshot=snapshot,execution={'frozen':True})
    vault=VaultService(settings,store)
    calls=[]
    backend=Backend(calls)
    backend.revoke_release=lambda item: calls.append(('revoke',item['id']))
    worker=Worker(settings,store,Source(calls),backend)
    worker.access=SimpleNamespace(authorize_operation=lambda *args,**kw:None,repository_credentials_allowed=lambda *args:False)
    return settings,store,actor,job,version,vault,backend,worker,calls


def revoke(setup):
    _,store,actor,job,version,vault,*_=setup
    return vault.revoke(job['app_id'],'TOKEN',version,version,'测试吊销',actor=actor)


def test_new_app_is_registered_in_same_ownership_transaction(setup):
    settings,store,actor,job,*_=setup
    with store._connection() as db:
        row=db.execute('SELECT * FROM vault_apps WHERE app_id=?',(job['app_id'],)).fetchone()
        assert row['tenant_id']=='tenant' and row['created_by']==actor['id']
        assert db.execute('SELECT count(*) FROM app_secret_scopes').fetchone()[0]==0


def test_revoke_during_build_prevents_start_then_confirms_cleanup(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    original=backend.build
    def build(item,checkpoint):
        result=original(item,checkpoint)
        revoke(setup)
        return result
    backend.build=build
    assert worker.run_once()
    assert 'start' not in calls
    assert store.get(job['id'])['error']['code']=='SECRET_REVOKED'
    assert worker.run_once()
    assert vault.get_revocation(vault.describe(job['app_id'],actor=actor)['operations'][0]['id'])['status']=='succeeded'
    assert store.get(job['id'])['execution']['resources_released']


def test_active_revocation_precedes_system_recovery_and_releases_only_after_confirmation(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    assert worker.run_once()
    assert store.get(job['id'])['status']=='succeeded'
    operation=revoke(setup)
    calls.clear()
    assert worker.run_once()
    assert calls==[('revoke',job['id'])]
    assert store.active_release(job['app_id']) is None
    assert store.apps()[0]['subdomain'] is None
    assert vault.get_revocation(operation['id'])['status']=='succeeded'
    with store._connection() as db:
        assert not db.execute('SELECT * FROM reservations').fetchall()


def test_uncertain_revocation_blocks_recovery_and_needs_explicit_retry(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    operation=revoke(setup)
    backend.revoke_release=lambda item: (_ for _ in ()).throw(RuntimeError('secret-must-not-appear'))
    worker.run_once()
    pending=vault.get_revocation(operation['id'])
    assert pending['status']=='recovery_required' and 'secret-must-not-appear' not in json.dumps(pending)
    assert store.active_release(job['app_id']) is not None
    assert store.next_active_recovery('new-boot') is None
    assert vault.next_revocation() is None
    with pytest.raises(DeployError):
        store.enqueue(payload(job['app_id']),'next',actor=actor)
    backend.revoke_release=lambda item: None
    assert revoke(setup)['id']==operation['id']
    worker.run_once()
    assert vault.get_revocation(operation['id'])['status']=='succeeded'


def test_finish_and_recovery_cannot_bypass_revoked_versions(setup):
    _,store,_,job,_,_,_,worker,_=setup
    worker.run_once()
    revoke(setup)
    for action in (lambda:store.finish(job['id']),lambda:store.begin_active_recovery(job['id'],'boot')):
        with pytest.raises(DeployError):
            action()


def test_revocation_retains_original_data_identity_for_next_deployment(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    volume = {'web': {'name':'original-volume','instance':'retained-original-identity'}}
    store.checkpoint(job['id'],execution={'storage_volumes':volume})
    revoke(setup)
    worker.run_once()
    vault.rotate(job['app_id'],'TOKEN','replacement-test-secret',version,actor=actor)
    next_job=store.enqueue(payload(job['app_id']),'next',actor=actor)
    prepared=store.prepare_context(next_job['id'])
    assert prepared['execution']['storage_volumes']==volume
    assert prepared['execution']['previous_release'] is None


def test_offline_and_revocation_do_not_create_competing_destruction_intents(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    with store._connection(write=True) as db:
        db.execute("INSERT INTO offline_operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ('offline-one','key',job['app_id'],job['id'],'queued','queued','{}',None,time.time(),time.time()))
    with pytest.raises(DeployError) as error:
        revoke(setup)
    assert error.value.code=='SECRET_CONFIGURATION_BUSY'
    assert vault.next_revocation() is None
    VaultRuntime(settings).validate(job)
    with pytest.raises(DeployError) as error:
        vault.rotate(job['app_id'],'TOKEN','replacement-test-secret',version,actor=actor)
    assert error.value.code=='SECRET_CONFIGURATION_BUSY'


def test_revoked_unstarted_candidate_cannot_hide_active_data_identity(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    volume={'web':{'name':'original-volume','instance':'retained-original-identity'}}
    store.checkpoint(job['id'],execution={'storage_volumes':volume})
    candidate=store.enqueue(payload(job['app_id']),'candidate',actor=actor)
    store.checkpoint(candidate['id'],snapshot=job['snapshot'])
    operation=revoke(setup)
    claimed=vault.claim_revocation(operation['id'])
    # Confirm the unstarted candidate after the old release to reproduce the
    # worst ordering. It has no volume snapshot of its own.
    store.confirm_secret_revocation_release(operation['id'],job['id'])
    store.confirm_secret_revocation_release(operation['id'],candidate['id'])
    vault.checkpoint_revocation(operation['id'],{'confirmed_job_ids':claimed['execution']['job_ids']})
    vault.finish_revocation(operation['id'])
    next_job=store.enqueue(payload(job['app_id']),'new',actor=actor)
    assert store.prepare_context(next_job['id'])['execution']['storage_volumes']==volume


def test_revocation_after_previous_recovery_still_blocks_route_rollback(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    new_version=vault.rotate(job['app_id'],'TOKEN','new-candidate-secret',version,actor=actor)['current_version']
    candidate=store.enqueue(payload(job['app_id']),'candidate',actor=actor)
    snapshot={**job['snapshot'],'secret_versions':{'web':{'TOKEN':new_version}}}
    store.checkpoint(candidate['id'],snapshot=snapshot,execution={'frozen':True})
    backend.fail_at='verify'
    original_event=store.event
    def event(job_id,phase,message):
        original_event(job_id,phase,message)
        if phase=='rollback':
            vault.revoke(job['app_id'],'TOKEN',version,new_version,'撤销旧版本',actor=actor)
    store.event=event
    calls.clear()
    worker.run_once()
    assert 'recover_active' in calls
    assert 'rollback' not in calls
    assert store.get(candidate['id'])['status']=='recovery_required'
    from awesome_deploy.runtime import DeploymentBackend
    runtime=DeploymentBackend(settings)
    runtime._gateway=lambda *args: pytest.fail('revoked previous must never reach gateway rollback')
    with pytest.raises(DeployError) as error:
        runtime.rollback(store.get(candidate['id']))
    assert error.value.code=='SECRET_REVOKED'
    # Gateway verification has an internal fallback to rollback; its caller
    # must also reject a revoked predecessor before invoking that helper.
    with pytest.raises(DeployError) as error:
        runtime.verify(store.get(candidate['id']))
    assert error.value.code=='SECRET_REVOKED'


def test_revoked_predecessor_blocks_verifier_which_could_automatically_rollback(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    current=vault.rotate(job['app_id'],'TOKEN','candidate-secret',version,actor=actor)['current_version']
    candidate=store.enqueue(payload(job['app_id']),'candidate',actor=actor)
    store.checkpoint(candidate['id'],snapshot={**job['snapshot'],'secret_versions':{'web':{'TOKEN':current}}},
                     execution={'frozen':True})
    backend.observer=lambda _:vault.revoke(job['app_id'],'TOKEN',version,current,'旧版本吊销',actor=actor)
    calls.clear()
    worker.run_once()
    assert 'publish' in calls and 'verify' not in calls and 'rollback' not in calls
    assert store.get(candidate['id'])['status']=='recovery_required'


def test_pending_revocation_also_blocks_retain_data_offline(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    worker.run_once()
    revoke(setup)
    with pytest.raises(DeployError) as error:
        store.enqueue_offline(job['app_id'],job['id'],'offline',actor=actor)
    assert error.value.code=='SECRET_REVOCATION_STATE'


def test_encrypted_destroy_scope_does_not_inherit_other_apps_domain_history(setup):
    settings,store,actor,job,version,vault,backend,worker,calls=setup
    other=store.enqueue(payload(),'other-app',actor=actor)
    store.fail(other['id'],DeployError('GIT_FAILED','fixture').as_dict())
    worker.run_once()
    with store._connection() as db:
        app=dict(db.execute('SELECT * FROM apps WHERE id=?',(job['app_id'],)).fetchone())
        metadata=store._deletion_metadata(db,app)
    assert metadata['job_ids']==[job['id']]
