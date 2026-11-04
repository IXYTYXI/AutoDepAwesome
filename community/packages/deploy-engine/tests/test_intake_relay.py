import json
import sqlite3
import stat

import pytest

from awesome_deploy.intake_relay import IntakeRelay, IntakeError
from awesome_deploy.intake_remote import PlatformRejected


BODY = {'repository': 'https://github.com/org/repo.git', 'ref': 'main', 'subdomain': 'demo'}


class FakePlatform:
    def __init__(self):
        self.available = False
        self.posts = []
        self.job = {'id': 'job-1', 'appId': 'app-1', 'status': 'queued'}

    def post(self, request_id, body):
        self.posts.append((request_id, body))
        if not self.available:
            raise ConnectionError('offline')
        return self.job

    def get(self, job_id):
        return {'id': job_id, 'appId': 'app-1', 'status': 'succeeded'}


class FakeBase:
    def __init__(self):
        self.available = True
        self.records = {}
        self.attempts = 0

    def upsert(self, request_id, fields):
        self.attempts += 1
        if not self.available:
            raise ConnectionError('offline')
        self.records[request_id] = dict(fields)

    def list_records(self):
        return list(self.records.values())


def relay(tmp_path, platform=None, base=None, clock=None):
    return IntakeRelay(tmp_path / 'intake.sqlite3', platform or FakePlatform(),
                       base or FakeBase(), integrity_key=b'test-integrity-key',
                       **({'clock': clock} if clock else {}))


