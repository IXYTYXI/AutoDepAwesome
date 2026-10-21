"""Application ownership, durable authorization and safe read projections.

Identity and membership epochs are captured with each accepted operation. An old
operation never gains permission from a later grant, including remove/add cycles.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time

from .errors import DeployError


def denied():
    return DeployError('ACCESS_DENIED', 'You do not have permission for this operation.')


def offline_started(operation):
    # Pre-authentication versions already journaled destructive intents. Those
    # journals must finish safely after an upgrade, even without a user binding.
    return any(operation.get('execution', {}).get(key) is True for key in (
        'authorization_started', 'deactivate_intent', 'gateway_deactivated',
        'stop_intent', 'containers_stopped', 'cleanup_intent', 'cleanup_confirmed',
        'data_delete_intent', 'data_deleted',
    ))


class AccessStore:
    def __init__(self, settings, *, identity=None):
        self.settings = settings
        self.identity = identity
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS app_access (
                app_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                credentials_approved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS app_members (
                app_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(app_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS app_secret_scopes (
                repository TEXT NOT NULL, subdomain TEXT NOT NULL, app_id TEXT NOT NULL,
                approved_by TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(repository,subdomain)
            );
            CREATE TABLE IF NOT EXISTS operation_auth (
                kind TEXT NOT NULL, id TEXT NOT NULL, user_id TEXT NOT NULL,
                user_revision INTEGER NOT NULL, app_id TEXT NOT NULL,
                app_revision INTEGER NOT NULL, action TEXT NOT NULL, request_key TEXT,
                PRIMARY KEY(kind,id)
            );
            CREATE TABLE IF NOT EXISTS access_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                action TEXT NOT NULL, app_id TEXT, subject_id TEXT,
                detail TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
            );
        ''')
        if 'request_key' not in {row[1] for row in db.execute('PRAGMA table_info(operation_auth)')}:
            db.execute('ALTER TABLE operation_auth ADD COLUMN request_key TEXT')

    @contextmanager
    def _connection(self, write=False):
        db = sqlite3.connect(self.settings.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
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

    @staticmethod
    def _actor(db, actor):
        if not isinstance(actor, dict) or not isinstance(actor.get('id'), str):
            raise denied()
        try:
            row = db.execute('SELECT * FROM auth_users WHERE id=?', (actor['id'],)).fetchone()
        except sqlite3.OperationalError:
            raise denied() from None
        if row is None:
            raise denied()
        fresh = dict(row)
        if (not fresh['enabled'] or not fresh['eligible']
                or fresh['revision'] != actor.get('revision')
                or fresh['app_id'] != actor.get('app_id') or fresh['tenant_key'] != actor.get('tenant_key')):
            raise denied()
        return fresh

    @staticmethod
    def _role(db, actor, app_id):
        if not db.execute('SELECT 1 FROM apps WHERE id=?', (app_id,)).fetchone():
            raise denied()
        if actor['is_admin']:
            return 'admin'
        row = db.execute('SELECT role FROM app_members WHERE app_id=? AND user_id=? AND enabled=1',
                         (app_id, actor['id'])).fetchone()
        return row['role'] if row else None

    @classmethod
    def _require(cls, db, actor, app_id, action='view'):
        actor = cls._actor(db, actor)
        role = cls._role(db, actor, app_id)
        allowed = {
            'view': {'admin', 'owner', 'maintainer', 'viewer'},
            'detail': {'admin', 'owner', 'maintainer'},
            'deploy': {'admin', 'owner', 'maintainer'},
            'secrets': {'admin', 'owner', 'maintainer'},
            'secrets_rotate': {'admin', 'owner', 'maintainer'},
            'secrets_revoke': {'admin', 'owner'},
            'offline': {'admin', 'owner'},
            'delete': {'admin', 'owner'},
            'members': {'admin', 'owner'},
        }
        if role not in allowed.get(action, set()):
            raise denied()
        if action in {'deploy', 'secrets', 'secrets_rotate', 'secrets_revoke', 'offline', 'delete'} and not actor['can_deploy']:
            raise denied()
        return role

    def require_app(self, actor, app_id, action='view'):
        with self._connection() as db:
            return self._require(db, actor, app_id, action)

    def require_job(self, actor, job, action='detail'):
        if job.get('app_id'):
            return self.require_app(actor, job['app_id'], action)
        # Pre-upgrade failures have no established application ownership.
        with self._connection() as db:
            fresh = self._actor(db, actor)
            if (not fresh['is_admin'] or action not in {'view', 'detail', 'secrets'}
                    or action == 'secrets' and not fresh['can_deploy']):
                raise denied()
        return 'admin'

    def require_offline(self, actor, operation, action='detail'):
        return self.require_app(actor, operation['app_id'], action)

    def role(self, actor, app_id):
        try:
            return self.require_app(actor, app_id)
        except DeployError as error:
            if error.code != 'ACCESS_DENIED':
                raise
            return None

    def can_view(self, actor, app_id):
        return self.role(actor, app_id) is not None

    def project_app(self, actor, app):
        role = self.require_app(actor, app['id'])
        if role == 'viewer':
            safe = {'id', 'subdomain', 'lastSubdomain', 'domainBound', 'active_release_id',
                    'created_at', 'updated_at'}
            return {**{key: value for key, value in app.items() if key in safe}, 'role': role}
        return {**app, 'role': role}

    @staticmethod
    def scoped_key(actor, operation, key):
        data = json.dumps([actor['app_id'], actor['tenant_key'], actor['id'], operation, key],
                          ensure_ascii=False, separators=(',', ':'))
        return 'auth:' + hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _audit(db, actor_id, action, app_id=None, subject_id=None, detail=None):
        db.execute('''INSERT INTO access_audit(actor_id,action,app_id,subject_id,detail,created_at)
            VALUES (?,?,?,?,?,?)''', (actor_id, action, app_id, subject_id,
                                    json.dumps(detail or {}, sort_keys=True), time.time()))

    @classmethod
    def create_owned(cls, db, actor, app_id):
        fresh = cls._actor(db, actor)
        if not fresh['can_deploy']:
            raise denied()
        db.execute('INSERT INTO app_access(app_id,owner_id,credentials_approved) VALUES (?,?,?)',
                   (app_id, fresh['id'], int(bool(fresh['is_admin']))))
        db.execute('INSERT INTO app_members(app_id,user_id,role) VALUES (?,?,?)',
                   (app_id, fresh['id'], 'owner'))
        cls._audit(db, fresh['id'], 'application_created', app_id, fresh['id'])

    @staticmethod
    def require_secret_scope(db, app_id, repository, subdomain):
        row = db.execute("SELECT app_id FROM app_secret_scopes WHERE repository=? AND subdomain=?",
                         (repository, subdomain)).fetchone()
        if row is None or row['app_id'] != app_id:
            raise denied()

    @classmethod
    def claim_secret_scope(cls, db, settings, actor, app_id, repository, subdomain):
        """Bind a new namespace without adopting somebody else's saved secrets."""
        from .secrets_store import SecretStore
        actor = cls._actor(db, actor)
        if getattr(settings, 'secret_backend', 'legacy') == 'encrypted':
            from .vault_format import require_ready
            require_ready(settings, db)
            return  # Encrypted ownership is app_id; legacy mappings stay unchanged for migration audits.
        row = db.execute("SELECT app_id FROM app_secret_scopes WHERE repository=? AND subdomain=?",
                         (repository, subdomain)).fetchone()
        if row is not None:
            if row['app_id'] != app_id:
                raise denied()
            return
        # A historical app binding remains authoritative after unbinding its DNS name.
        for historical in db.execute('SELECT app_id,input,execution FROM jobs'):
            payload, execution = json.loads(historical['input']), json.loads(historical['execution'])
            cleared = execution.get('data_deleted') is True and isinstance(execution.get('data_reset_operation'), str) \
                and bool(execution['data_reset_operation'])
            if (cleared or payload.get('repository') != repository or payload.get('subdomain') != subdomain):
                continue
            owner = historical['app_id'] or payload.get('appId')
            if owner != app_id and not (owner is None and actor['is_admin']):
                raise denied()
        # Metadata only: even an orphaned version/pointer or unsafe existing path
        # needs an administrator's explicit binding, never automatic adoption.
        scope = settings.state_dir / 'secrets' / SecretStore._scope_name(repository, subdomain)
        try:
            scope.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise denied() from None
        else:
            if not actor['is_admin']:
                raise denied()
        db.execute('INSERT INTO app_secret_scopes VALUES (?,?,?,?,?)',
                   (repository, subdomain, app_id, actor['id'], time.time()))
        cls._audit(db, actor['id'], 'secret_scope_bound', app_id,
                   detail={'repository': repository, 'subdomain': subdomain})

    @classmethod
    def record_operation(cls, db, actor, kind, operation_id, app_id, action, request_key=None):
        cls._require(db, actor, app_id, action)
        row = db.execute('SELECT revision FROM app_access WHERE app_id=?', (app_id,)).fetchone()
        # Administrators can manage unassigned legacy applications; no member can.
        app_revision = row['revision'] if row else 0
        db.execute('''INSERT INTO operation_auth
                   (kind,id,user_id,user_revision,app_id,app_revision,action,request_key) VALUES (?,?,?,?,?,?,?,?)''',
                   (kind, operation_id, actor['id'], actor['revision'], app_id, app_revision, action, request_key))
        cls._audit(db, actor['id'], kind + '_accepted', app_id, operation_id)

    @classmethod
    def _authorize_operation_local(cls, db, kind, operation_id, *, required=False):
        row = db.execute('SELECT * FROM operation_auth WHERE kind=? AND id=?', (kind, operation_id)).fetchone()
        if row is None:
            if required:
                raise denied()
            return None
        binding = dict(row)
        user = db.execute('SELECT * FROM auth_users WHERE id=?', (binding['user_id'],)).fetchone()
        if user is None or user['revision'] != binding['user_revision']:
            raise denied()
        actor = dict(user)
        cls._require(db, actor, binding['app_id'], binding['action'])
        app = db.execute('SELECT revision FROM app_access WHERE app_id=?', (binding['app_id'],)).fetchone()
        if (app['revision'] if app else 0) != binding['app_revision']:
            raise denied()
        if kind == 'job':
            job = db.execute('SELECT input FROM jobs WHERE id=?', (operation_id,)).fetchone()
            if job is None:
                raise denied()
            payload = json.loads(job['input'])
            from .vault_format import state
            vault_format = state(db)
            if vault_format is None:
                cls.require_secret_scope(db, binding['app_id'], payload.get('repository'), payload.get('subdomain'))
            elif vault_format['format'] != 1 or vault_format['phase'] != 'active':
                raise DeployError('VAULT_MAINTENANCE', '密钥库尚未完成维护，暂不执行新任务。')
        return actor

    def authorize_operation_local(self, kind, operation_id):
        with self._connection() as db:
            return self._authorize_operation_local(db, kind, operation_id,
                                                   required=getattr(self.settings, 'auth_mode', 'basic') == 'feishu')

    def authorize_operation(self, kind, operation_id, *, force=False):
        actor = self.authorize_operation_local(kind, operation_id)
        if actor is None:
            return None
        if self.identity is None:
            from .identity import IdentityStore
            self.identity = IdentityStore(self.settings)
        self.identity.authorize_user(actor['id'], expected_revision=actor['revision'], force=force)
        # Refreshing identity is network I/O; recheck the local epochs after it.
        return self.authorize_operation_local(kind, operation_id)

    @classmethod
    def _target(cls, db, actor, user_id):
        row = db.execute('SELECT * FROM auth_users WHERE id=?', (user_id,)).fetchone()
        if (row is None or not row['enabled'] or not row['eligible']
                or row['app_id'] != actor['app_id'] or row['tenant_key'] != actor['tenant_key']):
            raise denied()
        return dict(row)

    def list_members(self, actor, app_id):
        with self._connection() as db:
            self._require(db, actor, app_id, 'members')
            return [dict(userId=row['user_id'], displayName=row['name'], role=row['role']) for row in db.execute(
                '''SELECT m.user_id,m.role,u.name FROM app_members m JOIN auth_users u ON u.id=m.user_id
                   WHERE m.app_id=? AND m.enabled=1 ORDER BY m.role,u.name,u.id''', (app_id,))]

    @staticmethod
    def _bump(db, app_id):
        db.execute('UPDATE app_access SET revision=revision+1 WHERE app_id=?', (app_id,))

    def set_member(self, actor, app_id, user_id, role):
        if role not in {'maintainer', 'viewer'}:
            raise denied()
        with self._connection(write=True) as db:
            self._require(db, actor, app_id, 'members')
            fresh = self._actor(db, actor)
            self._target(db, fresh, user_id)
            owner = db.execute('SELECT owner_id FROM app_access WHERE app_id=?', (app_id,)).fetchone()
            if not owner or owner['owner_id'] == user_id:
                raise denied()
            db.execute('''INSERT INTO app_members(app_id,user_id,role) VALUES (?,?,?)
                ON CONFLICT(app_id,user_id) DO UPDATE SET role=excluded.role,enabled=1,revision=revision+1''',
                       (app_id, user_id, role))
            self._bump(db, app_id)
            self._audit(db, fresh['id'], 'member_set', app_id, user_id, {'role': role})
        return self.list_members(actor, app_id)

    def remove_member(self, actor, app_id, user_id):
        with self._connection(write=True) as db:
            self._require(db, actor, app_id, 'members')
            owner = db.execute('SELECT owner_id FROM app_access WHERE app_id=?', (app_id,)).fetchone()
            if not owner or owner['owner_id'] == user_id:
                raise denied()
            db.execute('UPDATE app_members SET enabled=0,revision=revision+1 WHERE app_id=? AND user_id=?',
                       (app_id, user_id))
            self._bump(db, app_id)
            self._audit(db, actor['id'], 'member_removed', app_id, user_id)
        return self.list_members(actor, app_id)

    def transfer_owner(self, actor, app_id, user_id):
        with self._connection(write=True) as db:
            self._require(db, actor, app_id, 'members')
            fresh = self._actor(db, actor)
            self._target(db, fresh, user_id)
            app = db.execute('SELECT owner_id FROM app_access WHERE app_id=?', (app_id,)).fetchone()
            if not app or app['owner_id'] == user_id:
                raise denied()
            db.execute("UPDATE app_members SET role='maintainer',revision=revision+1 WHERE app_id=? AND user_id=?",
                       (app_id, app['owner_id']))
            db.execute('''INSERT INTO app_members(app_id,user_id,role) VALUES (?,?,'owner')
                ON CONFLICT(app_id,user_id) DO UPDATE SET role='owner',enabled=1,revision=revision+1''',
                       (app_id, user_id))
            db.execute('UPDATE app_access SET owner_id=?,revision=revision+1 WHERE app_id=?', (user_id, app_id))
            self._audit(db, fresh['id'], 'owner_transferred', app_id, user_id)
        return {'appId': app_id, 'ownerId': user_id}

    def assign_owner(self, actor, app_id, user_id, *, credentials_approved=True):
        with self._connection(write=True) as db:
            fresh = self._actor(db, actor)
            if not fresh['is_admin'] or self._role(db, fresh, app_id) != 'admin':
                raise denied()
            self._target(db, fresh, user_id)
            if db.execute('SELECT 1 FROM app_access WHERE app_id=?', (app_id,)).fetchone():
                raise denied()
            db.execute('INSERT INTO app_access(app_id,owner_id,credentials_approved) VALUES (?,?,?)',
                       (app_id, user_id, int(bool(credentials_approved))))
            db.execute("INSERT INTO app_members(app_id,user_id,role) VALUES (?,?,'owner')", (app_id, user_id))
            app = db.execute('SELECT repository,subdomain FROM apps WHERE id=?', (app_id,)).fetchone()
            scopes = {(app['repository'], app['subdomain'])} if app['subdomain'] else set()
            for row in db.execute('SELECT input,execution FROM jobs WHERE app_id=?', (app_id,)):
                payload, execution = json.loads(row['input']), json.loads(row['execution'])
                if not (execution.get('data_deleted') is True and execution.get('data_reset_operation')):
                    scopes.add((payload['repository'], payload['subdomain']))
            for repository, subdomain in sorted(scopes):
                self.claim_secret_scope(db, self.settings, fresh, app_id, repository, subdomain)
            self._audit(db, fresh['id'], 'legacy_owner_assigned', app_id, user_id,
                        {'credentialsApproved': bool(credentials_approved)})
        return self.list_members(actor, app_id)

    def retry_key(self, actor, operation):
        with self._connection() as db:
            try:
                self._require(db, actor, operation['app_id'],
                              'delete' if operation['execution'].get('data_policy') == 'delete' else 'offline')
            except DeployError:
                return None
            row = db.execute("SELECT * FROM operation_auth WHERE kind='offline' AND id=?", (operation['id'],)).fetchone()
            if row is None or row['user_id'] != actor['id']:
                return None
            if not offline_started(operation):
                try:
                    self._authorize_operation_local(db, 'offline', operation['id'], required=True)
                except DeployError:
                    return None
            return row['request_key']

    def repository_credentials_allowed(self, app_id):
        with self._connection() as db:
            row = db.execute('SELECT credentials_approved FROM app_access WHERE app_id=?', (app_id,)).fetchone()
            # Only legacy Basic mode may implicitly use the historical global helper.
            return bool(row['credentials_approved']) if row else getattr(self.settings, 'auth_mode', 'basic') == 'basic'

    def set_credentials_approved(self, actor, app_id, approved):
        if type(approved) is not bool:
            raise denied()
        with self._connection(write=True) as db:
            fresh = self._actor(db, actor)
            if not fresh['is_admin'] or self._role(db, fresh, app_id) != 'admin':
                raise denied()
            if not db.execute('SELECT 1 FROM app_access WHERE app_id=?', (app_id,)).fetchone():
                raise denied()
            db.execute('UPDATE app_access SET credentials_approved=?,revision=revision+1 WHERE app_id=?',
                       (int(approved), app_id))
            self._audit(db, fresh['id'], 'repository_credentials_changed', app_id,
                        detail={'credentialsApproved': approved})
