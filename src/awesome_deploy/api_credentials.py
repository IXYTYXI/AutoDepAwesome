"""Explicit, narrowly scoped credentials for unattended deployment API callers."""

from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import time
import uuid

from .access import AccessStore, denied
from .errors import DeployError


TOKEN = re.compile(r'adp_[A-Za-z0-9_-]{43}\Z')
NAME = re.compile(r'[\w .:/-]{1,80}\Z', re.UNICODE)


class ApiCredentialStore:
    def __init__(self, settings, *, clock=time.time):
        self.settings, self.clock = settings, clock
        with self._connection() as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS api_credentials (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                owner_user_id TEXT NOT NULL REFERENCES auth_users(id),
                owner_revision INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                app_id TEXT REFERENCES apps(id), allow_create INTEGER NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                revoked_at REAL,
                CHECK (kind IN ('person','source')),
                CHECK ((app_id IS NULL AND allow_create=1) OR (app_id IS NOT NULL AND allow_create=0))
            );
            CREATE INDEX IF NOT EXISTS api_credentials_owner ON api_credentials(owner_user_id,created_at);
        ''')
        if 'allow_any_app' not in {row[1] for row in db.execute('PRAGMA table_info(api_credentials)')}:
            db.execute('ALTER TABLE api_credentials ADD COLUMN allow_any_app INTEGER NOT NULL DEFAULT 0')
        if 'self_managed' not in {row[1] for row in db.execute('PRAGMA table_info(api_credentials)')}:
            db.execute('ALTER TABLE api_credentials ADD COLUMN self_managed INTEGER NOT NULL DEFAULT 0')

        if 'delegation_source' not in {row[1] for row in db.execute('PRAGMA table_info(api_credentials)')}:
            db.execute('ALTER TABLE api_credentials ADD COLUMN delegation_source TEXT')

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.settings.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def issue(self, owner_user_id, name, *, kind, app_id=None, allow_create=False,
              allow_any_app=False, self_managed=False, expires_days=30, delegation_source=None):
        if (self.settings.auth_mode != 'feishu' or kind not in {'person', 'source'}
                or not isinstance(name, str) or not NAME.fullmatch(name)
                or type(expires_days) is not int or not 1 <= expires_days <= 90
                or type(allow_create) is not bool or type(allow_any_app) is not bool
                or type(self_managed) is not bool or self_managed and kind != 'person'
                or sum((app_id is not None, allow_create, allow_any_app)) != 1
                or app_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', app_id)):
            raise DeployError('INVALID_API_CREDENTIAL', 'API 凭证名称、范围或有效期无效。')
        if delegation_source is not None and (kind != 'source' or self_managed
                or not allow_any_app or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', delegation_source)):
            raise DeployError('INVALID_API_CREDENTIAL', '委托来源必须使用具名来源凭证和所有应用范围。')
        token = 'adp_' + secrets.token_urlsafe(32)
        now, credential_id = self.clock(), uuid.uuid4().hex
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM auth_users WHERE id=?', (owner_user_id,)).fetchone()
            if not row:
                raise denied()
            actor = AccessStore._actor(db, dict(row))
            if (actor['app_id'] != self.settings.feishu_app_id
                    or actor['tenant_key'] != self.settings.feishu_tenant_key or not actor['can_deploy']):
                raise denied()
            if app_id is not None:
                AccessStore._require(db, actor, app_id, 'deploy')
            db.execute('''INSERT INTO api_credentials
                (id,token_hash,owner_user_id,owner_revision,kind,name,app_id,allow_create,allow_any_app,self_managed,created_at,expires_at,delegation_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (credential_id, hashlib.sha256(token.encode()).hexdigest(), actor['id'], actor['revision'],
                 kind, name, app_id, int(allow_create or allow_any_app), int(allow_any_app), int(self_managed),
                 now, now + expires_days * 86400, delegation_source))
            db.execute('''INSERT INTO auth_audit(actor_id,action,target_id,result,details,created_at)
                VALUES (?,'api_credential_issued',?,'success','{}',?)''', (actor['id'], credential_id, now))
        return {'id': credential_id, 'token': token, 'ownerUserId': owner_user_id, 'kind': kind,
                'delegationSource': delegation_source, 'name': name, 'appId': app_id, 'allowCreate': bool(allow_create),
                'allowAnyApp': bool(allow_any_app), 'selfManaged': bool(self_managed),
                'expiresAt': now + expires_days * 86400}

    def authenticate(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        now = self.clock()
        with self._connection() as db:
            row = db.execute('SELECT * FROM api_credentials WHERE token_hash=?',
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not row or row['revoked_at'] is not None or not row['created_at'] <= now < row['expires_at']:
                return None
            user = db.execute('SELECT * FROM auth_users WHERE id=?', (row['owner_user_id'],)).fetchone()
            if (not user or user['revision'] != row['owner_revision']
                    or not user['enabled'] or not user['eligible'] or not user['can_deploy']
                    or user['app_id'] != self.settings.feishu_app_id
                    or user['tenant_key'] != self.settings.feishu_tenant_key):
                return None
            return dict(user), dict(row)

    @staticmethod
    def authorize_scope(db, actor, credential_id, app_id, *, delegated=False):
        row = db.execute('SELECT * FROM api_credentials WHERE id=?', (credential_id,)).fetchone()
        now = time.time()
        if (not row or row['revoked_at'] is not None or not row['created_at'] <= now < row['expires_at']
                or not delegated and row['delegation_source'] is not None
                or not delegated and (row['owner_user_id'] != actor['id']
                                      or row['owner_revision'] != actor['revision'])):
            raise denied()
        if delegated:
            ApiCredentialStore.require_delegation(db, row)
        if app_id is None:
            allowed = row['allow_any_app'] or row['allow_create']
        else:
            allowed = row['allow_any_app'] or row['app_id'] == app_id
        if not allowed:
            raise denied()
        return dict(row)

    @staticmethod
    def require_delegation(db, row):
        if not row or row['kind'] != 'source' or not row['delegation_source'] or not row['allow_any_app']:
            raise denied()
        owner = db.execute('SELECT * FROM auth_users WHERE id=?', (row['owner_user_id'],)).fetchone()
        if (not owner or not owner['enabled'] or not owner['eligible'] or not owner['can_deploy']
                or owner['revision'] != row['owner_revision']):
            raise denied()
        return dict(owner)

    def list(self, *, owner_user_id=None):
        with self._connection() as db:
            query = ("SELECT * FROM api_credentials WHERE owner_user_id=? AND self_managed=1 "
                     'ORDER BY created_at DESC,id DESC' if owner_user_id is not None else
                     'SELECT * FROM api_credentials ORDER BY created_at DESC,id DESC')
            return [dict(id=row['id'], ownerUserId=row['owner_user_id'], kind=row['kind'], name=row['name'], delegationSource=row['delegation_source'],
                         appId=row['app_id'], allowCreate=bool(row['allow_create'] and not row['allow_any_app']),
                         allowAnyApp=bool(row['allow_any_app']), selfManaged=bool(row['self_managed']),
                         createdAt=row['created_at'], expiresAt=row['expires_at'],
                         revokedAt=row['revoked_at'])
                    for row in db.execute(query, (owner_user_id,) if owner_user_id is not None else ())]

    def revoke(self, credential_id, *, actor=None):
        now = self.clock()
        with self._connection(write=True) as db:
            row = db.execute('SELECT owner_user_id,self_managed,revoked_at FROM api_credentials WHERE id=?',
                             (credential_id,)).fetchone()
            if row is None:
                raise DeployError('NOT_FOUND', 'API 凭证不存在。')
            if actor is not None:
                fresh = AccessStore._actor(db, actor)
                if fresh['id'] != row['owner_user_id'] or not row['self_managed']:
                    raise denied()
            if row['revoked_at'] is None:
                db.execute('UPDATE api_credentials SET revoked_at=? WHERE id=?', (now, credential_id))
                db.execute('''INSERT INTO auth_audit(actor_id,action,target_id,result,details,created_at)
                    VALUES (?,'api_credential_revoked',?,'success','{}',?)''',
                    (row['owner_user_id'], credential_id, now))
