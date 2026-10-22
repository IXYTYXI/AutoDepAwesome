import copy
from contextlib import contextmanager
import hashlib
import json
from dataclasses import replace

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.storage import ManagedStorage
from awesome_deploy.store import Store
from test_offline_deletion_store import active


def setup_migration(tmp_path, monkeypatch):
    from awesome_deploy.storage_migration import StorageMigrations
    monkeypatch.setattr(Store, '_available', staticmethod(lambda port: True))
    settings = Settings(state_dir=tmp_path, storage_backend='xfs-project')
    store = Store(settings)
    job = active(store)
    spec = job['snapshot']['manifest']['services']['web']['storage']
    source = job['execution']['storage_volumes']['web']
    identity = {'app_id': job['app_id'], 'service': 'web', 'name': spec['name']}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    target = {'backend': 'xfs-project', 'storage_id': digest, 'generation': 'b' * 32}
    migrations = StorageMigrations(settings)
    row = migrations.prepare(job['id'], 'web', source, target, operation_id='test-migration')
    return settings, store, job, spec, source, target, migrations, row


def commit(migrations):
    return migrations.commit('test-migration', source_digest='c' * 64, target_digest='c' * 64,
                             stopped_container_ids=['d' * 64])


def test_migration_requires_verified_commit_then_resolves_without_mutating_history(tmp_path, monkeypatch):
    settings, store, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    before = copy.deepcopy(store.get(job['id']))
    assert migrations.resolve(job['app_id'], 'web', spec, source) == source
    commit(migrations)
    assert migrations.resolve(job['app_id'], 'web', spec, source) == target
    assert store.get(job['id']) == before
    from awesome_deploy.storage_migration import resolve_job
    resolved = resolve_job(settings, job)
    assert resolved['execution']['storage_volumes']['web'] == target
    assert job == before


@pytest.mark.parametrize('kind', ['digest', 'active-release', 'snapshot', 'source'])
def test_commit_refuses_changed_source_or_publication(tmp_path, monkeypatch, kind):
    _, store, job, _, _, _, migrations, _ = setup_migration(tmp_path, monkeypatch)
    if kind == 'digest':
        with pytest.raises(DeployError):
            migrations.commit('test-migration', source_digest='a' * 64, target_digest='b' * 64,
                              stopped_container_ids=['d' * 64])
        return
    with store._connection(write=True) as db:
        if kind == 'active-release':
            db.execute('UPDATE apps SET active_release_id=NULL WHERE id=?', (job['app_id'],))
        elif kind == 'snapshot':
            changed = copy.deepcopy(job['snapshot'])
            changed['commit'] = 'f' * 40
            db.execute('UPDATE jobs SET snapshot=? WHERE id=?', (json.dumps(changed), job['id']))
        else:
            state = copy.deepcopy(job['execution'])
            state['storage_volumes']['web']['instance'] = 'f' * 32
            db.execute('UPDATE jobs SET execution=? WHERE id=?', (json.dumps(state), job['id']))
    with pytest.raises(DeployError):
        commit(migrations)


def test_new_writer_durably_disables_rollback(tmp_path, monkeypatch):
    _, _, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    migrations.mark_writes_started(target)
    with pytest.raises(DeployError) as error:
        migrations.rollback('test-migration')
    assert error.value.code == 'STORAGE_MIGRATION_WRITES_STARTED'
    assert migrations.resolve(job['app_id'], 'web', spec, source) == target


def test_rollback_before_writer_restores_original_reference(tmp_path, monkeypatch):
    _, _, job, spec, source, _, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    migrations.rollback('test-migration')
    assert migrations.resolve(job['app_id'], 'web', spec, source) == source


def test_migration_refuses_replaced_source_and_unrelated_target(tmp_path, monkeypatch):
    _, _, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    with pytest.raises(DeployError):
        migrations.resolve(job['app_id'], 'web', spec, {**source, 'instance': 'f' * 32})
    with pytest.raises(DeployError):
        migrations.prepare(job['id'], 'web', source, {**target, 'storage_id': 'f' * 64},
                           operation_id='wrong-target')


def test_managed_storage_reuses_target_with_legacy_manifest(tmp_path, monkeypatch):
    settings, _, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    class Helper:
        def call(self, op, **kw):
            assert kw['expected'] == target
            return {'reference': target, 'identity': {'app_id': job['app_id'], 'service': 'web', 'name': spec['name']},
                    'path': '/quota/data', 'state': 'ready'}
    storage = ManagedStorage(lambda *a, **kw: pytest.fail('legacy Docker volume must not be started'),
                             settings, quota=Helper())
    assert storage.ensure(job['app_id'], 'web', spec, source) == target
    assert storage.mount_argument(job, 'web') == 'type=bind,source=/quota/data,destination=/data,bind-propagation=rprivate'
    with pytest.raises(DeployError) as error:
        ManagedStorage(None, replace(settings, storage_backend='local'), quota=Helper()).ensure(
            job['app_id'], 'web', spec, source)
    assert error.value.code == 'QUOTA_BACKEND_REQUIRED'


