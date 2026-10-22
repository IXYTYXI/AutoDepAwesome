"""Explicit legacy-to-quota identity transitions; never copy data or infer a path.

Operator mutations require the worker lock and a maintenance window. The caller
verifies Docker identities, stops all source writers, copies as the mapped runtime
user and checks the target through the privileged helper before committing. This
ledger preserves original releases; runtime and deletion resolve their references.
"""

from contextlib import contextmanager
import copy
import hashlib
import json
import re
import sqlite3
import time

from .errors import DeployError


ACTIVE = {'committed', 'writes_started', 'source_removed'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _fail(message='Storage migration identity or state changed'):
    raise DeployError('STORAGE_MIGRATION_CONFLICT', message)


class StorageMigrations:
    def __init__(self, settings):
        self.path = getattr(settings, 'database_path', None)

    @staticmethod
    def ensure_schema(db):
        db.execute('''CREATE TABLE IF NOT EXISTS storage_migrations (
            operation_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, service TEXT NOT NULL,
            name TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
            release_id TEXT NOT NULL, release_digest TEXT NOT NULL,
            state TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL)''')
        db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS storage_migrations_source
            ON storage_migrations(app_id,service,name,source) WHERE state!='rolled_back' ''')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS storage_migrations_target ON storage_migrations(target)')

    @contextmanager
    def _connection(self, *, write=False):
        if self.path is None or not self.path.exists():
            if write:
                _fail('Platform state is required for an explicit migration')
            yield None
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True,
                             isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except sqlite3.Error:
            if write:
                db.rollback()
            _fail('Storage migration ledger is unavailable')
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _rows(db, app_id):
        if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_migrations'").fetchone():
            return []
        result = []
        for item in db.execute('SELECT * FROM storage_migrations WHERE app_id=? ORDER BY created_at,operation_id', (app_id,)):
            row = dict(item)
            for key in ('source', 'target', 'evidence'):
                row[key] = json.loads(row[key])
            if row['state'] not in ACTIVE | {'prepared', 'rolled_back'}:
                _fail()
            result.append(row)
        return result

    @staticmethod
    def _validate(app_id, service, spec, source, target):
        from .storage import ManagedStorage
        name, _ = ManagedStorage.identity(app_id, service, spec)
        if (not isinstance(source, dict) or set(source) != {'name', 'created_at', 'instance'}
                or source['name'] != name or not isinstance(source['created_at'], str)
                or not source['created_at'].strip()
                or not re.fullmatch(r'[0-9a-f]{32}', str(source['instance']))):
            _fail('Migration requires the exact original Docker volume instance')
        ManagedStorage(None).validate_reference(app_id, service, spec, target)

    def resolve(self, app_id, service, spec, expected, *, db=None):
        if expected is None:
            return expected
        if db is None:
            with self._connection() as connection:
                return self._resolve(connection, app_id, service, spec, expected)
        return self._resolve(db, app_id, service, spec, expected)

    def _resolve(self, db, app_id, service, spec, expected):
        rows = [row for row in self._rows(db, app_id)
                if row['service'] == service and row['name'] == spec.get('name')]
        for row in rows:
            self._validate(app_id, service, spec, row['source'], row['target'])
            if expected == row['target'] and row['state'] not in ACTIVE:
                _fail('Migration target is not committed for business writes')
            if expected == row['source'] and row['state'] in ACTIVE:
                return copy.deepcopy(row['target'])
        if any(row['state'] in ACTIVE for row in rows) and isinstance(expected, dict) and expected.get('backend') != 'xfs-project':
            _fail('Legacy volume does not match its recorded migration source')
        return expected

    def persisted_reference(self, app_id, service, spec, expected):
        """Keep old release identities intact while runtime uses the target copy."""
        with self._connection() as db:
            for row in self._rows(db, app_id):
                if row['service'] == service and row['name'] == spec['name'] and row['target'] == expected:
                    self._validate(app_id, service, spec, row['source'], row['target'])
                    if row['state'] not in ACTIVE:
                        _fail('Migration target is not committed for business writes')
                    return copy.deepcopy(row['source'])
        return expected

    @staticmethod
    def _release(db, release_id):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (release_id,)).fetchone()
        if row is None:
            _fail('Migration release is missing')
        job = dict(row)
        job['snapshot'] = json.loads(job['snapshot'])
        job['execution'] = json.loads(job['execution'])
        app = db.execute('SELECT active_release_id FROM apps WHERE id=?', (job['app_id'],)).fetchone()
        if (app is None or app[0] != release_id or job['status'] != 'succeeded'
                or job['execution'].get('resources_released') or job['execution'].get('failure_intent')):
            _fail('Migration requires the unchanged active release')
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','recovery_required') LIMIT 1").fetchone():
            _fail('Unfinished deployment work must finish before migration')
        if db.execute("SELECT 1 FROM offline_operations WHERE status IN ('queued','running','recovery_required') LIMIT 1").fetchone():
            _fail('Unfinished offline work must finish before migration')
        return job

    @staticmethod
    def _release_digest(job):
        return hashlib.sha256(_json({'snapshot': job['snapshot'],
                                     'images': job['execution'].get('images', {})}).encode()).hexdigest()

    def prepare(self, release_id, service, source, target, *, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', operation_id):
            _fail('Migration operation ID is invalid')
        with self._connection(write=True) as db:
            self.ensure_schema(db)
            job = self._release(db, release_id)
            spec = job['snapshot']['manifest']['services'].get(service, {}).get('storage')
            self._validate(job['app_id'], service, spec, source, target)
            if job['execution'].get('storage_volumes', {}).get(service) != source:
                _fail('Active release does not record the requested source')
            existing = db.execute('SELECT * FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone()
            values = (job['app_id'], service, spec['name'], _json(source), _json(target), release_id,
                      self._release_digest(job))
            if existing:
                if tuple(existing[k] for k in ('app_id', 'service', 'name', 'source', 'target', 'release_id', 'release_digest')) != values:
                    _fail('Migration operation ID is already bound to another transition')
                return dict(existing)
            now = time.time()
            db.execute('INSERT INTO storage_migrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (operation_id, *values, 'prepared', '{}', now, now))
            return dict(db.execute('SELECT * FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone())

    def commit(self, operation_id, *, source_digest, target_digest, stopped_container_ids):
        if (source_digest != target_digest or not re.fullmatch(r'[0-9a-f]{64}', str(source_digest))
                or not isinstance(stopped_container_ids, list) or not stopped_container_ids
                or len(set(stopped_container_ids)) != len(stopped_container_ids)
                or any(not re.fullmatch(r'[0-9a-f]{64}', str(item)) for item in stopped_container_ids)):
            _fail('Migration requires equal verified tree digests and exact stopped writers')
        evidence = {'source_digest': source_digest, 'target_digest': target_digest,
                    'stopped_container_ids': sorted(stopped_container_ids)}
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None:
                _fail('Prepare the explicit migration before committing it')
            if row['state'] in ACTIVE:
                if json.loads(row['evidence']) != evidence:
                    _fail('Committed migration evidence changed')
                return
            if row['state'] != 'prepared':
                _fail()
            job = self._release(db, row['release_id'])
            if (self._release_digest(job) != row['release_digest']
                    or job['execution'].get('storage_volumes', {}).get(row['service']) != json.loads(row['source'])):
                _fail('Frozen release or source identity changed during migration')
            db.execute("UPDATE storage_migrations SET state='committed',evidence=?,updated_at=? WHERE operation_id=?",
                       (_json(evidence), time.time(), operation_id))

    def mark_writes_started(self, target):
        if self.path is None or not self.path.exists():
            return
        with self._connection(write=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_migrations'").fetchone():
                return
            states = db.execute('SELECT state FROM storage_migrations WHERE target=?', (_json(target),)).fetchall()
            if any(row[0] not in ACTIVE for row in states):
                _fail('Migration target is not committed for business writes')
            db.execute("UPDATE storage_migrations SET state='writes_started',updated_at=? WHERE target=? AND state='committed'",
                       (time.time(), _json(target)))

    def rollback(self, operation_id):
        with self._connection(write=True) as db:
            row = db.execute('SELECT state FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None:
                _fail()
            if row[0] in {'writes_started', 'source_removed'}:
                raise DeployError('STORAGE_MIGRATION_WRITES_STARTED', 'Target may contain new writes; reverse migration is required')
            db.execute("UPDATE storage_migrations SET state='rolled_back',updated_at=? WHERE operation_id=?",
                       (time.time(), operation_id))

    def source_removed(self, operation_id, source):
        """Call only after exact-instance removal or confirmed original absence."""
        with self._connection(write=True) as db:
            row = db.execute('SELECT state,source FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None or row['state'] not in ACTIVE or json.loads(row['source']) != source:
                _fail()
            db.execute("UPDATE storage_migrations SET state='source_removed',updated_at=? WHERE operation_id=?",
                       (time.time(), operation_id))

    def retained_sources(self, app_id, *, db=None, include_removed=False):
        if db is None:
            with self._connection() as connection:
                return self._sources(connection, app_id, include_removed)
        return self._sources(db, app_id, include_removed)

    def _sources(self, db, app_id, include_removed):
        return [{'service': row['service'], 'spec': {'name': row['name'], 'mountPath': '/data'},
                 'expected': row['source'], 'target': row['target'], 'operation_id': row['operation_id']}
                for row in self._rows(db, app_id)
                if row['state'] in (ACTIVE if include_removed else {'committed', 'writes_started'})]


def resolve_job(settings, job):
    migrations = StorageMigrations(settings)
    result = copy.deepcopy(job)
    for service, reference in job.get('execution', {}).get('storage_volumes', {}).items():
        spec = (job.get('snapshot') or {}).get('manifest', {}).get('services', {}).get(service, {}).get('storage')
        if spec:
            result['execution']['storage_volumes'][service] = migrations.resolve(job['app_id'], service, spec, reference)
    return result
