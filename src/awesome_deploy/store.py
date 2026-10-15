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


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, settings, *, verified_offline_domains=None):
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
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

    @contextmanager
    def secret_configuration_scope(self, job_id, *, write=False):
        """Authorize a new secret writer independently of old-version reuse rules.

        The caller holds worker.lock before requesting a write transaction, and
        keeps this transaction open until all create-only filesystem writes end.
        Read transactions authorize metadata from a single consistent DB view.
        """
        with self._connection(write=write) as db:
            if not write:
                db.execute("BEGIN")
            job = self._get(db, job_id)
            if (job["status"] != "failed" or not isinstance(job.get("error"), dict)
                    or job["error"].get("code") != "SECRET_NOT_FOUND"
                    or not isinstance(job.get("input"), dict)):
                raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "该任务不能补充秘密配置。")
            can_generate = self._authorize_secret_configuration(db, job)
            yield job, can_generate

    def _authorize_secret_configuration(self, db, job):
        payload = job["input"]
        repository, subdomain = payload.get("repository"), payload.get("subdomain")
        if not isinstance(repository, str) or not isinstance(subdomain, str):
            raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "该任务不能补充秘密配置。")
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
        for row in db.execute("SELECT * FROM jobs"):
            other = self._job(row)
            data = other.get("input")
            if not isinstance(data, dict):
                raise DeployError("SECRET_CONFIGURATION_NOT_AVAILABLE", "无法核验配置空间的历史归属。")
            identities = {value for value in (other.get("app_id"), data.get("appId")) if isinstance(value, str) and value}
            same_scope = data.get("repository") == repository and data.get("subdomain") == subdomain
            same_owner = owner is not None and owner in identities
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
        return owner is None

    def enqueue(self, payload, idempotency_key):
        encoded = _json(payload)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise DeployError("INVALID_IDEMPOTENCY_KEY", "Provide an idempotency key of 1–200 characters.")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if row["input"] != encoded:
                    raise DeployError(
                        "IDEMPOTENCY_CONFLICT", "This key already belongs to a different request."
                    )
                return self._job(row)
            self._check_offline_gate(db, payload.get("appId"), payload.get("subdomain"))
            job_id, now = uuid.uuid4().hex, time.time()
            db.execute(
                """INSERT INTO jobs
                (id,idempotency_key,input,status,phase,created_at,updated_at)
                VALUES (?,?,?,'queued','queued',?,?)""",
                (job_id, idempotency_key, encoded, now, now),
            )
            return self._get(db, job_id)

    def list_jobs(self, limit=50):
        with self._connection() as db:
            return [
                self._job(row)
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT ?", (max(1, min(limit, 200)),)
                )
            ]

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
        if db.execute("""SELECT operations.id FROM offline_operations AS operations
            JOIN apps ON apps.id=operations.app_id
            WHERE operations.status IN ('queued','running','recovery_required')
            AND (apps.id=? OR apps.subdomain=?) LIMIT 1""", (app_id, subdomain)).fetchone():
            raise DeployError("APP_OFFLINE_BUSY", "Application offline operation must finish first.")

    def _offline_block_reason(self, db, app, *, ignore_operation=None):
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

    def enqueue_offline(self, app_id, expected_release_id, idempotency_key):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise DeployError("INVALID_IDEMPOTENCY_KEY", "Provide an idempotency key of 1–200 characters.")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM offline_operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            existing = self._offline(row) if row else None
            if existing:
                if existing["app_id"] != app_id or existing["target_release_id"] != expected_release_id:
                    raise DeployError("IDEMPOTENCY_CONFLICT", "This key already belongs to a different request.")
                if existing["status"] != "recovery_required":
                    return existing
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise DeployError("NOT_FOUND", "Application was not found.")
            app = dict(row)
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
            db.execute("""INSERT INTO offline_operations
                (id,idempotency_key,app_id,target_release_id,status,phase,created_at,updated_at)
                VALUES (?,?,?,?,'queued','offline_queued',?,?)""",
                (operation_id, idempotency_key, app_id, expected_release_id, now, now))
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

    def finish_offline(self, operation_id):
        with self._connection(write=True) as db:
            operation = self._get_offline(db, operation_id)
            if operation["status"] == "succeeded":
                return operation
            if operation["status"] != "running" or not all(operation["execution"].get(key) is True for key in
                    ("gateway_deactivated", "containers_stopped", "cleanup_confirmed")):
                raise DeployError("OFFLINE_STATE", "Offline completion requires confirmed route and runtime cleanup.")
            app = db.execute("SELECT * FROM apps WHERE id=?", (operation["app_id"],)).fetchone()
            if app is None or app["active_release_id"] != operation["target_release_id"]:
                raise DeployError("OFFLINE_RELEASE_CHANGED", "The active release changed before offline completion.")
            job = self._get(db, operation["target_release_id"])
            now = time.time()
            state = {**job["execution"], "resources_released": True, "offline_operation": operation_id}
            db.execute("UPDATE apps SET active_release_id=NULL,subdomain=NULL,updated_at=? WHERE id=?", (now, app["id"]))
            db.execute("DELETE FROM reservations WHERE job_id=?", (job["id"],))
            db.execute("UPDATE jobs SET execution=?,updated_at=? WHERE id=?", (_json(state), now, job["id"]))
            db.execute("UPDATE offline_operations SET status='succeeded',phase='offline_complete',error=NULL,updated_at=? WHERE id=?",
                       (now, operation_id))
            return self._get_offline(db, operation_id)

    def _retained_release(self, db, app_id):
        row = db.execute("""SELECT jobs.* FROM offline_operations AS operations
            JOIN jobs ON jobs.id=operations.target_release_id
            WHERE operations.app_id=? AND operations.status='succeeded'
            ORDER BY operations.created_at DESC,operations.id DESC LIMIT 1""", (app_id,)).fetchone()
        return self._job(row) if row else None

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
                    if retained:
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
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                raise DeployError(
                    "JOB_STATE", "A cleaned or failing deployment cannot reserve resources again."
                )
            app = self._app_context(db, job, create=True)
            # freeze reads only version identifiers. Enforce their application
            # ownership before every reservation, including a frozen job replay.
            self._check_secret_scope(db, app, job, snapshot)
            existing = list(db.execute("SELECT service,port FROM reservations WHERE job_id=?", (job_id,)))
            if existing:
                return job
            services = snapshot["manifest"]["services"]
            resources = [
                (name, float(service["resources"]["cpu"]), int(service["resources"]["memoryMiB"]))
                for name, service in services.items()
            ]
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
                if retained:
                    state["previous_manifest"] = retained["snapshot"]["manifest"]
                    state["storage_volumes"] = retained["execution"].get("storage_volumes", {})
            db.execute(
                "UPDATE jobs SET app_id=?,snapshot=?,execution=?,phase='reserved',updated_at=? WHERE id=?",
                (app["id"], _json(snapshot), _json(state), time.time(), job_id),
            )
            return self._get(db, job_id)

    def _check_secret_scope(self, db, app, job, snapshot):
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
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                raise DeployError("JOB_STATE", "A cleaned or failing deployment cannot become active.")
            if not job["app_id"] or not job["execution"].get("ports"):
                raise DeployError("JOB_STATE", "An unreserved deployment cannot become active.")
            if job["status"] not in ("queued", "running", "succeeded"):
                raise DeployError("JOB_STATE", "A failed deployment cannot become active.")
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