def test_deletion_keeps_proven_source_and_target_once_without_changing_history(tmp_path, monkeypatch):
    _, store, job, _, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    with store._connection() as db:
        app = dict(db.execute('SELECT * FROM apps WHERE id=?', (job['app_id'],)).fetchone())
        metadata = store._deletion_metadata(db, app)
    assert len(metadata['volumes']) == 1
    assert metadata['volumes'][0]['expected'] == target
    assert metadata['migration_sources'][0]['expected'] == source
    assert metadata['migration_sources'][0]['target'] == target
    assert store.get(job['id'])['execution']['storage_volumes']['web'] == source


def test_deletion_metadata_validates_retained_source_against_durable_association(tmp_path, monkeypatch):
    from awesome_deploy.offline_data import OfflineDataDeletion
    settings, store, job, _, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    with store._connection() as db:
        app = dict(db.execute('SELECT * FROM apps WHERE id=?', (job['app_id'],)).fetchone())
        metadata = store._deletion_metadata(db, app)
    deletion = OfflineDataDeletion(settings, None)
    deletion._metadata(metadata)
    assert [item['expected'] for item in deletion._volumes(metadata)] == [target, source]
    metadata['migration_sources'][0]['expected']['instance'] = 'f' * 32
    with pytest.raises(DeployError):
        deletion._metadata(metadata)


def test_source_removal_is_recorded_and_not_reintroduced_in_later_deletion(tmp_path, monkeypatch):
    _, store, job, _, source, _, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    migrations.source_removed('test-migration', source)
    with store._connection() as db:
        app = dict(db.execute('SELECT * FROM apps WHERE id=?', (job['app_id'],)).fetchone())
        assert 'migration_sources' not in store._deletion_metadata(db, app)
    with pytest.raises(DeployError):
        migrations.rollback('test-migration')


def test_guard_marks_write_intent_before_yielding_start_permission(tmp_path, monkeypatch):
    settings, _, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    class Helper:
        @contextmanager
        def session(self, op, **kwargs):
            assert kwargs['items'][0]['expected'] == target
            from types import SimpleNamespace
            yield SimpleNamespace(result={'records': [{'reference': target, 'path': '/quota/data',
                'identity': {'app_id': job['app_id'], 'service': 'web', 'name': spec['name']}}]})
    storage = ManagedStorage(None, settings, quota=Helper())
    row = {'Id': 'd' * 64, 'Name': '/awesome-' + job['id'] + '-web', 'Mounts': [
        {'Type': 'bind', 'Source': '/quota/data', 'Destination': '/data', 'RW': True}]}
    with storage.guard(job, 'web', row):
        with pytest.raises(DeployError) as error:
            migrations.rollback('test-migration')
        assert error.value.code == 'STORAGE_MIGRATION_WRITES_STARTED'
    assert job['execution']['storage_volumes']['web'] == source


@pytest.mark.parametrize('method,internal', [('start', '_start'), ('recover_active', '_recover_active')])
def test_runtime_entrypoints_resolve_old_references_on_a_copy(tmp_path, monkeypatch, method, internal):
    from awesome_deploy.runtime import DeploymentBackend
    settings, _, job, _, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    backend = DeploymentBackend(settings)
    monkeypatch.setattr(backend, internal, lambda resolved, checkpoint: resolved['execution']['storage_volumes']['web'])
    assert getattr(backend, method)(job, lambda delta: None) == target
    assert job['execution']['storage_volumes']['web'] == source


def test_prestart_checkpoint_preserves_source_and_rollback_blocks_target(tmp_path, monkeypatch):
    from awesome_deploy.runtime import DeploymentBackend
    from awesome_deploy.quota_client import QuotaClient
    from awesome_deploy.storage_migration import resolve_job
    settings, store, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    monkeypatch.setattr(ManagedStorage, 'ensure', lambda *a, **kw: target)
    monkeypatch.setattr(QuotaClient, 'call', lambda *a, **kw: {'enabled': True})
    candidate = resolve_job(settings, job)
    DeploymentBackend(settings).prepare_storage(candidate, lambda patch: store.checkpoint(job['id'], execution=patch))
    assert store.get(job['id'])['execution']['storage_volumes']['web'] == source
    assert candidate['execution']['storage_volumes']['web'] == target
    migrations.rollback('test-migration')
    with pytest.raises(DeployError):
        migrations.resolve(job['app_id'], 'web', spec, target)


def test_recreated_source_after_removal_blocks_data_deletion(tmp_path, monkeypatch):
    from awesome_deploy.offline_data import OfflineDataDeletion
    settings, store, job, _, source, _, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    migrations.source_removed('test-migration', source)
    with store._connection() as db:
        app = dict(db.execute('SELECT * FROM apps WHERE id=?', (job['app_id'],)).fetchone())
        metadata = store._deletion_metadata(db, app)
    deletion = OfflineDataDeletion(settings, lambda *a, **kw: source['name'])
    monkeypatch.setattr(deletion.storage, 'inventory_record', lambda *a, **kw: {})
    with pytest.raises(DeployError) as error:
        deletion._check_volumes(metadata, unused=False)
    assert error.value.code == 'OFFLINE_DATA_CONFLICT'


