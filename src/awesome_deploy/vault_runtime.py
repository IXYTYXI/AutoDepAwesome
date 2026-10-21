"""Read-only runtime bridge: frozen app identity and versions are mandatory."""

from contextlib import contextmanager
import json
import sqlite3

from .errors import DeployError
from .secret_vault import SecretVault
from .vault_format import require_ready


class VaultRuntime:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def ensure_schema(db):
        db.execute('''CREATE TABLE IF NOT EXISTS vault_legacy_jobs (
            job_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, data_generation TEXT NOT NULL
        )''')

    @staticmethod
    def has_retained_inputs(status, snapshot, execution):
        # Field presence is durable evidence of pinning, even when the map is
        # empty or malformed. Callers must then validate its shape and binding.
        return bool(status == 'succeeded' or execution.get('frozen')
                    or 'secret_versions' in snapshot or 'secret_versions' in execution)

    @contextmanager
    def connection(self, db=None):
        if db is not None:
            require_ready(self.settings, db)
            yield db
            return
        connection = sqlite3.connect(self.settings.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN')
            require_ready(self.settings, connection)
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _invalid():
        return DeployError('SECRET_SNAPSHOT_INVALID', '部署固定的密钥身份或版本不完整，不能启动或恢复。')

    def _binding(self, job, db, vault):
        app_id = job.get('app_id')
        if app_id is None:
            legacy = db.execute('SELECT * FROM vault_legacy_jobs WHERE job_id=?', (job.get('id'),)).fetchone()
            if legacy is None:
                raise self._invalid()
            app_id = legacy['app_id']
        app = vault._app(db, app_id)
        if app['tenant_id'] != self.settings.feishu_tenant_key:
            raise self._invalid()
        vault._dek(db, app)
        return {key: app[key] for key in ('tenant_id', 'app_id', 'data_generation')}

    def binding(self, job, *, db=None):
        with self.connection(db) as connection:
            return self._binding(job, connection, SecretVault(self.settings))

    def _validate_binding(self, job, db, vault):
        binding = self._binding(job, db, vault)
        snapshot = job.get('snapshot') or {}
        frozen = snapshot.get('vault_identity') or (job.get('execution') or {}).get('vault_identity')
        if frozen is None:
            legacy = db.execute('SELECT * FROM vault_legacy_jobs WHERE job_id=?', (job.get('id'),)).fetchone()
            if not legacy or any(legacy[key] != binding[key] for key in ('app_id', 'data_generation')):
                raise self._invalid()
        elif frozen != binding:
            raise self._invalid()
        return binding

    @staticmethod
    def _refs(snapshot, service):
        try:
            refs = snapshot['manifest']['services'][service].get('secretRefs', {})
        except (KeyError, TypeError):
            raise VaultRuntime._invalid() from None
        if not isinstance(refs, dict):
            raise VaultRuntime._invalid()
        return refs

    def _check_kinds(self, db, vault, app_id, refs):
        for env, name in refs.items():
            app = vault._app(db, app_id)
            record = vault._record(db, app, name)
            if env == 'MASTER_KEY' and record['kind'] != 'platform_managed_master':
                raise DeployError('SECRET_MASTER_PROTECTED', '主密钥类型已固定，不能通过配置改名替换业务数据主密钥。')

    def versions(self, job, service, *, db=None):
        with self.connection(db) as connection:
            vault = SecretVault(self.settings)
            binding = self._binding(job, connection, vault)
            refs = self._refs(job['snapshot'], service)
            self._check_kinds(connection, vault, binding['app_id'], refs)
            return vault.versions(connection, binding['app_id'], refs)

    def _validate(self, job, db, vault):
        binding = self._validate_binding(job, db, vault)
        snapshot = job.get('snapshot') or {}
        versions = snapshot.get('secret_versions')
        if versions is None:
            versions = (job.get('execution') or {}).get('secret_versions')
        services = snapshot.get('manifest', {}).get('services')
        legacy = not snapshot.get('vault_identity') and not (job.get('execution') or {}).get('vault_identity')
        if legacy and isinstance(versions, dict) and isinstance(services, dict):
            versions = {**{name: {} for name, service in services.items() if not service.get('secretRefs')}, **versions}
        if not isinstance(services, dict) or not isinstance(versions, dict) or set(versions) != set(services):
            raise self._invalid()
        for service in services:
            refs = self._refs(snapshot, service)
            frozen = versions.get(service)
            if not isinstance(frozen, dict) or set(frozen) != set(refs.values()):
                raise self._invalid()
            self._check_kinds(db, vault, binding['app_id'], refs)
            vault.validate_versions(db, binding['app_id'], frozen)
        return binding, versions

    def validate(self, job, *, db=None):
        with self.connection(db) as connection:
            self._validate(job, connection, SecretVault(self.settings))

    def resolve(self, job, service, *, db=None):
        with self.connection(db) as connection:
            vault = SecretVault(self.settings)
            binding, versions = self._validate(job, connection, vault)
            return vault.resolve(connection, binding['app_id'], self._refs(job['snapshot'], service), versions[service])

    def validate_previous(self, candidate):
        previous_id = (candidate.get('execution') or {}).get('previous_release')
        if not previous_id:
            return  # Rollback to no release restores no secret-bearing instance.
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (previous_id,)).fetchone()
            app = db.execute('SELECT active_release_id FROM apps WHERE id=?', (candidate.get('app_id'),)).fetchone()
            if (not row or not app or app['active_release_id'] != previous_id
                    or row['app_id'] != candidate.get('app_id') or row['status'] != 'succeeded'):
                raise self._invalid()
            previous = dict(row)
            for field in ('snapshot', 'execution'):
                previous[field] = json.loads(previous[field] or '{}')
            self._validate(previous, db, SecretVault(self.settings))
