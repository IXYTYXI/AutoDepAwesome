"""Short, transactional SQLite state changes for durable deployments."""

from contextlib import contextmanager
from collections.abc import Mapping
import json
import re
import socket
import sqlite3
import time
import uuid

from .errors import DeployError
from .access import AccessStore, denied, offline_started
from .storage import ManagedStorage


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, settings, *, verified_offline_domains=None, allow_vault_maintenance=False):
        self.settings = settings
        self.allow_vault_maintenance = allow_vault_maintenance
        settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    input TEXT NOT NULL, app_id TEXT, status TEXT NOT NULL, phase TEXT NOT NULL,
                    snapshot TEXT, execution TEXT NOT NULL DEFAULT '{}', error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS apps (
                    id TEXT PRIMARY KEY, repository TEXT NOT NULL, subdomain TEXT UNIQUE,
                    active_release_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    job_id TEXT NOT NULL REFERENCES jobs(id), service TEXT NOT NULL,
                    port INTEGER NOT NULL UNIQUE, cpu REAL NOT NULL, memory_mib INTEGER NOT NULL,
                    PRIMARY KEY(job_id, service)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
                    phase TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS jobs_app ON jobs(app_id, status);
                CREATE TABLE IF NOT EXISTS offline_operations (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    app_id TEXT NOT NULL REFERENCES apps(id),
                    target_release_id TEXT NOT NULL REFERENCES jobs(id),
                    status TEXT NOT NULL, phase TEXT NOT NULL,
                    execution TEXT NOT NULL DEFAULT '{}', error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS offline_app ON offline_operations(app_id, status);
            """)
            self._migrate_domain_bindings(db, verified_offline_domains)
            AccessStore.ensure_schema(db)
            from .storage_migration import StorageMigrations
            StorageMigrations.ensure_schema(db)
            from .secret_vault import SecretVault
            from .vault_runtime import VaultRuntime
            from .vault_service import VaultService
            from .vault_format import ensure_schema, require_ready
            SecretVault.ensure_schema(db)
            VaultRuntime.ensure_schema(db)
            VaultService.ensure_schema(db)
            ensure_schema(db)
            if not allow_vault_maintenance:
                require_ready(settings, db)

    def _migrate_domain_bindings(self, db, verified_offline_domains):
        """Relax only the old domain constraint; preserve every child table and foreign key."""
        if verified_offline_domains is None:
            verified_offline_domains = {}
        if not isinstance(verified_offline_domains, Mapping) or any(
            not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None
            for pair in verified_offline_domains.items() for value in pair
        ):
            raise DeployError("STATE_MIGRATION_FAILED", "Verified offline domains must identify exact applications and releases.")
        columns = list(db.execute("PRAGMA table_info(apps)"))
        legacy_schema = next(row for row in columns if row["name"] == "subdomain")["notnull"]
        if not legacy_schema and not verified_offline_domains:
            return
        if legacy_schema:
            db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            # Another process may have migrated while this connection waited for the write lock.
            columns = list(db.execute("PRAGMA table_info(apps)"))
            if not next(row for row in columns if row["name"] == "subdomain")["notnull"]:
                self._release_verified_offline_domains(db, verified_offline_domains)
                db.commit()
                return
            if [row["name"] for row in columns] != [
                "id", "repository", "subdomain", "active_release_id", "created_at", "updated_at"
            ] or db.execute("PRAGMA foreign_key_check").fetchall():
                raise DeployError("STATE_MIGRATION_FAILED", "Application domain migration requires a consistent database.")
            existing_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='apps'").fetchone()[0]
            normalized = re.sub(r"\s+", "", existing_sql).replace('"apps"', 'apps').lower()
            expected = ("createtableapps(idtextprimarykey,repositorytextnotnull,subdomaintextnotnullunique,"
                        "active_release_idtext,created_atrealnotnull,updated_atrealnotnull)")
            if normalized != expected:
                raise DeployError("STATE_MIGRATION_FAILED", "Application schema differs from the reviewed legacy version.")
            # Do not silently discard operator-added app indexes or triggers.
            if db.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='apps' AND type!='table' AND sql IS NOT NULL").fetchone():
                raise DeployError("STATE_MIGRATION_FAILED", "Application schema has unexpected custom objects.")
            other_schema = [tuple(row) for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name!='apps' ORDER BY type,name")]
            db.execute("""CREATE TABLE apps_domain_migration (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL, subdomain TEXT UNIQUE,
                active_release_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""")
            db.execute("INSERT INTO apps_domain_migration SELECT * FROM apps")
            # Keep the original name in all child FOREIGN KEY declarations. Renaming
            # the old table first would rewrite those references on current SQLite.
            db.execute("DROP TABLE apps")
            db.execute("ALTER TABLE apps_domain_migration RENAME TO apps")
            current_schema = [tuple(row) for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name!='apps' ORDER BY type,name")]
            if current_schema != other_schema or db.execute("PRAGMA foreign_key_check").fetchall():
                raise DeployError("STATE_MIGRATION_FAILED", "Application migration changed a dependent schema.")
            self._release_verified_offline_domains(db, verified_offline_domains)
            db.commit()
        except BaseException as error:
            db.rollback()
            if isinstance(error, sqlite3.Error):
                raise DeployError("STATE_MIGRATION_FAILED", "Application domain migration could not be completed safely.") from None
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    def _release_verified_offline_domains(self, db, verified_offline_domains):
        # Old completion flags did not prove that every old gateway worker had
        # drained. Only the upgrade caller can supply that external verification.
        if verified_offline_domains and db.execute("PRAGMA foreign_key_check").fetchall():
            raise DeployError("STATE_MIGRATION_FAILED", "Verified domain release requires a consistent database.")
        for app_id, release_id in verified_offline_domains.items():
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if row is None or not self._legacy_offline_releasable(db, dict(row), release_id):
                raise DeployError("STATE_MIGRATION_FAILED", "Verified offline application state changed; no domains were released.")
            db.execute("UPDATE apps SET subdomain=NULL WHERE id=?", (app_id,))

    def _legacy_offline_releasable(self, db, app, expected_release_id):
        if app["active_release_id"] is not None:
            return False
        row = db.execute("""SELECT * FROM offline_operations WHERE app_id=?
            ORDER BY created_at DESC,id DESC LIMIT 1""", (app["id"],)).fetchone()
        if not row or row["status"] != "succeeded" or row["target_release_id"] != expected_release_id:
            return False
        operation = self._offline(row)
        previous = self._get(db, operation["target_release_id"])
        if (previous["app_id"] != app["id"] or previous["status"] != "succeeded"
                or app["subdomain"] not in (None, previous["input"].get("subdomain"))
                or not previous["execution"].get("resources_released")
                or not all(operation["execution"].get(key) is True for key in
                           ("gateway_deactivated", "containers_stopped", "cleanup_confirmed"))):
            return False
        if db.execute("""SELECT 1 FROM reservations JOIN jobs ON jobs.id=reservations.job_id
            WHERE jobs.app_id=? LIMIT 1""", (app["id"],)).fetchone():
            return False
        if db.execute("""SELECT 1 FROM offline_operations WHERE app_id=?
            AND status IN ('queued','running','recovery_required') LIMIT 1""", (app["id"],)).fetchone():
            return False
        for row in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','recovery_required')"):
            pending = self._job(row)
            if (pending["app_id"] == app["id"] or pending["input"].get("appId") == app["id"]
                    or (app["subdomain"] is not None and pending["input"].get("subdomain") == app["subdomain"])):
                return False
        return True

    @contextmanager
    def _connection(self, write=False):
        db = sqlite3.connect(self.settings.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
                if not self.allow_vault_maintenance:
                    from .vault_format import require_ready
                    require_ready(self.settings, db)
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
    def _job(row):
        if row is None:
            raise DeployError("NOT_FOUND", "Deployment job was not found.")
        result = dict(row)
        for key in ("input", "snapshot", "execution", "error"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def _get(self, db, job_id):
        return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get(self, job_id):
        with self._connection() as db:
            return self._get(db, job_id)

    @staticmethod
    def _data_cleared(job):
        state = job.get("execution") or {}
        return (state.get("data_deleted") is True
                and isinstance(state.get("data_reset_operation"), str)
                and bool(state["data_reset_operation"]))

    @staticmethod
    def _secret_scope_identity(repository, subdomain):
        if (not isinstance(repository, str) or not repository or len(repository) > 4096 or "\0" in repository
                or not isinstance(subdomain, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", subdomain)):
            raise DeployError("OFFLINE_DATA_CONFLICT", "Cannot verify the historical data scope.")
        return repository, subdomain

    @staticmethod
    def _check_secret_delete_gate(db, repository, subdomain):
        for row in db.execute("SELECT execution FROM offline_operations WHERE status IN ('queued','running','recovery_required')"):
            state = json.loads(row["execution"])
            if state.get("data_policy") == "delete" and any(
                scope.get("repository") == repository and scope.get("subdomain") == subdomain
                for scope in state["deletion_metadata"]["scopes"]
            ):
                raise DeployError("SECRET_CONFIGURATION_BUSY", "This secret scope has a pending data deletion.")

    @contextmanager
    def secret_write_scope(self, repository, subdomain):
        """Serialize CLI secret writes with delete admission; caller also holds worker.lock."""
        self._secret_scope_identity(repository, subdomain)
        with self._connection(write=True) as db:
            self._check_secret_delete_gate(db, repository, subdomain)
            yield

    @contextmanager
    def secret_configuration_scope(self, job_id, *, write=False, actor=None, include_connection=False):
        """Authorize a new secret writer independently of old-version reuse rules.

        The caller holds worker.lock before requesting a write transaction, and
        keeps this transaction open until all create-only filesystem writes end.
        Read transactions authorize metadata from a single consistent DB view.
        """
        with self._connection(write=write) as db:
            if not write:
                db.execute("BEGIN")
            job = self._get(db, job_id)
            if actor is not None:
                fresh = AccessStore._actor(db, actor)
                if job.get("app_id"):
                    AccessStore._require(db, fresh, job["app_id"], "secrets")
                    if not fresh["is_admin"] and self.settings.secret_backend != "encrypted":
                        AccessStore.require_secret_scope(db, job["app_id"],
                            job["input"].get("repository"), job["input"].get("subdomain"))
                elif not fresh["is_admin"] or not fresh["can_deploy"]:
                    raise denied()
            elif getattr(self.settings, "auth_mode", "basic") == "feishu":
                raise denied()
            if (job["status"] != "failed" or not isinstance(job.get("error"), dict)
                    or job["error"].get("code") != "SECRET_NOT_FOUND"
                    or not isinstance(job.get("input"), dict) or self._data_cleared(job)):
                raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "该任务不能补充秘密配置。")
            can_generate = self._authorize_secret_configuration(db, job)
            yield (job, can_generate, db) if include_connection else (job, can_generate)

    def _authorize_secret_configuration(self, db, job):
        payload = job["input"]
        repository, subdomain = payload.get("repository"), payload.get("subdomain")
        if not isinstance(repository, str) or not isinstance(subdomain, str):
            raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "该任务不能补充秘密配置。")
        self._check_secret_delete_gate(db, repository, subdomain)
        conflict = DeployError("SECRET_SCOPE_CONFLICT", "该配置空间不属于所选应用。")
        busy = DeployError("SECRET_CONFIGURATION_BUSY", "相关部署或恢复工作尚未结束。")
        selected, assigned = payload.get("appId"), job.get("app_id")
        if selected is not None and (not isinstance(selected, str) or not selected):
            raise conflict
        if assigned is not None and (not isinstance(assigned, str) or not assigned):
            raise conflict
        if selected and assigned and selected != assigned:
            raise conflict
        owner = assigned or selected
        if self.settings.secret_backend == 'encrypted':
            self._check_offline_gate(db, owner, subdomain)
        active_release = None
        if owner:
            app = db.execute("SELECT * FROM apps WHERE id=?", (owner,)).fetchone()
            if (not app or app["repository"] != repository
                    or app["subdomain"] is not None and app["subdomain"] != subdomain):
                raise conflict
            active_release = app["active_release_id"]
        occupied = db.execute("SELECT id FROM apps WHERE subdomain=?", (subdomain,)).fetchone()
        if occupied and occupied["id"] != owner:
            raise conflict
        relevant_ids = set()
        reset_owner, generation_data = False, False
        for row in db.execute("SELECT * FROM jobs"):
            other = self._job(row)
            if self._data_cleared(other):
                reset_owner = reset_owner or owner is not None and other.get("app_id") == owner
                continue
            data = other.get("input")
            if not isinstance(data, dict):
                raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "无法核验配置空间的历史归属。")
            identities = {value for value in (other.get("app_id"), data.get("appId")) if isinstance(value, str) and value}
            same_scope = (self.settings.secret_backend != "encrypted"
                          and data.get("repository") == repository and data.get("subdomain") == subdomain)
            same_owner = owner is not None and owner in identities
            if (same_owner or same_scope) and (other["status"] == "succeeded"
                                              or (other.get("execution") or {}).get("storage_volumes")
                                              or self._storage_may_exist(other)):
                generation_data = True
            if same_scope:
                relevant_ids.add(other["id"])
                # Failed jobs and deleted applications still establish ownership.
                # A rejected user app selection is not a confirmed allocation.
                if other.get("app_id") is not None and other["app_id"] != owner:
                    raise conflict
                if not owner and any((other.get("execution") or {}).get(key) for key in (
                    "ports", "storage_volumes", "images", "publish_intent", "storage_handoff_intent",
                )):
                    raise busy
            if same_owner or data.get("subdomain") == subdomain:
                if other["status"] in {"queued", "running", "recovery_required"}:
                    raise busy
                recovery = (other.get("execution") or {}).get("active_recovery", {})
                if (other["id"] == active_release and isinstance(recovery, dict)
                        and recovery.get("outcome") in {"checking", "failed"}):
                    raise busy
            if (same_scope or same_owner) and db.execute(
                "SELECT 1 FROM reservations WHERE job_id=? LIMIT 1", (other["id"],)
            ).fetchone() and (not owner or other["status"] != "succeeded"):
                raise busy
        for row in db.execute("SELECT * FROM offline_operations WHERE status IN ('queued','running','recovery_required')"):
            if row["app_id"] == owner or row["target_release_id"] in relevant_ids:
                raise busy
        managed_draft = owner is not None and db.execute(
            "SELECT 1 FROM app_access WHERE app_id=?", (owner,)
        ).fetchone() is not None
        return (owner is None or reset_owner or managed_draft) and not active_release and not generation_data

    def enqueue(self, payload, idempotency_key, *, actor=None, credential_id=None):
        encoded = _json(payload)
        request_key = idempotency_key
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise DeployError("INVALID_IDEMPOTENCY_KEY", "Provide an idempotency key of 1–200 characters.")
        if actor is None and getattr(self.settings, "auth_mode", "basic") == "feishu":
            raise denied()
        if credential_id is not None and actor is None:
            raise denied()
        with self._connection(write=True) as db:
            if actor is not None:
                actor = AccessStore._actor(db, actor)
                if not actor["can_deploy"]:
                    raise denied()
                if credential_id is not None:
                    from .api_credentials import ApiCredentialStore
                    ApiCredentialStore.authorize_scope(db, actor, credential_id, payload.get('appId'))
                scoped_request_key = (f'api:{credential_id}:{idempotency_key}'
                                      if credential_id is not None else idempotency_key)
                idempotency_key = AccessStore.scoped_key(actor, "job", scoped_request_key)
            row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if actor is not None:
                    AccessStore._authorize_operation_local(db, "job", row["id"], required=True)
                    if credential_id is not None:
                        binding = db.execute("SELECT credential_id FROM operation_credentials WHERE kind='job' AND id=?",
                                             (row['id'],)).fetchone()
                        if binding is None or binding['credential_id'] != credential_id:
                            raise denied()
                if row["input"] != encoded:
                    raise DeployError("IDEMPOTENCY_CONFLICT", "This key already belongs to a different request.")
                return self._job(row)
            app_id = payload.get("appId")
            if actor is not None:
                if app_id:
                    AccessStore._require(db, actor, app_id, "deploy")
                    app = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
                    if (app["repository"] != payload["repository"]
                            or app["subdomain"] is not None and app["subdomain"] != payload["subdomain"]):
                        raise DeployError("APP_IDENTITY_CONFLICT", "Repository and subdomain must match the application.")
                else:
                    if db.execute("SELECT 1 FROM apps WHERE subdomain=?", (payload["subdomain"],)).fetchone():
                        raise denied()
                    app_id, created = uuid.uuid4().hex, time.time()
                    db.execute("INSERT INTO apps VALUES (?,?,NULL,NULL,?,?)",
                               (app_id, payload["repository"], created, created))
                    AccessStore.create_owned(db, actor, app_id)
                    if self.settings.secret_backend == "encrypted":
                        from .secret_vault import SecretVault
                        SecretVault(self.settings).register_app(db, app_id, self.settings.feishu_tenant_key,
                                                               uuid.uuid4().hex, actor_id=actor["id"])
                if self.settings.secret_backend == "encrypted":
                    self._prepare_vault_generation(db, app_id, actor["id"])
                else:
                    AccessStore.claim_secret_scope(db, self.settings, actor, app_id,
                                                   payload["repository"], payload["subdomain"])
            self._check_offline_gate(db, app_id, payload.get("subdomain"))
            job_id, now = uuid.uuid4().hex, time.time()
            db.execute(
                """INSERT INTO jobs
                (id,idempotency_key,input,app_id,status,phase,created_at,updated_at)
                VALUES (?,?,?,?,'queued','queued',?,?)""",
                (job_id, idempotency_key, encoded, app_id if actor is not None else None, now, now),
            )
            if actor is not None:
                AccessStore.record_operation(db, actor, "job", job_id, app_id, "deploy", request_key,
                                             credential_id=credential_id)
            return self._get(db, job_id)

    def _prepare_vault_generation(self, db, app_id, actor_id):
        from .secret_vault import SecretVault
        vault = SecretVault(self.settings)
        app = vault._app(db, app_id, allow_destroyed=True)
        if app['state'] == 'destroyed':
            current = db.execute('SELECT active_release_id FROM apps WHERE id=?', (app_id,)).fetchone()
            jobs = [self._job(row) for row in db.execute('SELECT * FROM jobs WHERE app_id=?', (app_id,))]
            if (not jobs or current['active_release_id'] is not None
                    or any(not self._data_cleared(job) for job in jobs)):
                raise DeployError('SECRET_MASTER_PROTECTED', '原数据代际尚未确认完整清除，不能生成新主密钥。')
            vault.register_app(db, app_id, self.settings.feishu_tenant_key, uuid.uuid4().hex, actor_id=actor_id)
        else:
            if app['tenant_id'] != self.settings.feishu_tenant_key:
                raise DeployError('VAULT_IDENTITY_CONFLICT', '应用密钥所属企业不匹配。')
            vault._dek(db, app)

    def validate_runtime_secrets(self, job, *, db=None):
        if self.settings.secret_backend == 'encrypted':
            from .vault_runtime import VaultRuntime
            VaultRuntime(self.settings).validate(job, db=db)

    def list_jobs(self, limit=50, *, actor=None):
        with self._connection() as db:
            params = []
            clause = ""
            if actor is not None:
                actor = AccessStore._actor(db, actor)
                if not actor["is_admin"]:
                    clause = """WHERE EXISTS (SELECT 1 FROM app_members m
                        WHERE m.app_id=jobs.app_id AND m.user_id=? AND m.enabled=1
                        AND m.role IN ('owner','maintainer'))"""
                    params.append(actor["id"])
            params.append(max(1, min(limit, 200)))
            return [self._job(row) for row in db.execute(
                f"SELECT * FROM jobs {clause} ORDER BY created_at DESC,id DESC LIMIT ?", params)]

    def apps(self):
        with self._connection() as db:
            return [self._app_view(db, row) for row in db.execute("SELECT * FROM apps ORDER BY created_at,id")]

    def _app_view(self, db, row):
        app = dict(row)
        last = app["subdomain"]
        if last is None:
            retained = self._retained_release(db, app["id"])
            recent = db.execute("SELECT * FROM jobs WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                                (app["id"],)).fetchone()
            job = retained or (self._job(recent) if recent else None)
            last = job["input"].get("subdomain") if job else None
        return {**app, "lastSubdomain": last, "domainBound": app["subdomain"] is not None}

    @staticmethod
    def _offline(row):
        if row is None:
            raise DeployError("NOT_FOUND", "Offline operation was not found.")
        result = dict(row)
        for key in ("execution", "error"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def _get_offline(self, db, operation_id):
        return self._offline(db.execute("SELECT * FROM offline_operations WHERE id=?", (operation_id,)).fetchone())

    def get_offline(self, operation_id):
        with self._connection() as db:
            return self._get_offline(db, operation_id)

    def list_offline_operations(self, active_only=False):
        clause = "WHERE status IN ('queued','running','recovery_required')" if active_only else ""
        with self._connection() as db:
            return [self._offline(row) for row in db.execute(
                f"SELECT * FROM offline_operations {clause} ORDER BY created_at,id"
            )]

    @staticmethod
    def _check_offline_gate(db, app_id, subdomain):
        if db.execute("""SELECT 1 FROM vault_revocations r JOIN apps a ON r.app_id=a.id
                WHERE r.status IN ('queued','running','recovery_required')
                AND (a.id=? OR a.subdomain=?) LIMIT 1""", (app_id, subdomain)).fetchone():
            raise DeployError('SECRET_REVOCATION_STATE', '密钥吊销尚未确认完成，应用暂不能部署、恢复或下线。')
        if db.execute("""SELECT operations.id FROM offline_operations AS operations
            JOIN apps ON apps.id=operations.app_id
            WHERE operations.status IN ('queued','running','recovery_required')
            AND (apps.id=? OR apps.subdomain=?) LIMIT 1""", (app_id, subdomain)).fetchone():
            raise DeployError("APP_OFFLINE_BUSY", "Application offline operation must finish first.")
        for row in db.execute("SELECT execution FROM offline_operations WHERE status IN ('queued','running','recovery_required')"):
            state = json.loads(row["execution"])
            if state.get("data_policy") == "delete" and any(
                scope.get("subdomain") == subdomain for scope in state["deletion_metadata"]["scopes"]
            ):
                raise DeployError("APP_OFFLINE_BUSY", "A historical application scope is being deleted.")

    def _offline_block_reason(self, db, app, *, ignore_operation=None):
        if db.execute("""SELECT 1 FROM vault_revocations WHERE app_id=?
                AND status IN ('queued','running','recovery_required') LIMIT 1""", (app['id'],)).fetchone():
            return 'SECRET_REVOCATION_STATE'
        if not app["active_release_id"]:
            return "OFFLINE_RELEASE_CHANGED"
        if db.execute("""SELECT id FROM offline_operations WHERE app_id=?
            AND status IN ('queued','running','recovery_required') AND id<>? LIMIT 1""",
            (app["id"], ignore_operation or "")).fetchone():
            return "APP_OFFLINE_BUSY"
        for row in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','recovery_required')"):
            job = self._job(row)
            if (job["app_id"] == app["id"] or job["input"].get("appId") == app["id"]
                    or job["input"].get("subdomain") == app["subdomain"]):
                return "APP_BUSY"
        release = self._get(db, app["active_release_id"])
        if release["status"] != "succeeded" or release["execution"].get("resources_released"):
            return "OFFLINE_STATE"
        if self.retirement_pending(release):
            return "APP_BUSY"
        return None

    @staticmethod
    def _storage_may_exist(job):
        state = job.get("execution") or {}
        manifest = (job.get("snapshot") or {}).get("manifest") or state.get("previous_manifest") or {}
        declarations = any(service.get("storage") for service in manifest.get("services", {}).values())
        if not declarations:
            return False
        early = {"queued", "source", "prepared", "freeze", "frozen", "reserved", "build", "built", "storage_handoff"}
        return (job["status"] == "succeeded" or job["phase"] not in early
                or any(state.get(key) for key in ("networks", "network_identities", "started", "port_forwards", "publish_intent")))

    def _deletion_metadata(self, db, app):
        """Freeze confirmed ownership and recorded volume instances, never inferred paths."""
        from .storage_migration import StorageMigrations
        migrations = StorageMigrations(self.settings)
        conflict = DeployError("OFFLINE_DATA_CONFLICT", "Historical application data cannot be safely isolated.")
        jobs = [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at,id")]
        jobs = [job for job in jobs if not self._data_cleared(job)]
        owned = [job for job in jobs if job["app_id"] == app["id"]]
        scopes = set()
        volumes = {}
        for job in owned:
            data = job["input"]
            scope = self._secret_scope_identity(data.get("repository"), data.get("subdomain"))
            scopes.add(scope)
            snapshot = job.get("snapshot") or {}
            manifest = snapshot.get("manifest") or job["execution"].get("previous_manifest") or {}
            if (snapshot.get("repository", scope[0]) != scope[0]
                    or manifest.get("app", {}).get("subdomain", scope[1]) != scope[1]
                    and snapshot.get("manifest")):
                raise conflict
            services = manifest.get("services") or {}
            records = job["execution"].get("storage_volumes", {})
            if not isinstance(records, dict) or set(records) - set(services):
                raise conflict
            for service, config in services.items():
                spec = config.get("storage")
                if not spec:
                    if service in records:
                        raise conflict
                    continue
                try:
                    name, _ = ManagedStorage.identity(app["id"], service, spec)
                except DeployError:
                    raise conflict from None
                expected = records.get(service)
                if expected is None:
                    if self._storage_may_exist(job):
                        raise conflict
                    continue
                expected = migrations.resolve(app['id'], service, spec, expected, db=db)
                if ManagedStorage.is_quota(expected):
                    try:
                        ManagedStorage(None).validate_reference(app["id"], service, spec, expected)
                    except DeployError:
                        raise conflict from None
                elif (not isinstance(expected, dict) or set(expected) != {"name", "created_at", "instance"}
                      or expected["name"] != name or not isinstance(expected["created_at"], str)
                      or not expected["created_at"].strip() or not isinstance(expected["instance"], str)
                      or not re.fullmatch(r"[0-9a-f]{32}", expected["instance"])):
                    raise conflict
                # Historical requested limits are not storage identity or an
                # authority to shrink the current helper-managed allocation.
                stable_spec = {key: spec[key] for key in ("name", "mountPath")}
                volume = {"service": service, "spec": stable_spec, "expected": expected}
                if name in volumes and volumes[name] != volume:
                    raise conflict
                volumes[name] = volume
        if not owned or app["active_release_id"] not in {job["id"] for job in owned}:
            raise conflict
        selected = {job["id"] for job in owned}
        domains = {domain for _, domain in scopes}
        for job in jobs:
            data = job["input"]
            if self.settings.secret_backend == 'encrypted' and job['id'] not in selected:
                # Ownership is an application identity after migration. A released
                # domain may now belong to another app without sharing its vault.
                continue
            same_scope = (data.get("repository"), data.get("subdomain")) in scopes
            if (job["status"] in {"queued", "running", "recovery_required"}
                    and (same_scope or data.get("subdomain") in domains)):
                raise DeployError("APP_BUSY", "Historical data scope has unfinished deployment work.")
            if not same_scope or job["id"] in selected:
                continue
            if job["app_id"] is not None:
                raise conflict
            if job["status"] == "failed":
                if (any(job["execution"].get(key) for key in ("ports", "storage_volumes", "networks", "port_forwards"))
                        or db.execute("SELECT 1 FROM reservations WHERE job_id=?", (job["id"],)).fetchone()):
                    raise conflict
                selected.add(job["id"])
        for _, domain in (scopes if self.settings.secret_backend != 'encrypted' else {(None, app['subdomain'])}):
            self._check_offline_gate(db, app["id"], domain)
        metadata = {"app_id": app["id"], "job_ids": sorted(selected),
                "scopes": [{"repository": repository, "subdomain": domain} for repository, domain in sorted(scopes)],
                "volumes": [volumes[name] for name in sorted(volumes)]}
        sources = migrations.retained_sources(app['id'], db=db)
        if sources:
            metadata['migration_sources'] = sources
        return metadata

    def enqueue_offline(self, app_id, expected_release_id, idempotency_key, *, data_policy="retain",
                        delete_confirmation=None, actor=None):
        request_key = idempotency_key
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise DeployError("INVALID_IDEMPOTENCY_KEY", "Provide an idempotency key of 1–200 characters.")
        if data_policy not in ("retain", "delete"):
            raise DeployError("OFFLINE_DATA_POLICY", "Choose retain or delete for application data.")
        if actor is None and getattr(self.settings, "auth_mode", "basic") == "feishu":
            raise denied()
        with self._connection(write=True) as db:
            if actor is not None:
                actor = AccessStore._actor(db, actor)
                AccessStore._require(db, actor, app_id, "delete" if data_policy == "delete" else "offline")
                idempotency_key = AccessStore.scoped_key(actor, "offline", idempotency_key)
            row = db.execute("SELECT * FROM offline_operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            existing = self._offline(row) if row else None
            if existing:
                if actor is not None and not offline_started(existing):
                    AccessStore._authorize_operation_local(db, "offline", existing["id"], required=True)
                if (existing["app_id"] != app_id or existing["target_release_id"] != expected_release_id
                        or existing["execution"].get("data_policy", "retain") != data_policy):
                    raise DeployError("IDEMPOTENCY_CONFLICT", "This key already belongs to a different request.")
                if (data_policy == "delete"
                        and delete_confirmation != "删除 " + self._get(db, expected_release_id)["input"]["subdomain"]):
                    raise DeployError("OFFLINE_DELETE_CONFIRMATION", "Type the exact application deletion confirmation.")
                if existing["status"] != "recovery_required":
                    return existing
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise DeployError("NOT_FOUND", "Application was not found.")
            app = dict(row)
            if data_policy == "delete" and delete_confirmation != "删除 " + str(app["subdomain"]):
                raise DeployError("OFFLINE_DELETE_CONFIRMATION", "Type the exact application deletion confirmation.")
            if not expected_release_id or app["active_release_id"] != expected_release_id:
                raise DeployError("OFFLINE_RELEASE_CHANGED", "The active release changed; refresh before taking it offline.")
            reason = self._offline_block_reason(db, app, ignore_operation=existing["id"] if existing else None)
            if reason:
                raise DeployError(reason, "Application has unfinished deployment or offline work.")
            now = time.time()
            if existing:
                # The same confirmed request explicitly retries the durable journal.
                db.execute("UPDATE offline_operations SET status='queued',error=NULL,updated_at=? WHERE id=?",
                           (now, existing["id"]))
                return self._get_offline(db, existing["id"])
            operation_id = uuid.uuid4().hex
            state = {"data_policy": data_policy}
            if data_policy == "delete":
                state["deletion_metadata"] = self._deletion_metadata(db, app)
            db.execute("""INSERT INTO offline_operations
                (id,idempotency_key,app_id,target_release_id,status,phase,execution,created_at,updated_at)
                VALUES (?,?,?,?,'queued','offline_queued',?,?,?)""",
                (operation_id, idempotency_key, app_id, expected_release_id, _json(state), now, now))
            if actor is not None:
                AccessStore.record_operation(db, actor, "offline", operation_id, app_id,
                                             "delete" if data_policy == "delete" else "offline", request_key)
            return self._get_offline(db, operation_id)

    def next_offline(self):
        with self._connection() as db:
            row = db.execute("""SELECT * FROM offline_operations WHERE status IN ('queued','running')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,created_at,id LIMIT 1""").fetchone()
            return self._offline(row) if row else None

    def claim_offline(self, operation_id):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if operation["status"] not in {"queued", "running"}:
                raise DeployError("OFFLINE_STATE", "This offline operation cannot be started.")
            app = db.execute("SELECT * FROM apps WHERE id=?", (operation["app_id"],)).fetchone()
            if app is None or app["active_release_id"] != operation["target_release_id"]:
                raise DeployError("OFFLINE_RELEASE_CHANGED", "The active release changed before offline execution.")
            reason = self._offline_block_reason(db, app, ignore_operation=operation_id)
            if reason:
                raise DeployError(reason, "Application is not ready to go offline.")
            db.execute("UPDATE offline_operations SET status='running',phase='offline',updated_at=? WHERE id=?",
                       (time.time(), operation_id))
            return self._get_offline(db, operation_id)

    def checkpoint_offline(self, operation_id, execution):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if operation["status"] != "running":
                raise DeployError("OFFLINE_STATE", "Only a running offline operation may checkpoint.")
            patch = dict(execution)
            if patch.get("authorization_started") is True and not offline_started(operation):
                AccessStore._authorize_operation_local(db, "offline", operation_id,
                    required=getattr(self.settings, "auth_mode", "basic") == "feishu")
                AccessStore._audit(db, "SYSTEM", "offline_cleanup_started", operation["app_id"], operation_id)
            if operation["execution"].get("authorization_started") and patch.get("authorization_started", True) is not True:
                raise DeployError("OFFLINE_STATE", "Started offline cleanup cannot be cancelled.")
            for key in ("data_policy", "deletion_metadata"):
                if key in patch and patch[key] != operation["execution"].get(key, "retain" if key == "data_policy" else None):
                    raise DeployError("OFFLINE_STATE", "The confirmed deletion plan cannot change.")
            if ("deletion_plan" in operation["execution"] and "deletion_plan" in patch
                    and patch["deletion_plan"] != operation["execution"]["deletion_plan"]):
                raise DeployError("OFFLINE_STATE", "The frozen data inventory cannot change.")
            phase = patch.pop("phase", operation["phase"])
            state = {**operation["execution"], **patch}
            db.execute("UPDATE offline_operations SET phase=?,execution=?,updated_at=? WHERE id=?",
                       (phase, _json(state), time.time(), operation_id))
            return self._get_offline(db, operation_id)

    def fail_offline(self, operation_id, error):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if operation["status"] == "succeeded":
                raise DeployError("OFFLINE_STATE", "A completed offline operation cannot fail.")
            db.execute("UPDATE offline_operations SET status='recovery_required',error=?,updated_at=? WHERE id=?",
                       (_json(error), time.time(), operation_id))
            return self._get_offline(db, operation_id)

    def cancel_offline(self, operation_id, error):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if offline_started(operation):
                raise DeployError("OFFLINE_STATE", "Started offline cleanup cannot be cancelled.")
            db.execute("UPDATE offline_operations SET status='failed',error=?,updated_at=? WHERE id=?",
                       (_json(error), time.time(), operation_id))
            return self._get_offline(db, operation_id)

    def finish_offline(self, operation_id):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if operation["status"] == "succeeded":
                return operation
            if operation["status"] != "running" or not all(operation["execution"].get(key) is True for key in
                    ("gateway_deactivated", "containers_stopped", "cleanup_confirmed")):
                raise DeployError("OFFLINE_STATE", "Offline completion requires confirmed route and runtime cleanup.")
            deleting = operation["execution"].get("data_policy", "retain") == "delete"
            if deleting and operation["execution"].get("data_deleted") is not True:
                raise DeployError("OFFLINE_STATE", "Data deletion must be confirmed before releasing the application.")
            app = db.execute("SELECT * FROM apps WHERE id=?", (operation["app_id"],)).fetchone()
            if app is None or app["active_release_id"] != operation["target_release_id"]:
                raise DeployError("OFFLINE_RELEASE_CHANGED", "The active release changed before offline completion.")
            job = self._get(db, operation["target_release_id"])
            now = time.time()
            state = {**job["execution"], "resources_released": True, "offline_operation": operation_id}
            if deleting:
                metadata = operation["execution"]["deletion_metadata"]
                if metadata["app_id"] != app["id"] or job["id"] not in metadata["job_ids"]:
                    raise DeployError("OFFLINE_DATA_CONFLICT", "The frozen deletion ownership changed.")
                scopes = {(scope["repository"], scope["subdomain"]) for scope in metadata["scopes"]}
                for job_id in metadata["job_ids"]:
                    historical = self._get(db, job_id)
                    if (historical["app_id"] not in (None, app["id"])
                            or historical["app_id"] is None and historical["status"] != "failed"
                            or (historical["input"].get("repository"), historical["input"].get("subdomain")) not in scopes):
                        raise DeployError("OFFLINE_DATA_CONFLICT", "The frozen deletion ownership changed.")
                    cleared = {**historical["execution"], "data_deleted": True, "data_reset_operation": operation_id}
                    db.execute("UPDATE jobs SET execution=?,updated_at=? WHERE id=?", (_json(cleared), now, job_id))
                state.update(data_deleted=True, data_reset_operation=operation_id)
                for repository, subdomain in scopes:
                    db.execute("DELETE FROM app_secret_scopes WHERE app_id=? AND repository=? AND subdomain=?",
                               (app["id"], repository, subdomain))
            db.execute("UPDATE apps SET active_release_id=NULL,subdomain=NULL,updated_at=? WHERE id=?", (now, app["id"]))
            db.execute("DELETE FROM reservations WHERE job_id=?", (job["id"],))
            db.execute("UPDATE jobs SET execution=?,updated_at=? WHERE id=?", (_json(state), now, job["id"]))
            db.execute("UPDATE offline_operations SET status='succeeded',phase='offline_complete',error=NULL,updated_at=? WHERE id=?",
                       (now, operation_id))
            return self._get_offline(db, operation_id)

    def _retained_release(self, db, app_id):
        row = db.execute("""SELECT jobs.*,operations.created_at AS retained_at FROM offline_operations AS operations
            JOIN jobs ON jobs.id=operations.target_release_id
            WHERE operations.app_id=? AND operations.status='succeeded'
            ORDER BY operations.created_at DESC,operations.id DESC LIMIT 1""", (app_id,)).fetchone()
        retained = self._job(row) if row else None
        retained_at = retained.pop('retained_at') if retained is not None else 0
        # Revocation removes runtime resources, but retains application data and
        # its exact volume identities just like a retain-data offline operation.
        for item in db.execute('SELECT * FROM jobs WHERE app_id=? ORDER BY updated_at DESC,id DESC', (app_id,)):
            candidate = self._job(item)
            if (candidate['execution'].get('secret_revoked') and candidate['execution'].get('resources_released')
                    and (candidate['status'] == 'succeeded' or candidate['execution'].get('storage_volumes'))
                    and (retained is None or candidate['updated_at'] > retained_at)):
                return candidate
        return retained

    def management_snapshot(self):
        with self._connection() as db:
            db.execute("BEGIN")
            operations = [self._offline(row) for row in db.execute(
                "SELECT * FROM offline_operations ORDER BY created_at,id")]
            latest = {operation["app_id"]: operation for operation in operations}
            apps = []
            for row in db.execute("SELECT * FROM apps ORDER BY created_at,id"):
                app = self._app_view(db, row)
                reason = self._offline_block_reason(db, app)
                app.update(active_job=self._get(db, app["active_release_id"]) if app["active_release_id"] else None,
                           retained_job=self._retained_release(db, app["id"]), operation=latest.get(app["id"]),
                           can_offline=reason is None, offline_block_reason=reason)
                apps.append(app)
            return {"apps": apps, "deployments": [self._job(row) for row in db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','recovery_required') ORDER BY created_at,id")],
                "offline_operations": [operation for operation in operations if operation["status"] != "succeeded"]}

    def active_release(self, app_id):
        with self._connection() as db:
            row = db.execute(
                """SELECT jobs.* FROM jobs JOIN apps ON apps.active_release_id=jobs.id
                WHERE apps.id=? AND jobs.status='succeeded'""",
                (app_id,),
            ).fetchone()
            return self._job(row) if row else None

    def next_active_recovery(self, boot_id, now=None):
        now = time.time() if now is None else now
        with self._connection() as db:
            jobs = [
                self._job(row)
                for row in db.execute("""SELECT jobs.* FROM jobs
                JOIN apps ON apps.active_release_id=jobs.id WHERE jobs.status='succeeded'
                ORDER BY jobs.created_at,jobs.id""")
            ]
            blocked = {
                pending["execution"].get("previous_release")
                for row in db.execute("SELECT * FROM jobs WHERE status IN ('running','recovery_required')")
                if (pending := self._job(row))["execution"].get("storage_handoff_intent")
            }
            blocked.update(row[0] for row in db.execute("""SELECT target_release_id FROM offline_operations
                WHERE status IN ('queued','running','recovery_required')"""))
            blocked.update(row[0] for row in db.execute("""SELECT a.active_release_id FROM apps a
                JOIN vault_revocations r ON r.app_id=a.id WHERE r.status IN ('queued','running','recovery_required')"""))
        due = []
        for job in jobs:
            if job["id"] in blocked:
                continue
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                continue
            state = job["execution"].get("active_recovery", {})
            different_boot = state.get("boot_id") != boot_id
            if different_boot or state.get("next_at", 0) <= now:
                due.append((not different_boot, state.get("next_at", 0), job["created_at"], job["id"], job))
        return min(due, key=lambda item: item[:4])[-1] if due else None

    def _require_active(self, db, job_id, *, allow_offline_pending=False):
        job = self._get(db, job_id)
        if not allow_offline_pending:
            self._check_offline_gate(db, job["app_id"], job["input"].get("subdomain"))
        if (
            job["status"] != "succeeded"
            or job["execution"].get("resources_released")
            or job["execution"].get("failure_intent")
            or not db.execute(
                "SELECT id FROM apps WHERE active_release_id=? AND id=?", (job_id, job["app_id"])
            ).fetchone()
        ):
            raise DeployError(
                "ACTIVE_RECOVERY_STATE", "Only the current successful release may be recovered."
            )
        return job

    def begin_active_recovery(self, job_id, boot_id, now=None):
        now = time.time() if now is None else now
        with self._connection(write=True) as db:
            job = self._require_active(db, job_id)
            self.validate_runtime_secrets(job, db=db)
            previous = job["execution"].get("active_recovery", {})
            attempts = min(previous.get("attempts", 0) + 1, 16)
            state = {
                **job["execution"],
                "active_recovery": {
                    "boot_id": boot_id,
                    "attempts": attempts,
                    "outcome": "checking",
                    "retry_health": previous.get("retry_health", False)
                    or previous.get("outcome") in {"checking", "failed"},
                    "next_at": now + min(60 * 2 ** (attempts - 1), 900),
                },
            }
            db.execute("UPDATE jobs SET execution=?,updated_at=? WHERE id=?", (_json(state), now, job_id))
            return self._get(db, job_id)

    def finish_active_recovery(self, job_id, *, success, restarted=None, now=None):
        now = time.time() if now is None else now
        with self._connection(write=True) as db:
            # A request may arrive while the worker is already checking this release.
            # Finishing bookkeeping starts nothing; the same worker lock then handles offline.
            job = self._require_active(db, job_id, allow_offline_pending=True)
            if success:
                self.validate_runtime_secrets(job, db=db)
            previous = job["execution"].get("active_recovery", {})
            attempts = 0 if success else min(max(previous.get("attempts", 1), 1), 16)
            state = {
                **job["execution"],
                "active_recovery": {
                    "boot_id": previous.get("boot_id"),
                    "attempts": attempts,
                    "outcome": ("restarted" if restarted else "healthy") if success else "failed",
                    "next_at": now + (60 if success else min(60 * 2 ** (attempts - 1), 900)),
                },
            }
            db.execute("UPDATE jobs SET execution=?,updated_at=? WHERE id=?", (_json(state), now, job_id))
            return self._get(db, job_id)

    def next_job(self):
        with self._connection() as db:
            row = db.execute("""SELECT * FROM jobs WHERE status IN ('queued','running')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,created_at,id LIMIT 1""").fetchone()
            return self._job(row) if row else None

    @staticmethod
    def retirement_pending(job):
        state = job["execution"]
        # Also recover releases written before the durable retirement queue existed.
        legacy = (
            [state["previous_release"]]
            if state.get("previous_release") and not state.get("previous_retired")
            else []
        )
        return list(state.get("retirement_pending", legacy))

    def next_retirement(self):
        """Only the current successful release owns an application's retirement backlog."""
        with self._connection() as db:
            rows = db.execute("""SELECT jobs.* FROM jobs JOIN apps ON apps.active_release_id=jobs.id
                WHERE jobs.status='succeeded' ORDER BY jobs.created_at,jobs.id""")
            for row in rows:
                job = self._job(row)
                if (
                    self.retirement_pending(job)
                    and job["execution"].get("retirement_retry_at", 0) <= time.time()
                ):
                    return job
            return None

    def claim(self, job_id):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            cleanup = any(job["execution"].get(key) for key in (
                "failure_intent", "resources_released", "storage_handoff_intent", "publish_intent"))
            if not cleanup:
                AccessStore._authorize_operation_local(db, "job", job_id,
                    required=getattr(self.settings, "auth_mode", "basic") == "feishu")
            if job["status"] not in ("queued", "running"):
                raise DeployError("JOB_STATE", "This job cannot be started.")
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (time.time(), job_id))
            return self._get(db, job_id)

    def checkpoint(self, job_id, phase=None, execution=None, snapshot=None):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            state = job["execution"]
            patch = dict(execution or {})
            if state.get("commit") and patch.get("commit", state["commit"]) != state["commit"]:
                raise DeployError("PIN_CONFLICT", "The deployment commit cannot change after it is pinned.")
            if "images" in patch:
                patch["images"] = {**state.get("images", {}), **patch["images"]}
            state.update(patch)
            db.execute(
                "UPDATE jobs SET phase=?,execution=?,snapshot=?,updated_at=? WHERE id=?",
                (
                    phase or job["phase"],
                    _json(state),
                    _json(snapshot)
                    if snapshot is not None
                    else _json(job["snapshot"])
                    if job["snapshot"]
                    else None,
                    time.time(),
                    job_id,
                ),
            )
            return self._get(db, job_id)

    def _app_context(self, db, job, create=False):
        if self._data_cleared(job):
            raise DeployError("JOB_STATE", "A data-cleared historical deployment cannot be reused.")
        payload = job["input"]
        app_id = job["app_id"] or payload.get("appId")
        self._check_offline_gate(db, app_id, payload.get("subdomain"))
        if app_id:
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if not row:
                raise DeployError("NOT_FOUND", "Application was not found.")
            app = dict(row)
            if (app["repository"] != payload["repository"]
                    or app["subdomain"] is not None and app["subdomain"] != payload["subdomain"]):
                raise DeployError(
                    "APP_IDENTITY_CONFLICT", "Repository and subdomain must match the application."
                )
            if app["active_release_id"] and app["subdomain"] is None:
                raise DeployError("APP_IDENTITY_CONFLICT", "An active application must retain its bound subdomain.")
            recovery = db.execute(
                "SELECT id FROM jobs WHERE app_id=? AND status='recovery_required' AND id<>?",
                (app_id, job["id"]),
            ).fetchone()
            if recovery:
                raise DeployError("RECOVERY_REQUIRED", "This application requires deployment recovery first.")
            occupied = db.execute("SELECT id FROM apps WHERE subdomain=? AND id<>?",
                                  (payload["subdomain"], app_id)).fetchone()
            if occupied:
                raise DeployError("DOMAIN_CONFLICT", "This subdomain belongs to an existing application.")
            if create and app["subdomain"] is None:
                db.execute("UPDATE apps SET subdomain=?,updated_at=? WHERE id=?",
                           (payload["subdomain"], time.time(), app_id))
                app["subdomain"] = payload["subdomain"]
            return app
        row = db.execute("SELECT id FROM apps WHERE subdomain=?", (payload["subdomain"],)).fetchone()
        if row:
            raise DeployError("DOMAIN_CONFLICT", "This subdomain belongs to an existing application.")
        if not create:
            return None
        app_id, now = uuid.uuid4().hex, time.time()
        db.execute(
            "INSERT INTO apps VALUES (?,?,?,?,?,?)",
            (app_id, payload["repository"], payload["subdomain"], None, now, now),
        )
        return dict(db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone())

    def prepare_context(self, job_id):
        """Resolve application ownership and previous defaults before source preparation."""
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            app = self._app_context(db, job)
            state = job["execution"]
            if app and "previous_release" not in state:
                previous_id = app["active_release_id"]
                state["previous_release"] = previous_id
                if previous_id:
                    previous = self._get(db, previous_id)
                    state["previous_manifest"] = previous["snapshot"]["manifest"]
                else:
                    retained = self._retained_release(db, app["id"])
                    if retained and not self._data_cleared(retained):
                        state["previous_manifest"] = retained["snapshot"]["manifest"]
                        state["storage_volumes"] = retained["execution"].get("storage_volumes", {})
                db.execute(
                    "UPDATE jobs SET app_id=?,execution=?,updated_at=? WHERE id=?",
                    (app["id"], _json(state), time.time(), job_id),
                )
            return self._get(db, job_id)

    @staticmethod
    def _available(port):
        # Advisory probe only: closing this socket does not reserve the OS port.
        # Database reservations and the worker lock coordinate platform jobs, not
        # external processes; a later Docker bind failure follows failure cleanup.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def reserve(self, job_id, snapshot):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            AccessStore._authorize_operation_local(db, "job", job_id,
                required=getattr(self.settings, "auth_mode", "basic") == "feishu")
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                raise DeployError(
                    "JOB_STATE", "A cleaned or failing deployment cannot reserve resources again."
                )
            app = self._app_context(db, job, create=True)
            # freeze reads only version identifiers. Enforce their application
            # ownership before every reservation, including a frozen job replay.
            self._check_secret_scope(db, app, job, snapshot)
            existing = list(db.execute("SELECT service,port FROM reservations WHERE job_id=?", (job_id,)))
            services = snapshot["manifest"]["services"]
            resources = [
                (name, float(service["resources"]["cpu"]), int(service["resources"]["memoryMiB"]))
                for name, service in services.items()
            ]
            app_memory = db.execute(
                """SELECT COALESCE(SUM(reservations.memory_mib),0)
                   FROM reservations JOIN jobs ON jobs.id=reservations.job_id
                   WHERE jobs.app_id=?""", (app["id"],)
            ).fetchone()[0]
            # Replays still enforce the current application budget, without
            # counting this job's already reserved services a second time.
            additional_memory = 0 if existing else sum(memory for _, _, memory in resources)
            if app_memory + additional_memory > self.settings.max_app_memory_mib:
                raise DeployError(
                    "RESOURCE_QUOTA", "Active and candidate releases exceed the application memory quota."
                )
            if existing:
                return job
            used = db.execute(
                "SELECT COALESCE(SUM(cpu),0),COALESCE(SUM(memory_mib),0) FROM reservations"
            ).fetchone()
            if (
                used[0] + sum(cpu for _, cpu, _ in resources) > self.settings.max_cpu
                or used[1] + sum(memory for _, _, memory in resources) > self.settings.max_memory_mib
            ):
                raise DeployError(
                    "RESOURCE_QUOTA", "Active and candidate releases exceed the resource quota."
                )
            assigned = {row[0] for row in db.execute("SELECT port FROM reservations")}
            ports = {}
            candidates = iter(range(self.settings.port_start, self.settings.port_end + 1))
            for name, cpu, memory in resources:
                port = next(
                    (port for port in candidates if port not in assigned and self._available(port)), None
                )
                if port is None:
                    raise DeployError("PORTS_EXHAUSTED", "No available host ports remain.")
                ports[name] = port
                db.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (job_id, name, port, cpu, memory))
            state = job["execution"]
            state["ports"] = ports
            state["previous_release"] = app["active_release_id"]
            if app["active_release_id"]:
                state["previous_manifest"] = self._get(db, app["active_release_id"])["snapshot"]["manifest"]
            else:
                retained = self._retained_release(db, app["id"])
                if retained and not self._data_cleared(retained):
                    state["previous_manifest"] = retained["snapshot"]["manifest"]
                    state["storage_volumes"] = retained["execution"].get("storage_volumes", {})
            db.execute(
                "UPDATE jobs SET app_id=?,snapshot=?,execution=?,phase='reserved',updated_at=? WHERE id=?",
                (app["id"], _json(snapshot), _json(state), time.time(), job_id),
            )
            if self.settings.secret_backend == 'encrypted':
                from .secret_vault import SecretVault
                for service, versions in snapshot['secret_versions'].items():
                    for name, version in versions.items():
                        SecretVault._audit(db, 'SYSTEM', 'secret_version_bound', app['id'], name, version,
                                           {'job_id': job_id, 'service': service})
            return self._get(db, job_id)

    def _check_secret_scope(self, db, app, job, snapshot):
        if self.settings.secret_backend == 'encrypted':
            self.validate_runtime_secrets({**job, 'app_id': app['id'], 'snapshot': snapshot}, db=db)
            return
        wanted = {(name, version) for versions in snapshot.get("secret_versions", {}).values()
                  for name, version in versions.items()}
        if not wanted:
            return
        # Secret files are scoped by repository/domain, not app identity. A file
        # may have been rotated or never referenced by the old manifest, so a
        # historical snapshot cannot prove that any current version is new. A
        # successful consumer may, however, reuse the exact versions it used.
        shared, proven = False, set()
        for row in db.execute("SELECT * FROM jobs WHERE app_id IS NOT NULL AND id<>?", (job["id"],)):
            previous = self._job(row)
            if self._data_cleared(previous):
                continue
            if (previous["input"].get("repository") != job["input"]["repository"]
                    or previous["input"].get("subdomain") != job["input"]["subdomain"]):
                continue
            if previous["app_id"] != app["id"]:
                # Deleted apps and failed jobs still establish a shared scope.
                shared = True
                continue
            if previous["status"] != "succeeded":
                continue
            frozen = previous.get("snapshot") or {}
            manifest = frozen.get("manifest") or {}
            if (frozen.get("repository") != job["input"]["repository"]
                    or manifest.get("app", {}).get("subdomain") != job["input"]["subdomain"]):
                continue
            for service, versions in frozen.get("secret_versions", {}).items():
                refs = manifest.get("services", {}).get(service, {}).get("secretRefs") or {}
                proven.update((name, version) for name, version in versions.items() if name in refs.values())
        if shared and not wanted <= proven:
            raise DeployError("SECRET_SCOPE_CONFLICT", "This secret scope was used by another application.",
                              "secretRefs", "Use the original application or configure secrets for a different free subdomain.")

    def finish(self, job_id):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            AccessStore._authorize_operation_local(db, "job", job_id,
                required=getattr(self.settings, "auth_mode", "basic") == "feishu")
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                raise DeployError("JOB_STATE", "A cleaned or failing deployment cannot become active.")
            if not job["app_id"] or not job["execution"].get("ports"):
                raise DeployError("JOB_STATE", "An unreserved deployment cannot become active.")
            if job["status"] not in ("queued", "running", "succeeded"):
                raise DeployError("JOB_STATE", "A failed deployment cannot become active.")
            self.validate_runtime_secrets(job, db=db)
            app = db.execute("SELECT subdomain FROM apps WHERE id=?", (job["app_id"],)).fetchone()
            if app is None or app["subdomain"] != job["input"]["subdomain"]:
                raise DeployError("DOMAIN_CONFLICT", "The candidate no longer owns its requested subdomain.")
            now = time.time()
            state = job["execution"]
            previous_id = state.get("previous_release")
            pending = []
            if previous_id:
                previous = self._get(db, previous_id)
                pending = list(dict.fromkeys([previous_id, *self.retirement_pending(previous)]))
            state.update(retirement_pending=pending, retirement_retry_at=0, previous_retired=not pending)
            db.execute(
                "UPDATE apps SET active_release_id=?,updated_at=? WHERE id=?", (job_id, now, job["app_id"])
            )
            db.execute(
                "UPDATE jobs SET status='succeeded',phase='complete',error=NULL,execution=?,updated_at=? WHERE id=?",
                (_json(state), now, job_id),
            )
            return self._get(db, job_id)

    def confirm_secret_revocation_release(self, operation_id, job_id):
        """Commit only after worker proved this fixed release has no traffic or containers."""
        with self._connection(write=True) as db:
            operation = db.execute('SELECT * FROM vault_revocations WHERE id=?', (operation_id,)).fetchone()
            job = self._get(db, job_id)
            if (not operation or operation['status'] != 'running' or job['app_id'] != operation['app_id']
                    or job_id not in json.loads(operation['execution'])['job_ids']):
                raise DeployError('SECRET_REVOCATION_STATE', '待清理版本不属于已受理的吊销范围。')
            db.execute('UPDATE apps SET active_release_id=NULL,updated_at=? WHERE id=? AND active_release_id=?',
                       (time.time(), job['app_id'], job_id))
            db.execute('DELETE FROM reservations WHERE job_id=?', (job_id,))
            if not db.execute('SELECT 1 FROM reservations JOIN jobs ON jobs.id=reservations.job_id WHERE jobs.app_id=?',
                              (job['app_id'],)).fetchone():
                db.execute('UPDATE apps SET subdomain=NULL,updated_at=? WHERE id=? AND active_release_id IS NULL',
                           (time.time(), job['app_id']))
            state = {**job['execution'], 'resources_released': True, 'secret_revoked': True,
                     'secret_revocation_id': operation_id}
            status = job['status'] if job['status'] == 'succeeded' else 'failed'
            error = {'code': 'SECRET_REVOKED', 'message': '该部署引用的密钥版本已吊销，实例已停止。',
                     'path': 'secretRefs', 'suggestion': '配置新的有效版本后重新部署。'}
            db.execute('UPDATE jobs SET status=?,execution=?,error=?,updated_at=? WHERE id=?',
                       (status, _json(state), _json(error), time.time(), job_id))
            return self._get(db, job_id)

    def fail(self, job_id, error, recovery=False):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            if job["status"] == "succeeded":
                raise DeployError("JOB_STATE", "An active successful release cannot be failed.")
            db.execute(
                "UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?",
                ("recovery_required" if recovery else "failed", _json(error), time.time(), job_id),
            )
            return self._get(db, job_id)

    def release(self, job_id):
        """Release only after the caller has positively confirmed backend cleanup."""
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            if db.execute("SELECT id FROM apps WHERE active_release_id=?", (job_id,)).fetchone():
                raise DeployError("ACTIVE_RELEASE", "The current active release cannot be removed.")
            db.execute("DELETE FROM reservations WHERE job_id=?", (job_id,))
            if job["app_id"] and not job["execution"].get("resources_released"):
                remaining = db.execute("""SELECT 1 FROM reservations JOIN jobs ON jobs.id=reservations.job_id
                    WHERE jobs.app_id=? LIMIT 1""", (job["app_id"],)).fetchone()
                if not remaining:
                    # cleanup/rollback has been confirmed by the caller. Never clear
                    # a live release's name, another candidate's claim, or a later replay.
                    db.execute("""UPDATE apps SET subdomain=NULL,updated_at=?
                        WHERE id=? AND active_release_id IS NULL AND subdomain=?""",
                        (time.time(), job["app_id"], job["input"].get("subdomain")))
            state = {**job["execution"], "resources_released": True}
            # A candidate cleanup and its terminal status commit together, so a
            # crash cannot leave a released candidate eligible for deployment.
            status = "failed" if state.get("failure_intent") else job["status"]
            error = state.get("failure_error", job["error"])
            db.execute(
                "UPDATE jobs SET execution=?,status=?,error=?,updated_at=? WHERE id=?",
                (_json(state), status, _json(error) if error else None, time.time(), job_id),
            )
            return self._get(db, job_id)

    def event(self, job_id, phase, message):
        # Do not persist command output; redact common credentials defensively as well.
        message = re.sub(r"[\x00-\x1f\x7f]", " ", str(message))
        message = re.sub(r"(?i)(token|password|secret|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", message)
        message = re.sub(r"https?://\S+@", "[redacted]@", message)[:512]
        with self._connection(write=True) as db:
            self._get(db, job_id)
            db.execute(
                "INSERT INTO events(job_id,phase,message,created_at) VALUES (?,?,?,?)",
                (job_id, re.sub(r"[^a-zA-Z0-9_ -]", "", str(phase))[:64], message, time.time()),
            )

    def events(self, job_id):
        with self._connection() as db:
            self._get(db, job_id)
            return [
                dict(row) for row in db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))
            ]