def test_operator_resume_refuses_old_release_after_normal_publication(tmp_path, monkeypatch):
    from pathlib import Path
    import runpy
    _, store, job, _, _, _, migrations, _ = setup_migration(tmp_path, monkeypatch)
    commit(migrations)
    operator = runpy.run_path(str(Path(__file__).parents[1] / 'ops' / 'migrate-legacy-storage.py'))
    saved = operator['operation'](migrations, 'test-migration')
    operator['validate_release'](migrations, job['id'], saved)
    with store._connection(write=True) as db:
        db.execute('UPDATE apps SET active_release_id=? WHERE id=?', ('other-release', job['app_id']))
    with pytest.raises(DeployError):
        operator['validate_release'](migrations, job['id'], saved)


def test_operator_inspects_exact_prepared_target_without_business_admission(tmp_path, monkeypatch):
    from pathlib import Path
    import runpy
    settings, _, job, spec, _, target, _, _ = setup_migration(tmp_path, monkeypatch)
    class Helper:
        def call(self, op, **kwargs):
            assert op == 'inspect' and kwargs['expected'] == target
            return {'reference': target, 'path': '/quota/data', 'state': 'ready',
                    'identity': {'app_id': job['app_id'], 'service': 'web', 'name': spec['name']}}
    storage = ManagedStorage(None, settings, quota=Helper())
    with pytest.raises(DeployError):
        storage.inspect(job['app_id'], 'web', spec, target)
    operator = runpy.run_path(str(Path(__file__).parents[1] / 'ops' / 'migrate-legacy-storage.py'))
    assert operator['inspect_target'](storage, job['app_id'], 'web', spec, target)['reference'] == target


def test_rolled_back_source_can_be_migrated_to_a_fresh_generation(tmp_path, monkeypatch):
    _, _, job, spec, source, target, migrations, _ = setup_migration(tmp_path, monkeypatch)
    migrations.rollback('test-migration')
    fresh = {**target, 'generation': 'e' * 32}
    migrations.prepare(job['id'], 'web', source, fresh, operation_id='fresh-migration')
    migrations.commit('fresh-migration', source_digest='c' * 64, target_digest='c' * 64,
                      stopped_container_ids=['d' * 64])
    assert migrations.resolve(job['app_id'], 'web', spec, source) == fresh
    with pytest.raises(DeployError):
        migrations.resolve(job['app_id'], 'web', spec, target)


@pytest.mark.parametrize('declared,expected', [
    ({'web': 512}, {'web': 500}),
    ({'api': 512, 'web': 512}, {'api': 250, 'web': 250}),
    ({'api': 128, 'web': 128}, {'api': 128, 'web': 128}),
    ({'api': 512, 'worker': 512, 'web': 512}, {'api': 167, 'web': 167, 'worker': 166}),
    ({'api': 32, 'web': 6000}, {'api': 6, 'web': 494}),
    ({'api': 32, 'web': 32, 'worker': 1000000}, {'api': 6, 'web': 6, 'worker': 488}),
])
def test_historical_runtime_memory_uses_application_cap_without_mutating_snapshot(tmp_path, declared, expected):
    from awesome_deploy.runtime import DeploymentBackend
    settings = Settings(state_dir=tmp_path, max_app_memory_mib=500)
    job = {'snapshot': {'manifest': {'services': {
        name: {'resources': {'cpu': 0.5, 'memoryMiB': amount}} for name, amount in declared.items()}}}}
    before = copy.deepcopy(job)
    actual = DeploymentBackend(settings)._runtime_resources(job)
    assert {name: limits['memoryMiB'] for name, limits in actual.items()} == expected
    assert job == before


def test_created_historical_container_has_equal_memory_swap_and_no_reservation(tmp_path):
    from test_runtime import backend, job
    engine, record = backend(tmp_path), job(tmp_path)
    service = record['snapshot']['manifest']['services']['web']
    assert service['resources']['memoryMiB'] == 512
    engine._create_container(record, 'web', service, {}, ['network'])
    args = next(args for args in engine.calls if args[:2] == ['container', 'create'])
    assert args[args.index('--memory') + 1] == '500m'
    assert args[args.index('--memory-swap') + 1] == '500m'
    assert '--memory-reservation' not in args
    assert service['resources']['memoryMiB'] == 512


def test_runtime_memory_refuses_budget_below_docker_minimum(tmp_path):
    from awesome_deploy.runtime import DeploymentBackend
    job = {'snapshot': {'manifest': {'services': {
        name: {'resources': {'cpu': 0.5, 'memoryMiB': 512}} for name in ('api', 'web')}}}}
    with pytest.raises(DeployError) as error:
        DeploymentBackend(Settings(state_dir=tmp_path, max_app_memory_mib=11))._runtime_resources(job)
    assert error.value.code == 'RESOURCE_QUOTA'
