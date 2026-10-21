import base64
from dataclasses import replace
import json
import os
import sqlite3

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.secret_vault import SecretVault
from awesome_deploy.vault_format import ensure_schema, set_format
from awesome_deploy.vault_runtime import VaultRuntime


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    key = tmp_path / 'keys.json'
    key.write_text(json.dumps({'format':1, 'active':'k1', 'keys':{'k1':base64.b64encode(os.urandom(32)).decode()}}))
    key.chmod(0o600)
    settings = replace(Settings(), state_dir=state, auth_mode='feishu',
                       feishu_app_id='cli_test', feishu_tenant_key='tenant',
                       secret_backend='encrypted', secret_kek_file=key)
    db = sqlite3.connect(settings.database_path, isolation_level=None)
    db.row_factory = sqlite3.Row
    SecretVault.ensure_schema(db)
    ensure_schema(db)
    VaultRuntime.ensure_schema(db)
    db.execute('BEGIN IMMEDIATE')
    set_format(db, settings, phase='active', migration_id='fixture')
    vault = SecretVault(settings)
    vault.register_app(db, 'app-one', 'tenant', 'generation-one', actor_id='fixture')
    version = vault.create_missing(db, 'app-one', 'API_KEY', 'runtime-fixture-secret', actor_id='fixture')
    db.commit()
    runtime = VaultRuntime(settings)
    job = {'id':'job-one','app_id':'app-one','snapshot':{
        'manifest':{'services':{'web':{'secretRefs':{'MODEL_KEY':'API_KEY'}}}},
        'secret_versions':{'web':{'API_KEY':version}},
        'vault_identity':runtime.binding({'app_id':'app-one'})}}
    yield settings, db, vault, runtime, job, version
    db.close()


def test_rotation_preserves_frozen_runtime_bytes(setup):
    settings, db, vault, runtime, job, version = setup
    db.execute('BEGIN IMMEDIATE')
    vault.rotate(db, 'app-one', 'API_KEY', 'replacement-fixture-secret', version, actor_id='fixture')
    db.commit()
    assert runtime.resolve(job, 'web') == {'MODEL_KEY':'runtime-fixture-secret'}


def test_revoked_frozen_version_is_rejected_without_container_creation(setup):
    settings, db, vault, runtime, job, version = setup
    db.execute('BEGIN IMMEDIATE')
    vault.revoke(db, 'app-one', 'API_KEY', version, version, actor_id='fixture')
    db.commit()
    with pytest.raises(DeployError) as error:
        runtime.validate(job)
    assert error.value.code == 'SECRET_REVOKED'


def test_frozen_version_cannot_be_attached_to_another_generation(setup):
    settings, db, vault, runtime, job, version = setup
    job['snapshot']['vault_identity']['data_generation'] = 'other'
    with pytest.raises(DeployError):
        runtime.validate(job)


def test_legacy_job_requires_explicit_migration_binding(setup):
    settings, db, vault, runtime, job, version = setup
    del job['snapshot']['vault_identity']
    with pytest.raises(DeployError):
        runtime.validate(job)
    db.execute('INSERT INTO vault_legacy_jobs VALUES(?,?,?)', ('job-one','app-one','generation-one'))
    runtime.validate(job)


def test_runtime_secret_cannot_be_remapped_to_master_key(setup):
    settings, db, vault, runtime, job, version = setup
    job['snapshot']['manifest']['services']['web']['secretRefs'] = {'MASTER_KEY':'API_KEY'}
    with pytest.raises(DeployError) as error:
        runtime.validate(job)
    assert error.value.code == 'SECRET_MASTER_PROTECTED'


def test_snapshot_must_freeze_every_declared_reference(setup):
    settings, db, vault, runtime, job, version = setup
    job['snapshot']['secret_versions']['web'] = {}
    with pytest.raises(DeployError):
        runtime.validate(job)


def test_legacy_services_without_refs_need_no_fabricated_snapshot_version(setup):
    settings, db, vault, runtime, job, version = setup
    del job['snapshot']['vault_identity']
    db.execute('INSERT INTO vault_legacy_jobs VALUES(?,?,?)', ('job-one','app-one','generation-one'))
    job['snapshot']['manifest']['services']['frontend'] = {}
    runtime.validate(job)
    assert runtime.resolve(job, 'frontend') == {}


@pytest.mark.parametrize('action', ['start', 'recover_active', 'publish', 'create'])
def test_runtime_entrypoints_reject_revoked_values_before_external_action(setup, action):
    from awesome_deploy.runtime import DeploymentBackend
    settings, db, vault, runtime, job, version = setup
    db.execute('BEGIN IMMEDIATE')
    vault.revoke(db, 'app-one', 'API_KEY', version, version, actor_id='fixture')
    db.commit()
    backend = DeploymentBackend(settings)
    calls = []
    backend._docker = lambda *args, **kwargs: calls.append('docker')
    backend._gateway = lambda *args: calls.append('gateway')
    with pytest.raises(DeployError) as error:
        if action == 'create':
            backend._create_container(job, 'web', job['snapshot']['manifest']['services']['web'], {}, [])
        elif action == 'publish':
            backend.publish(job)
        else:
            getattr(backend, action)(job, lambda delta: None)
    assert error.value.code == 'SECRET_REVOKED'
    assert not calls


def test_revocation_stops_owned_containers_even_when_gateway_fails(setup):
    from awesome_deploy.runtime import DeploymentBackend
    settings, db, vault, runtime, job, version = setup
    job.update(status='succeeded', execution={'publish_intent':True})
    backend = DeploymentBackend(settings)
    calls = []
    backend._runtime_preflight = lambda: None
    backend._gateway = lambda *args: (_ for _ in ()).throw(RuntimeError('unknown gateway state'))
    backend._stop_owned_containers = lambda *args, **kwargs: calls.append('stopped')
    backend.cleanup = lambda *args: calls.append('cleanup')
    with pytest.raises(DeployError) as error:
        backend.revoke_release(job)
    assert error.value.code == 'SECRET_REVOCATION_FAILED'
    assert calls == ['stopped']
