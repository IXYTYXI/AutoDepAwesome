"""Durable request intake for a separately hosted integration backend.

This module must run outside the deployment server's failure domain. A request is
acknowledged only after a local SQLite commit; Base is a recoverable second copy.
"""

import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import urlsplit

from .intake_remote import PlatformRejected


REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
BODY_FIELDS = {'repository', 'ref', 'subdomain', 'appId'}
FINAL = {'succeeded', 'failed', 'recovery_required', 'rejected'}


class IntakeError(ValueError):
    pass


def _body(value):
    if (not isinstance(value, dict) or set(value) - BODY_FIELDS
            or not {'repository', 'ref', 'subdomain'} <= set(value)
            or any(not isinstance(item, str) or not item or len(item) > 2048 for item in value.values())):
        raise IntakeError('请求内容只能包含 repository、ref、subdomain 和可选 appId。')
    try:
        repository = urlsplit(value['repository'])
        if (repository.scheme != 'https' or not repository.hostname or repository.username is not None
                or repository.password is not None or repository.port is not None or repository.query
                or repository.fragment or not re.fullmatch(r'/[A-Za-z0-9_./-]+', repository.path)
                or any(ord(char) <= 32 for char in value['repository'])
                or '\\' in value['repository']):
            raise ValueError
    except ValueError:
        raise IntakeError('仓库地址必须是无凭证、无查询参数的 HTTPS 地址。') from None
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', value['ref']):
        raise IntakeError('分支或版本格式无效。')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?', value['subdomain']):
        raise IntakeError('域名前缀格式无效。')
    if 'appId' in value and not re.fullmatch(r'[0-9a-f]{32}', value['appId']):
        raise IntakeError('应用 ID 格式无效。')
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode()) > 8192:
        raise IntakeError('请求内容过大。')
    return encoded