def test_accepted_request_survives_platform_outage_and_reuses_idempotency_key(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    now = [100.0]
    first = relay(tmp_path, platform, base, clock=lambda: now[0])
    accepted = first.accept('source-001', BODY)
    assert accepted['status'] == 'pending' and accepted['requestId'] == 'source-001'
    assert json.loads(base.records['source-001']['请求内容']) == BODY
    assert first.run_once()['pending'] == 1
    assert platform.posts == [('source-001', BODY)]
    platform.available = True
    restarted = relay(tmp_path, platform, base, clock=lambda: now[0])
    assert restarted.run_once()['pending'] == 1
    assert len(platform.posts) == 1
    now[0] += 3
    assert restarted.run_once()['pending'] == 0
    assert platform.posts[-1] == ('source-001', BODY)
    assert restarted.get('source-001')['jobId'] == 'job-1'
    assert restarted.run_once()['pending'] == 0
    assert len(platform.posts) == 2


def test_base_failure_does_not_block_successful_deployment(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    now = [100.0]
    platform.available, base.available = True, False
    worker = relay(tmp_path, platform, base, clock=lambda: now[0])
    assert worker.accept('source-002', BODY)['status'] == 'pending'
    assert worker.run_once()['pending'] == 0
    assert worker.get('source-002')['jobId'] == 'job-1'
    base.available = True
    now[0] += 3
    worker.run_once()
    assert base.records['source-002']['部署 ID'] == 'job-1'


def test_base_retry_has_bounded_backoff(tmp_path):
    now = [100.0]
    base = FakeBase()
    base.available = False
    worker = relay(tmp_path, FakePlatform(), base, clock=lambda: now[0])
    worker.accept('source-base-retry', BODY)
    worker.accept('source-base-retry', BODY)
    assert base.attempts == 1
    now[0] += 3
    worker.accept('source-base-retry', BODY)
    assert base.attempts == 2


def test_duplicate_request_is_stable_and_changed_body_conflicts(tmp_path):
    worker = relay(tmp_path)
    worker.accept('source-003', BODY)
    assert worker.accept('source-003', dict(BODY))['requestId'] == 'source-003'
    with pytest.raises(IntakeError):
        worker.accept('source-003', {**BODY, 'subdomain': 'other'})
    with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM intake_requests').fetchone()[0] == 1


def test_rejects_repository_credentials_before_writing_base(tmp_path):
    base = FakeBase()
    worker = relay(tmp_path, FakePlatform(), base)
    with pytest.raises(IntakeError):
        worker.accept('secret-in-url', {**BODY,
                                       'repository': 'https://user:password@github.com/org/repo.git?token=x'})
    assert base.records == {}


def test_local_request_database_is_private(tmp_path):
    worker = relay(tmp_path)
    worker.accept('source-private', BODY)
    assert stat.S_IMODE((tmp_path / 'intake.sqlite3').stat().st_mode) == 0o600
    (tmp_path / 'intake.sqlite3').chmod(0o644)
    with pytest.raises(IntakeError):
        relay(tmp_path)


def test_database_connections_are_closed_after_requests(tmp_path, monkeypatch):
    connections = []
    original = sqlite3.connect

    class SpyConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        connection = original(*args, factory=SpyConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr('awesome_deploy.intake_relay.sqlite3.connect', connect)
    worker = relay(tmp_path)
    worker.accept('source-closed', BODY)
    worker.get('source-closed')
    assert connections and all(connection.closed for connection in connections)


def test_pending_request_dispatches_before_retrying_base_sync(tmp_path):
    events = []

    class EventPlatform(FakePlatform):
        def post(self, request_id, body):
            events.append('platform')
            return super().post(request_id, body)

    class EventBase(FakeBase):
        def upsert(self, request_id, fields):
            events.append('base')
            return super().upsert(request_id, fields)

    platform, base = EventPlatform(), EventBase()
    platform.available = True
    worker = relay(tmp_path, platform, base)
    worker.accept('source-order', BODY)
    with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
        db.execute('UPDATE intake_requests SET base_sync_digest=NULL')
    events.clear()
    worker.run_once()
    assert events[0] == 'platform'


def test_backing_off_old_request_does_not_starve_fresh_one(tmp_path):
    now = [100.0]
    platform = FakePlatform()
    worker = relay(tmp_path, platform, FakeBase(), clock=lambda: now[0])
    worker.accept('source-a-old', BODY)
    worker.run_once(limit=1)
    worker.accept('source-z-new', {**BODY, 'subdomain': 'new'})
    worker.run_once(limit=1)
    assert platform.posts[-1][0] == 'source-z-new'


def test_backing_off_requests_do_not_starve_due_base_sync(tmp_path):
    now = [100.0]
    platform, base = FakePlatform(), FakeBase()
    worker = relay(tmp_path, platform, base, clock=lambda: now[0])
    worker.accept('source-final', BODY)
    platform.available = True
    worker.run_once()
    assert worker.get('source-final')['status'] == 'succeeded'
    for index in range(101):
        worker.accept(f'source-backoff-{index:03}', {**BODY, 'subdomain': 'b' + str(index)})
    with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
        db.execute("UPDATE intake_requests SET retry_after=1000 WHERE status='pending'")
        db.execute("UPDATE intake_requests SET base_sync_digest=NULL WHERE request_id='source-final'")
    base.records.pop('source-final')
    worker.run_once(limit=1)
    assert base.records['source-final']['状态'] == 'succeeded'


def test_restores_only_signed_request_from_base(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    original = relay(tmp_path, platform, base)
    original.accept('source-004', BODY)
    (tmp_path / 'intake.sqlite3').unlink()
    restored = relay(tmp_path, platform, base)
    assert restored.restore_from_base() == 1
    assert restored.get('source-004')['status'] == 'pending'
    bad = dict(base.records['source-004'])
    bad['请求 ID'] = 'source-evil'
    base.records['source-evil'] = bad
    assert restored.restore_from_base() == 0
    assert restored.get('source-evil') is None


def test_final_rows_do_not_starve_later_pending_requests(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    platform.available = True
    worker = relay(tmp_path, platform, base)
    worker.accept('source-a', BODY)
    worker.run_once(limit=1)
    platform.available = False
    worker.accept('source-b', {**BODY, 'subdomain': 'other'})
    worker.run_once(limit=1)
    assert platform.posts[-1][0] == 'source-b'


def test_platform_retry_has_bounded_backoff_and_safe_error(tmp_path):
    now = [100.0]
    platform = FakePlatform()
    worker = relay(tmp_path, platform, FakeBase(), clock=lambda: now[0])
    worker.accept('source-retry', BODY)
    worker.run_once()
    assert worker.get('source-retry')['error'] == 'delivery_unavailable'
    worker.run_once()
    assert len(platform.posts) == 1
    now[0] += 3
    worker.run_once()
    assert len(platform.posts) == 2


def test_permanently_rejected_submission_is_recorded_once(tmp_path):
    class RejectingPlatform(FakePlatform):
        def post(self, request_id, body):
            self.posts.append((request_id, body))
            raise PlatformRejected(422)

    platform, base = RejectingPlatform(), FakeBase()
    worker = relay(tmp_path, platform, base)
    worker.accept('source-rejected', BODY)
    worker.run_once()
    assert worker.get('source-rejected')['status'] == 'rejected'
    assert worker.get('source-rejected')['error'] == 'platform_http_422'
    assert base.records['source-rejected']['状态'] == 'rejected'
    worker.run_once()
    assert len(platform.posts) == 1
    (tmp_path / 'intake.sqlite3').unlink()
    recovered = relay(tmp_path, platform, base)
    assert recovered.restore_from_base() == 1
    assert recovered.get('source-rejected')['status'] == 'rejected'


def test_inflight_base_write_cannot_acknowledge_a_newer_request_state(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    worker = relay(tmp_path, platform, base, clock=lambda: 100.0)
    platform.available = True
    worker.accept('source-race', BODY)

    original_upsert = base.upsert

    def concurrent_update(request_id, fields):
        if fields['状态'] == 'pending':
            with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
                db.execute("UPDATE intake_requests SET status='submitted',job_id='job-1',"
                           "app_id='app-1',base_sync_digest=NULL WHERE request_id=?", (request_id,))
        original_upsert(request_id, fields)

    base.upsert = concurrent_update
    with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
        db.execute("UPDATE intake_requests SET base_sync_digest=NULL WHERE request_id='source-race'")
    worker._sync('source-race')
    with sqlite3.connect(tmp_path / 'intake.sqlite3') as db:
        assert db.execute("SELECT base_sync_digest FROM intake_requests WHERE request_id='source-race'").fetchone()[0] is None


def test_recovery_of_completed_base_row_does_not_resubmit(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    platform.available = True
    original = relay(tmp_path, platform, base)
    original.accept('source-complete', BODY)
    original.run_once()
    assert original.get('source-complete')['status'] == 'succeeded'
    (tmp_path / 'intake.sqlite3').unlink()
    recovered = relay(tmp_path, platform, base)
    assert recovered.restore_from_base() == 1
    assert recovered.get('source-complete')['status'] == 'succeeded'
    recovered.run_once()
    assert len(platform.posts) == 1


def test_unsynced_final_record_does_not_block_new_request(tmp_path):
    platform, base = FakePlatform(), FakeBase()
    platform.available = True
    worker = relay(tmp_path, platform, base)
    worker.accept('source-old', BODY)
    base.available = False
    worker.run_once(limit=1)
    worker.accept('source-new', {**BODY, 'subdomain': 'new'})
    worker.run_once(limit=1)
    assert platform.posts[-1][0] == 'source-new'
