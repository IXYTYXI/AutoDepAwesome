"""A-side durable outbox. Call accept only from an authenticated server handler.

The existing IntakeRelay owns restrictive SQLite permissions and storage helpers.
This component deliberately does not require Feishu to receive a request.
"""
import json
import time

from .intake_relay import IntakeRelay
from .intake_remote import PlatformRejected
from .team_sessions import canonical, deployment_body, request_key, SessionError


class TeamOutbox(IntakeRelay):
    def __init__(self, path, sessions, platform, *, clock=time.time):
        super().__init__(path, platform, None, integrity_key=sessions.key, clock=clock)
        self.sessions = sessions
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS team_requests (
                request_id TEXT PRIMARY KEY, deployment TEXT NOT NULL, receipt TEXT NOT NULL,
                status TEXT NOT NULL, job_id TEXT, app_id TEXT, error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            if 'result' not in {r[1] for r in db.execute('PRAGMA table_info(team_requests)')}:
                db.execute('ALTER TABLE team_requests ADD COLUMN result TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS team_requests_pending ON team_requests(status,retry_after)')

    def accept(self, request_id, deployment, session_id):
        request_key(request_id)
        deployment_body(deployment)
        receipt = self.sessions.admit(session_id, request_id, deployment)
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM team_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                old_identity = json.loads(row['receipt'])['identity']
                if row['deployment'] != canonical(deployment) or any(
                        old_identity[k] != receipt['identity'][k] for k in ('appId', 'tenantKey', 'openId')):
                    raise SessionError('相同请求 ID 已属于其他申请人或请求内容。')
            else:
                db.execute('''INSERT INTO team_requests
                    (request_id,deployment,receipt,status,created_at,updated_at) VALUES (?,?,?,'pending',?,?)''',
                    (request_id, canonical(deployment), canonical(receipt), now, now))
        return self.get(request_id)

    def get(self, request_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM team_requests WHERE request_id=?', (request_key(request_id),)).fetchone()
            if not row:
                return None
            return dict(requestId=row['request_id'], status=row['status'], jobId=row['job_id'],
                        appId=row['app_id'], error=row['error'],
                        deployment=json.loads(row['result']) if row['result'] else None)

    def run_once(self):
        # Multiple processes can race; B's source-scoped idempotency prevents duplicate jobs.
        # Conditional updates prevent a late response from reverting a terminal state.
        with self._db() as db:
            row = db.execute("SELECT * FROM team_requests WHERE status IN ('pending','submitted') "
                             'AND retry_after<=? ORDER BY retry_after,created_at,request_id LIMIT 1',
                             (self.clock(),)).fetchone()
        if not row:
            return False
        status, job_id, app_id, error = row['status'], row['job_id'], row['app_id'], None
        delay = 5
        result = json.loads(row['result']) if row['result'] else None
        try:
            if status == 'pending':
                result = self.platform.post(row['request_id'],
                    {'deployment': json.loads(row['deployment']), 'receipt': json.loads(row['receipt'])})
                if (not isinstance(result.get('id'), str) or not result['id']
                        or not isinstance(result.get('appId'), str) or not result['appId']):
                    raise ConnectionError('invalid receipt')
                job_id, app_id, status = result['id'], result['appId'], 'submitted'
            else:
                result = self.platform.get(job_id)
                if result.get('id') != job_id:
                    raise ConnectionError('invalid task')
                if result.get('status') in ('succeeded', 'failed', 'recovery_required', 'cancelled'):
                    status = result['status']
        except PlatformRejected as exc:
            status, error = 'rejected', f'平台拒绝请求 HTTP {exc.status_code}'
        except Exception:
            error = '平台暂不可用或凭证需维护；请求已保存，将使用原请求 ID 重试。'
            delay = min(300, 2 ** min(row['attempts'] + 1, 8))
        with self._db() as db:
            db.execute('''UPDATE team_requests SET status=?,job_id=?,app_id=?,error=?,attempts=attempts+1,
                retry_after=?,updated_at=?,result=? WHERE request_id=? AND status=? AND attempts=?''',
                (status, job_id, app_id, error, self.clock() + delay, self.clock(), canonical(result) if result is not None else None, row['request_id'],
                 row['status'], row['attempts']))
        return True
