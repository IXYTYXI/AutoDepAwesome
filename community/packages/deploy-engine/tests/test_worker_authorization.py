import sqlite3

import pytest

from awesome_deploy.access import AccessStore
from awesome_deploy.worker import Worker
from test_access import user
from test_worker import setup as base_setup, payload
from test_storage_worker import enable_storage


@pytest.fixture
def setup(tmp_path, monkeypatch):
    result = base_setup.__wrapped__(tmp_path)
    # Identity remote checks are independently tested by identity/provider suites.
    monkeypatch.setattr(AccessStore, 'authorize_operation',
                        lambda self, kind, oid, force=False: self.authorize_operation_local(kind, oid))
    return result


def revoke(settings, actor):
    with sqlite3.connect(settings.database_path) as db:
        db.execute('UPDATE auth_users SET enabled=0,revision=revision+1 WHERE id=?', (actor['id'],))


def run(setup):
    settings, store, _, source, backend = setup
    return Worker(settings, store, source, backend).run_once()


def test_revoked_queued_deployment_never_prepares_or_starts(setup):
    settings, store, calls, _, _ = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    revoke(settings, actor)
    assert run(setup)
    assert 'prepare' not in calls and 'start' not in calls
    assert store.get(job['id'])['error']['code'] == 'ACCESS_DENIED'


def test_revocation_after_build_does_not_stop_old_storage_release(setup):
    settings, store, calls, _, backend = setup
    actor = user(settings)
    enable_storage(setup)
    first = store.enqueue(payload(), 'first', actor=actor)
    run(setup)
    first = store.get(first['id'])
    second = store.enqueue(payload(first['app_id']), 'second', actor=actor)
    build = backend.build
    def revoke_after_build(job, checkpoint):
        result = build(job, checkpoint)
        revoke(settings, actor)
        return result
    backend.build = revoke_after_build
    calls.clear()
    run(setup)
    assert 'stop_previous' not in calls and 'start' not in calls
    assert store.active_release(first['app_id'])['id'] == first['id']
    assert store.get(second['id'])['error']['code'] == 'ACCESS_DENIED'


def test_revocation_before_publish_cannot_activate_candidate(setup):
    settings, store, calls, _, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    backend.ensure_https = lambda job: revoke(settings, actor)
    run(setup)
    assert 'publish' not in calls
    assert store.get(job['id'])['status'] == 'failed'


def test_revocation_during_verify_rolls_back_candidate_before_success(setup):
    settings, store, calls, _, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    backend.verify = lambda job: revoke(settings, actor)
    run(setup)
    assert 'publish' in calls and 'rollback' in calls
    assert store.get(job['id'])['status'] == 'failed'


def test_successful_release_recovery_does_not_depend_on_submitter(setup):
    settings, store, calls, _, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    run(setup)
    revoke(settings, actor)
    backend.boot_id = 'next-boot'
    calls.clear()
    run(setup)
    assert 'recover_active' in calls
    assert store.get(job['id'])['status'] == 'succeeded'


def test_public_only_application_never_inherits_global_repository_credentials(setup, monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    from awesome_deploy.source import GitSource
    settings, store, calls, source, backend = setup
    settings = replace(settings, git_askpass=Path('/operator-only/askpass'))
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    seen = []
    def prepare(self, job, checkpoint):
        seen.append((self.settings.git_askpass, self.lock_fd))
        return source.prepare(job, checkpoint)
    monkeypatch.setattr(GitSource, 'prepare', prepare)
    runner = Worker(settings, store, GitSource(settings), backend)
    runner.run_once()
    assert seen[0][0] is None and isinstance(seen[0][1], int)
    assert store.get(job['id'])['status'] == 'succeeded'


def test_revoked_offline_before_first_mutation_keeps_release_online(setup):
    settings, store, calls, source, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    run(setup)
    job = store.get(job['id'])
    operation = store.enqueue_offline(job['app_id'], job['id'], 'stop', actor=actor)
    revoke(settings, actor)
    backend.offline = lambda *args: pytest.fail('Revoked offline must not reach runtime')
    run(setup)
    assert store.get_offline(operation['id'])['status'] == 'failed'
    assert store.active_release(job['app_id'])['id'] == job['id']
    assert store.next_offline() is None


def test_started_offline_crash_finishes_safely_after_submitter_revoked(setup):
    from test_worker import Crash
    settings, store, calls, source, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    run(setup)
    job = store.get(job['id'])
    operation = store.enqueue_offline(job['app_id'], job['id'], 'stop', actor=actor)
    def crash(job, checkpoint):
        checkpoint({'gateway_deactivated': True})
        raise Crash('offline')
    backend.offline = crash
    with pytest.raises(Crash):
        run(setup)
    assert store.get_offline(operation['id'])['execution']['authorization_started']
    revoke(settings, actor)
    def complete(job, checkpoint):
        checkpoint({'gateway_deactivated': True, 'containers_stopped': True, 'cleanup_confirmed': True})
    backend.offline = complete
    assert run(setup)
    assert store.get_offline(operation['id'])['status'] == 'succeeded'
    assert store.active_release(job['app_id']) is None


def test_original_offline_retry_key_is_not_hashed_twice_or_shared(setup):
    settings, store, calls, _, _ = setup
    owner, admin = user(settings), user(settings, admin=True)
    job = store.enqueue(payload(), 'first', actor=owner)
    run(setup)
    job = store.get(job['id'])
    operation = store.enqueue_offline(job['app_id'], job['id'], 'original-key', actor=owner)
    access = AccessStore(settings)
    assert access.retry_key(owner, operation) == 'original-key'
    assert access.retry_key(admin, operation) is None
    retry = store.enqueue_offline(job['app_id'], job['id'], access.retry_key(owner, operation), actor=owner)
    assert retry['id'] == operation['id']


def test_legacy_destructive_offline_journal_is_not_cancelled_during_auth_upgrade(setup):
    from dataclasses import replace
    settings, store, calls, source, backend = setup
    actor = user(settings)
    job = store.enqueue(payload(), 'first', actor=actor)
    run(setup)
    job = store.get(job['id'])
    operation = store.enqueue_offline(job['app_id'], job['id'], 'stop')
    store.claim_offline(operation['id'])
    store.checkpoint_offline(operation['id'], {'deactivate_intent': True})
    upgraded = replace(settings, auth_mode='feishu')
    upgraded_store = type(store)(upgraded)
    def complete(job, checkpoint):
        checkpoint({'gateway_deactivated': True, 'containers_stopped': True, 'cleanup_confirmed': True})
    backend.offline = complete
    assert Worker(upgraded, upgraded_store, source, backend).run_once()
    assert store.get_offline(operation['id'])['status'] == 'succeeded'