class IntakeRelay:
    def __init__(self, database_path: Path, platform, base, *, integrity_key: bytes, clock=time.time):
        if not isinstance(integrity_key, bytes) or len(integrity_key) < 16:
            raise IntakeError('完整性密钥无效。')
        self.path, self.platform, self.base = Path(database_path), platform, base
        self.key, self.clock = integrity_key, clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise IntakeError('请求数据库目录必须由运行账号独占。')
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise IntakeError('请求数据库文件必须由运行账号独占且权限为 0600。')
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS intake_requests (
                request_id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL,
                job_id TEXT, app_id TEXT, error TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, base_sync_digest TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL NOT NULL DEFAULT 0,
                base_attempts INTEGER NOT NULL DEFAULT 0,
                base_retry_after REAL NOT NULL DEFAULT 0)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _signature(self, fields):
        signed = {key: fields[key] for key in ('请求 ID', '请求内容', '状态', '部署 ID', '应用 ID')}
        encoded = json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hmac.new(self.key, encoded.encode(), hashlib.sha256).hexdigest()

    def _fields(self, row):
        fields = {'请求 ID': row['request_id'], '请求内容': row['body'],
                '状态': row['status'], '部署 ID': row['job_id'] or '',
                '应用 ID': row['app_id'] or '',
                '提交时间': int(row['created_at'] * 1000),
                '更新时间': int(row['updated_at'] * 1000)}
        fields['完整性签名'] = self._signature(fields)
        return fields

    def _sync(self, request_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM intake_requests WHERE request_id=?', (request_id,)).fetchone()
            fields = self._fields(row)
            digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if row['base_sync_digest'] == digest:
                return
            if row['base_retry_after'] > self.clock():
                return
        try:
            self.base.upsert(request_id, fields)
        except Exception:
            attempts = row['base_attempts'] + 1
            with self._db() as db:
                db.execute('UPDATE intake_requests SET base_attempts=?,base_retry_after=? '
                           "WHERE request_id=? AND updated_at=? AND status=? "
                           "AND COALESCE(job_id,'')=? AND COALESCE(app_id,'')=?",
                           (attempts, self.clock() + min(3600, 2 ** min(attempts, 10)),
                            request_id, row['updated_at'], row['status'],
                            row['job_id'] or '', row['app_id'] or ''))
            return  # The local committed request remains dispatchable.
        with self._db() as db:
            db.execute('UPDATE intake_requests SET base_sync_digest=?,base_attempts=0,base_retry_after=0 '
                       "WHERE request_id=? AND updated_at=? AND status=? "
                       "AND COALESCE(job_id,'')=? AND COALESCE(app_id,'')=?",
                       (digest, request_id, row['updated_at'], row['status'],
                        row['job_id'] or '', row['app_id'] or ''))

    def accept(self, request_id: str, body: dict):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise IntakeError('请求 ID 必须是 1–128 位安全字符。')
        encoded = _body(body)
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT body FROM intake_requests WHERE request_id=?', (request_id,)).fetchone()
            if old and old['body'] != encoded:
                raise IntakeError('相同请求 ID 对应不同部署内容。')
            if not old:
                db.execute('INSERT INTO intake_requests(request_id,body,status,created_at,updated_at) '
                           'VALUES (?,?,?,?,?)', (request_id, encoded, 'pending', now, now))
        self._sync(request_id)  # Attempt a synchronous second copy; never gate deployment.
        return self.get(request_id)

    def get(self, request_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM intake_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        return {'requestId': row['request_id'], 'status': row['status'],
                'jobId': row['job_id'], 'appId': row['app_id'], 'error': row['error']}

    def run_once(self, *, limit=100):
        with self._db() as db:
            rows = db.execute("SELECT request_id,body,status,job_id,attempts,retry_after FROM intake_requests "
                              "WHERE (status IN ('pending','submitted') AND retry_after<=?) "
                              "OR (base_sync_digest IS NULL AND base_retry_after<=?) "
                              "ORDER BY CASE WHEN status IN ('pending','submitted') AND retry_after<=? "
                              "THEN 0 ELSE 1 END, created_at,request_id LIMIT ?",
                              (self.clock(), self.clock(), self.clock(), limit)).fetchall()
        for row in rows:
            request_id = row['request_id']
            if row['status'] in FINAL:
                self._sync(request_id)
                continue
            if row['retry_after'] > self.clock():
                self._sync(request_id)
                continue
            try:
                if row['status'] == 'pending':
                    response = self.platform.post(request_id, json.loads(row['body']))
                    if not isinstance(response, dict) or not all(
                            isinstance(response.get(key), str) and response[key]
                            for key in ('id', 'appId', 'status')):
                        raise ValueError('平台响应不完整')
                    with self._db() as db:
                        db.execute('UPDATE intake_requests SET status=?,job_id=?,app_id=?,updated_at=?, '
                                   'base_sync_digest=NULL,attempts=0,retry_after=0,error=NULL '
                                   'WHERE request_id=? AND status=?',
                                   ('submitted', response['id'], response['appId'], self.clock(),
                                    request_id, 'pending'))
                    job_id = response['id']
                elif row['status'] == 'submitted':
                    job_id = row['job_id']
                else:
                    continue
                status = self.platform.get(job_id)
                state = status.get('status') if isinstance(status, dict) else None
                if state in FINAL:
                    with self._db() as db:
                        db.execute('UPDATE intake_requests SET status=?,updated_at=?,base_sync_digest=NULL '
                                   'WHERE request_id=? '
                                   'AND status=?', (state, self.clock(), request_id, 'submitted'))
            except PlatformRejected as exc:
                with self._db() as db:
                    db.execute('UPDATE intake_requests SET status=?,updated_at=?,base_sync_digest=NULL, '
                               'retry_after=0,error=? WHERE request_id=? AND status=?',
                               ('rejected', self.clock(), f'platform_http_{exc.status_code}',
                                request_id, 'pending'))
                self._sync(request_id)
                continue
            except Exception:
                attempts = row['attempts'] + 1
                with self._db() as db:
                    db.execute('UPDATE intake_requests SET attempts=?,retry_after=?,error=?, '
                               'base_sync_digest=NULL WHERE request_id=?',
                               (attempts, self.clock() + min(3600, 2 ** min(attempts, 10)),
                                'delivery_unavailable', request_id))
                self._sync(request_id)
                continue  # Retry from the durable row with the same request ID.
            self._sync(request_id)
        with self._db() as db:
            pending = db.execute("SELECT count(*) FROM intake_requests WHERE status='pending'").fetchone()[0]
        return {'pending': pending}

    def restore_from_base(self):
        imported = 0
        for fields in self.base.list_records():
            request_id, raw, signature = (fields.get('请求 ID'), fields.get('请求内容'),
                                          fields.get('完整性签名'))
            if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                continue
            if not isinstance(raw, str) or not isinstance(signature, str):
                continue
            try:
                encoded = _body(json.loads(raw))
            except (ValueError, TypeError):
                continue
            status = fields.get('状态')
            job_id, app_id = fields.get('部署 ID') or '', fields.get('应用 ID') or ''
            if (raw != encoded or status not in {'pending', 'submitted'} | FINAL
                    or not isinstance(job_id, str) or not isinstance(app_id, str)
                    or (status not in {'pending', 'rejected'} and (not job_id or not app_id))
                    or (status in {'pending', 'rejected'} and (job_id or app_id))):
                continue
            signed = {'请求 ID': request_id, '请求内容': raw, '状态': status,
                      '部署 ID': job_id, '应用 ID': app_id}
            if not hmac.compare_digest(signature, self._signature(signed)):
                continue
            with self._db() as db:
                cursor = db.execute('INSERT OR IGNORE INTO intake_requests '
                                    '(request_id,body,status,job_id,app_id,created_at,updated_at) '
                                    'VALUES (?,?,?,?,?,?,?)',
                                    (request_id, raw, status, job_id or None, app_id or None,
                                     self.clock(), self.clock()))
                imported += cursor.rowcount
        return imported
