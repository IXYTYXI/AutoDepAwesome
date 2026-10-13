"""Short, transactional SQLite state changes for durable deployments."""

from contextlib import contextmanager
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
    def __init__(self, settings):
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
                    id TEXT PRIMARY KEY, repository TEXT NOT NULL, subdomain TEXT NOT NULL UNIQUE,
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
            """)

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
            return [dict(row) for row in db.execute("SELECT * FROM apps ORDER BY created_at,id")]

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
        due = []
        for job in jobs:
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                continue
            state = job["execution"].get("active_recovery", {})
            different_boot = state.get("boot_id") != boot_id
            if different_boot or state.get("next_at", 0) <= now:
                due.append((not different_boot, state.get("next_at", 0), job["created_at"], job["id"], job))
        return min(due, key=lambda item: item[:4])[-1] if due else None

    def _require_active(self, db, job_id):
        job = self._get(db, job_id)
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
            job = self._require_active(db, job_id)
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
        if app_id:
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if not row:
                raise DeployError("NOT_FOUND", "Application was not found.")
            app = dict(row)
            if app["repository"] != payload["repository"] or app["subdomain"] != payload["subdomain"]:
                raise DeployError(
                    "APP_IDENTITY_CONFLICT", "Repository and subdomain must match the application."
                )
            recovery = db.execute(
                "SELECT id FROM jobs WHERE app_id=? AND status='recovery_required' AND id<>?",
                (app_id, job["id"]),
            ).fetchone()
            if recovery:
                raise DeployError("RECOVERY_REQUIRED", "This application requires deployment recovery first.")
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
                db.execute(
                    "UPDATE jobs SET app_id=?,execution=?,updated_at=? WHERE id=?",
                    (app["id"], _json(state), time.time(), job_id),
                )
            return self._get(db, job_id)

    @staticmethod
    def _available(port):
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
            db.execute(
                "UPDATE jobs SET app_id=?,snapshot=?,execution=?,phase='reserved',updated_at=? WHERE id=?",
                (app["id"], _json(snapshot), _json(state), time.time(), job_id),
            )
            return self._get(db, job_id)

    def finish(self, job_id):
        with self._connection(write=True) as db:
            job = self._get(db, job_id)
            if job["execution"].get("resources_released") or job["execution"].get("failure_intent"):
                raise DeployError("JOB_STATE", "A cleaned or failing deployment cannot become active.")
            if not job["app_id"] or not job["execution"].get("ports"):
                raise DeployError("JOB_STATE", "An unreserved deployment cannot become active.")
            if job["status"] not in ("queued", "running", "succeeded"):
                raise DeployError("JOB_STATE", "A failed deployment cannot become active.")
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
