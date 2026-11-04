"""Durable authoritative allocations, reservations and writer admission.

All callers, including sessions, share the admission flock. SQLite commits a
reservation before any filesystem change. Pending operations never expire.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .errors import QuotaError

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
BUDGET_KEYS = tuple(f"{scope}_budget_{unit}" for scope in ("global", "app", "service")
                    for unit in ("mib", "inodes"))


def invalid(message="Invalid quota request"):
    raise QuotaError("QUOTA_INVALID_REQUEST", message)


def exact(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        invalid()


def positive(value, maximum=(1 << 43) - 1):
    if type(value) is not int or not 0 < value <= maximum:
        invalid("Limits must be positive bounded integers")


def reference(value):
    exact(value, ("backend", "storage_id", "generation"))
    if (value["backend"] != "xfs-project" or not isinstance(value["storage_id"], str)
            or not HEX64.fullmatch(value["storage_id"]) or not isinstance(value["generation"], str)
            or not HEX32.fullmatch(value["generation"])):
        invalid("Invalid stable storage reference")


def validate(request):
    if not isinstance(request, dict):
        invalid()
    op = request.get("op")
    shapes = {
        "check": (("op",), ()), "inventory": (("op",), ("app_id", "expected")),
        "ensure": (("op", "identity", "operation_id"), ("expected", "limits")),
        "inspect": (("op", "expected"), ()), "unblock": (("op",), ("expected", "pool")),
        "expand": (("op", "expected", "limits", "operation_id", "expected_revision"), ()),
        "delete": (("op", "expected", "operation_id"), ()),
        "retry": (("op", "expected", "operation_id"), ()),
        "guard": (("op", "items"), ()), "patrol": (("op",), ()),
        "retire": (("op", "container_id"), ()),
    }
    if not isinstance(op, str) or op not in shapes:
        invalid("Unsupported quota operation")
    exact(request, *shapes[op])
    if op == "unblock" and (("expected" in request) == ("pool" in request)
                            or ("pool" in request and request["pool"] is not True)):
        invalid("Unblock requires either an exact reference or pool:true")
    if "expected" in request:
        reference(request["expected"])
    if "identity" in request:
        exact(request["identity"], ("app_id", "service", "name"))
        for item in request["identity"].values():
            if not isinstance(item, str) or not NAME.fullmatch(item):
                invalid("Invalid storage identity")
    if "limits" in request:
        exact(request["limits"], ("size_mib", "max_inodes"))
        for item in request["limits"].values():
            positive(item)
    if "operation_id" in request:
        if not isinstance(request["operation_id"], str) or not OP_ID.fullmatch(request["operation_id"]):
            invalid("Invalid operation ID")
    if "expected_revision" in request:
        positive(request["expected_revision"])
    if "app_id" in request and (not isinstance(request["app_id"], str) or not NAME.fullmatch(request["app_id"])):
        invalid("Invalid application identity")
    if "container_id" in request:
        if not isinstance(request["container_id"], str) or not HEX64.fullmatch(request["container_id"]):
            invalid("An exact Docker container ID is required")
    if op == "guard":
        if not isinstance(request["items"], list) or not 1 <= len(request["items"]) <= 256:
            invalid("Guard requires a bounded nonempty item list")
        for item in request["items"]:
            exact(item, ("expected", "writer"))
            reference(item["expected"])
            exact(item["writer"], ("container_id", "container_name", "app_id", "release_id", "service"))
            for key, value in item["writer"].items():
                pattern = HEX64 if key == "container_id" else NAME
                if not isinstance(value, str) or not pattern.fullmatch(value):
                    invalid("Invalid writer identity")
    return op


def confirmed(error):
    return error.code.endswith("_MISMATCH") or error.code == "QUOTA_NOT_ENFORCED"


def _ref(row):
    return {"backend": "xfs-project", "storage_id": row["storage_id"], "generation": row["generation"]}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class QuotaService:
    def __init__(self, config, backend, state_dir, *, actor_uid=None):
        self.config = config
        self.actor_uid = os.getuid() if actor_uid is None else actor_uid
        self.backend = backend
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = self.state_dir / "ledger.sqlite3"
        for key in BUDGET_KEYS:
            positive(config.get(key))
        for key in ("reserve_free_mib", "reserve_free_inodes"):
            value = config.get(key, 0)
            if type(value) is not int or value < 0:
                invalid("Invalid free-space reserve")
        with self.admission():
            marker = self.state_dir / "initialized"
            fresh = not self.db_path.exists() and not marker.exists()
            if not self.db_path.exists():
                if marker.exists():
                    raise QuotaError("QUOTA_LEDGER_MISSING", "Established ledger is missing; manual recovery required")
                self.backend.check_pool()
                pool_root = config.get("pool_root")
                if pool_root and any(Path(pool_root).iterdir()):
                    raise QuotaError("QUOTA_LEDGER_MISSING", "Pool contains data but its ledger is missing")
            if not marker.exists():
                descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(descriptor, b"Established quota ledger; never remove to reset allocations.\n")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                descriptor = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            with self._connect(initialize=fresh) as db:
                if fresh:
                    self._initialize_schema(db)
            os.chmod(self.db_path, 0o600)

    def _initialize_schema(self, db):
        db.executescript("""
                    CREATE TABLE allocations (
                        storage_id TEXT PRIMARY KEY, generation TEXT NOT NULL, record TEXT NOT NULL);
                    CREATE TABLE projects (project_id INTEGER PRIMARY KEY AUTOINCREMENT);
                    CREATE TABLE operations (
                        operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                        storage_id TEXT NOT NULL, generation TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
                    CREATE TABLE writers (
                        container_id TEXT NOT NULL, storage_id TEXT NOT NULL, generation TEXT NOT NULL,
                        writer TEXT NOT NULL, PRIMARY KEY(container_id,storage_id,generation));
                    CREATE TABLE audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                        actor_uid INTEGER NOT NULL, operation TEXT NOT NULL, operation_id TEXT,
                        storage_id TEXT, generation TEXT, before_json TEXT, after_json TEXT,
                        result TEXT NOT NULL, error_code TEXT, request_json TEXT NOT NULL);
                    CREATE TABLE tombstones (
                        storage_id TEXT NOT NULL, generation TEXT NOT NULL, record TEXT NOT NULL,
                        PRIMARY KEY(storage_id,generation));
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    PRAGMA user_version=1;
                """)
        db.execute("INSERT INTO metadata VALUES ('pool_state',?)", (_json({"blocked": False, "generation": 0}),))
        db.commit()

    def _validate_schema(self, db):
        tables = {
            "allocations": {"storage_id", "generation", "record"}, "projects": {"project_id"},
            "operations": {"operation_id", "fingerprint", "storage_id", "generation", "state", "result"},
            "writers": {"container_id", "storage_id", "generation", "writer"},
            "audit": {"id", "created_at", "actor_uid", "operation", "operation_id", "storage_id", "generation",
                      "before_json", "after_json", "result", "error_code", "request_json"}, "metadata": {"key", "value"},
            "tombstones": {"storage_id", "generation", "record"},
        }
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise QuotaError("QUOTA_LEDGER_CORRUPT", "Ledger version is missing or unsupported")
        if [row[0] for row in db.execute("PRAGMA quick_check")] != ["ok"]:
            raise QuotaError("QUOTA_LEDGER_CORRUPT", "Ledger integrity check failed")
        for table, columns in tables.items():
            if {row[1] for row in db.execute(f"PRAGMA table_info({table})")} != columns:
                raise QuotaError("QUOTA_LEDGER_CORRUPT", "Established ledger schema is incomplete")
        self._pool_state(db)

    @contextmanager
    def _connect(self, *, initialize=False):
        db = None
        try:
            db = sqlite3.connect(self.db_path if initialize else self.db_path.as_uri() + "?mode=rw",
                                 timeout=30, uri=not initialize)
            db.row_factory = sqlite3.Row
            if not initialize:
                self._validate_schema(db)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_mode=DELETE")
            yield db
        except sqlite3.DatabaseError as exc:
            raise QuotaError("QUOTA_LEDGER_CORRUPT", "Authoritative ledger is unavailable; manual recovery required") from exc
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def admission(self):
        fd = os.open(self.state_dir / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def execute(self, request, *, locked=False):
        op = validate(request)
        if op in ("delete", "retry"):
            if locked:
                invalid("Deletion and retry cannot run inside an admission session")
            return self._delete_operation(request) if op == "delete" else self._retry_operation(request)
        if not locked:
            with self.admission():
                return self.execute(request, locked=True)
        with self._connect() as db:
            before = self._snapshot(db, request)
            try:
                result = getattr(self, "_" + op)(db, request)
            except QuotaError as exc:
                self._audit(db, request, before, self._snapshot(db, request), "error", exc.code)
                db.commit()
                raise
            if op in ("ensure", "expand", "delete", "guard", "retire", "unblock") or (
                    op == "patrol" and (result["blocked"] or result.get("pool_blocked"))):
                self._audit(db, request, before, self._snapshot(db, request), "success")
                db.commit()
            return result

    def _snapshot(self, db, request):
        if request["op"] in ("patrol", "check") or request.get("pool") is True:
            return self._pool_state(db)
        storage_id = request.get("expected", {}).get("storage_id")
        if not storage_id and "identity" in request:
            storage_id = hashlib.sha256(_json(request["identity"]).encode()).hexdigest()
        if storage_id:
            row = db.execute("SELECT record FROM allocations WHERE storage_id=?", (storage_id,)).fetchone()
            result = json.loads(row[0]) if row else None
            expected = request.get("expected")
            if expected and result and result["generation"] != expected["generation"]:
                row = db.execute("SELECT record FROM tombstones WHERE storage_id=? AND generation=?",
                                 (storage_id, expected["generation"])).fetchone()
                return json.loads(row[0]) if row else None
            return result
        return None

    def _audit(self, db, request, before, after, result, error_code=None):
        row = after or before or {}
        db.execute("""INSERT INTO audit
            (created_at,actor_uid,operation,operation_id,storage_id,generation,before_json,after_json,result,error_code,
             request_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), self.actor_uid, request["op"], request.get("operation_id"),
             row.get("storage_id"), row.get("generation"), _json(before), _json(after), result, error_code,
             _json(request)))

    def _records(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT record FROM allocations ORDER BY storage_id")]

    def _put(self, db, row):
        db.execute("INSERT OR REPLACE INTO allocations VALUES (?,?,?)",
                   (row["storage_id"], row["generation"], _json(row)))
        db.commit()

    def _lookup(self, db, expected, *, deleted=False):
        result = db.execute("SELECT record FROM allocations WHERE storage_id=?", (expected["storage_id"],)).fetchone()
        if result is None:
            raise QuotaError("QUOTA_REFERENCE_MISSING", "Storage reference is absent from the authoritative ledger")
        row = json.loads(result[0])
        if deleted and row["generation"] != expected["generation"]:
            tombstone = db.execute("SELECT record FROM tombstones WHERE storage_id=? AND generation=?",
                                   (expected["storage_id"], expected["generation"])).fetchone()
            if tombstone:
                return json.loads(tombstone[0])
        if row["generation"] != expected["generation"] or (row["state"] == "deleted" and not deleted):
            raise QuotaError("QUOTA_REFERENCE_MISMATCH", "Storage generation is unavailable or replaced")
        return row

    def _writers(self, db, row):
        return [json.loads(item[0]) for item in db.execute(
            "SELECT writer FROM writers WHERE storage_id=? AND generation=? ORDER BY container_id",
            (row["storage_id"], row["generation"]))]

    def _view(self, row, actual=None, request=None):
        result = {"reference": _ref(row), "identity": {key: row[key] for key in ("app_id", "service", "name")},
                  "path": row.get("directory", {}).get("path"), "size_mib": row["size_mib"],
                  "max_inodes": row["max_inodes"], "revision": row["revision"], "state": row["state"]}
        if actual:
            result.update({key: actual[key] for key in ("allocated_bytes", "used_inodes", "checked_at")})
            result["observed_limits"] = {key: actual[key] for key in ("size_mib", "max_inodes")}
        else:
            result.update({"allocated_bytes": None, "used_inodes": None, "checked_at": None,
                           "observed_limits": None})
        result["initial_limits"] = row.get("initial_limits")
        if request is not None:
            requested = request.get("limits")
            result["requested_limits"] = requested
            result["kept_current_limits"] = requested is None or any(
                row[key] > requested[key] for key in ("size_mib", "max_inodes"))
        if row.get("pending"):
            result["pending"] = row["pending"]
        if row.get("reason"):
            result["reason"] = row["reason"]
        return result

    def _pool_state(self, db):
        found = db.execute("SELECT value FROM metadata WHERE key='pool_state'").fetchone()
        try:
            value = json.loads(found[0])
            if type(value["blocked"]) is not bool or type(value["generation"]) is not int:
                raise ValueError
            return value
        except (TypeError, ValueError, KeyError):
            raise QuotaError("QUOTA_LEDGER_CORRUPT", "Pool admission state is missing or corrupt") from None

    def _require_pool_ready(self, db):
        if self._pool_state(db)["blocked"]:
            raise QuotaError("QUOTA_POOL_BLOCKED", "Pool is blocked; explicit pool revalidation and unblock are required")

    def _block_pool(self, db, error):
        state = self._pool_state(db)
        if not state["blocked"]:
            state["generation"] += 1
        state.update(blocked=True, reason=error.code, checked_at=datetime.now(timezone.utc).isoformat())
        db.execute("UPDATE metadata SET value=? WHERE key='pool_state'", (_json(state),))
        for row in self._records(db):
            if row["state"] != "deleted":
                if row["state"] != "blocked":
                    row["fencing_generation"] = row.get("fencing_generation", 0) + 1
                row.update(state="blocked", reason=error.code)
                db.execute("UPDATE allocations SET record=? WHERE storage_id=?", (_json(row), row["storage_id"]))
        db.commit()

    def _fence(self, db, row, error):
        if error.code in ("QUOTA_NOT_ENFORCED", "QUOTA_POOL_MISMATCH", "QUOTA_MAPPING_MISMATCH"):
            self._block_pool(db, error)
        if row["state"] != "blocked":
            row["fencing_generation"] = row.get("fencing_generation", 0) + 1
        row.update(state="blocked", reason=error.code)
        self._put(db, row)

    def _checked_pool(self, db):
        try:
            return self.backend.check_pool()
        except QuotaError as exc:
            if confirmed(exc):
                self._block_pool(db, exc)
            raise

    def _verified(self, db, row, *, require_ready=False):
        if require_ready and row["state"] == "blocked":
            raise QuotaError("QUOTA_BLOCKED", "Storage is blocked; explicit revalidation and unblock are required")
        if row["state"] not in ("ready", "blocked"):
            raise QuotaError("QUOTA_PENDING", "Storage has a pending operation requiring reconciliation")
        try:
            actual = self.backend.inspect(row)
            if actual["size_mib"] != row["size_mib"] or actual["max_inodes"] != row["max_inodes"]:
                raise QuotaError("QUOTA_LIMIT_MISMATCH", "Effective hard limits differ from the ledger")
        except QuotaError as exc:
            if confirmed(exc):
                self._fence(db, row, exc)
            raise
        return actual

    @contextmanager
    def _mutation(self, db, row):
        try:
            yield
        except QuotaError as exc:
            if confirmed(exc):
                self._fence(db, row, exc)
            raise

    def _pending_actual(self, db, row):
        """Pending expansion may have either dimension applied, but no other limits."""
        with self._mutation(db, row):
            actual = self.backend.inspect(row)
            pending = row.get("pending", {})
            for key in ("size_mib", "max_inodes"):
                allowed = {row[key], pending.get(key, row[key])}
                if pending.get("kind") == "ensure":
                    allowed.add(0)  # Not admitted: recovery must still finish both limits.
                if actual[key] not in allowed:
                    raise QuotaError("QUOTA_LIMIT_MISMATCH", "Pending operation has unexpected hard limits")
            return actual

    def _check(self, db, request):
        pool = self._checked_pool(db)
        blocked = self._pool_state(db)["blocked"]
        return {"backend": "xfs-project", "enabled": not blocked, **pool, "pool_blocked": blocked,
                "budgets": {key: self.config[key] for key in BUDGET_KEYS}}

    def _inventory(self, db, request):
        records = []
        totals = {"size_mib": 0, "max_inodes": 0}
        rows = ([self._lookup(db, request["expected"], deleted=True)] if "expected" in request
                else self._records(db))
        for row in rows:
            if ((row["state"] == "deleted" and "expected" not in request)
                    or (request.get("app_id") and row["app_id"] != request["app_id"])):
                continue
            actual = None
            query_error = None
            if row.get("directory") and row["state"] not in ("deleting", "deleted"):
                try:
                    actual = self._pending_actual(db, row) if row.get("pending") else self._verified(db, row)
                except QuotaError as exc:
                    query_error = exc.code
            result = self._view(row, actual)
            if query_error:
                result["query_error"] = query_error
            result["writers"] = self._writers(db, row)
            records.append(result)
            if row["state"] != "deleted":
                for key in totals:
                    totals[key] += row.get("pending", {}).get(key, row[key])
        return {"records": records, "totals": totals, "pool_state": self._pool_state(db)}

    def _operation(self, db, request):
        fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
        op = db.execute("SELECT * FROM operations WHERE operation_id=?", (request["operation_id"],)).fetchone()
        if op and op["fingerprint"] != fingerprint:
            raise QuotaError("QUOTA_OPERATION_CONFLICT", "Operation ID was already used for a different request")
        return op, fingerprint

    def _begin(self, db, request, row, fingerprint):
        before = self._snapshot(db, request)
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        row.setdefault("created_at", row["updated_at"])
        self._audit(db, request, before, row, "intent")
        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,NULL)",
                   (request["operation_id"], fingerprint, row["storage_id"], row["generation"], "pending"))
        db.execute("INSERT OR REPLACE INTO allocations VALUES (?,?,?)",
                   (row["storage_id"], row["generation"], _json(row)))
        db.commit()

    def _complete(self, db, request, row, result):
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        if row["state"] == "deleted":
            db.execute("INSERT OR REPLACE INTO tombstones VALUES (?,?,?)",
                       (row["storage_id"], row["generation"], _json(row)))
        db.execute("UPDATE operations SET state='done',result=? WHERE operation_id=?",
                   (_json(result), request["operation_id"]))
        db.execute("INSERT OR REPLACE INTO allocations VALUES (?,?,?)",
                   (row["storage_id"], row["generation"], _json(row)))
        db.commit()
        return result

    def _admit(self, db, candidate):
        rows = [row for row in self._records(db)
                if row["state"] != "deleted" and row["storage_id"] != candidate["storage_id"]] + [candidate]
        for scope in ("global", "app", "service"):
            selected = [row for row in rows if scope == "global" or (
                row["app_id"] == candidate["app_id"] and (scope == "app" or row["service"] == candidate["service"]))]
            for unit, key in (("mib", "size_mib"), ("inodes", "max_inodes")):
                total = sum(row.get("pending", {}).get(key, row[key]) for row in selected)
                if total > self.config[f"{scope}_budget_{unit}"]:
                    raise QuotaError("QUOTA_BUDGET_EXCEEDED", f"{scope} {unit} budget exceeded")
        pool = self._checked_pool(db)
        allocated_bytes = used_inodes = 0
        for row in rows:
            if row.get("directory"):
                actual = self.backend.inspect(row)
                allocated_bytes += actual["allocated_bytes"]
                used_inodes += actual["used_inodes"]
        committed_bytes = sum(row.get("pending", {}).get("size_mib", row["size_mib"]) * 1048576 for row in rows)
        committed_inodes = sum(row.get("pending", {}).get("max_inodes", row["max_inodes"]) for row in rows)
        if (pool["free_bytes"] + allocated_bytes < committed_bytes + self.config.get("reserve_free_mib", 0) * 1048576
                or pool["free_inodes"] + used_inodes < committed_inodes + self.config.get("reserve_free_inodes", 0)):
            raise QuotaError("QUOTA_POOL_RESERVE", "Pool reserve is insufficient for the committed hard limits")

    def _ensure(self, db, request):
        self._require_pool_ready(db)
        op, fingerprint = self._operation(db, request)
        identity = request["identity"]
        storage_id = hashlib.sha256(_json(identity).encode()).hexdigest()
        found = db.execute("SELECT record FROM allocations WHERE storage_id=?", (storage_id,)).fetchone()
        row = json.loads(found[0]) if found else None
        if "expected" in request:
            row = self._lookup(db, request["expected"])
            if row["storage_id"] != storage_id:
                raise QuotaError("QUOTA_REFERENCE_MISMATCH", "Expected reference has a different identity")
        if op:
            row = self._lookup(db, {"storage_id": op["storage_id"], "generation": op["generation"]})
            if op["state"] == "done":
                return self._view(row, self._verified(db, row, require_ready=True), request)
            if row.get("pending", {}).get("operation_id") != request["operation_id"]:
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "A different operation is pending")
        elif row and row["state"] != "deleted":
            actual = self._verified(db, row, require_ready=True)
            if any(request.get("limits", {}).get(key, row[key]) > row[key] for key in ("size_mib", "max_inodes")):
                raise QuotaError("QUOTA_EXPANSION_REQUIRED", "Larger declarations require explicit expansion")
            self._begin(db, request, row, fingerprint)
            return self._complete(db, request, row, self._view(row, actual, request))
        else:
            if "limits" not in request:
                invalid("New storage requires explicit byte and inode limits")
            row = {**identity, **request["limits"], "storage_id": storage_id, "generation": uuid.uuid4().hex,
                   "filesystem_uuid": self.config.get("pool_uuid"), "initial_limits": dict(request["limits"]),
                   "revision": 1, "state": "allocating", "pending": {**request["limits"],
                   "operation_id": request["operation_id"], "kind": "ensure"}}
            self._admit(db, row)
            cursor = db.execute("INSERT INTO projects DEFAULT VALUES")
            row["project_id"] = cursor.lastrowid
            if row["project_id"] > 0xFFFFFFFF:
                db.rollback()
                raise QuotaError("QUOTA_PROJECT_EXHAUSTED", "Project ID space exhausted")
            self._begin(db, request, row, fingerprint)
        if row["state"] == "blocked":
            raise QuotaError("QUOTA_BLOCKED", "Storage requires explicit revalidation and unblock")
        with self._mutation(db, row):
            if not row.get("directory"):
                row["directory"] = self.backend.create(row)
                self._put(db, row)
            self.backend.set_limits(row, row["size_mib"], row["max_inodes"])
            actual = self.backend.inspect(row)
            if actual["size_mib"] != row["size_mib"] or actual["max_inodes"] != row["max_inodes"]:
                raise QuotaError("QUOTA_LIMIT_MISMATCH", "New limits were not confirmed")
        row.update(state="ready")
        row.pop("pending", None)
        return self._complete(db, request, row, self._view(row, actual, request))

    def _inspect(self, db, request):
        row = self._lookup(db, request["expected"])
        return self._view(row, self._verified(db, row))

    def _expand(self, db, request):
        self._require_pool_ready(db)
        op, fingerprint = self._operation(db, request)
        row = self._lookup(db, request["expected"])
        if op and op["state"] == "done":
            return self._view(row, self._verified(db, row, require_ready=True), request)
        if not op:
            self._verified(db, row, require_ready=True)
            if row["revision"] != request["expected_revision"]:
                raise QuotaError("QUOTA_REVISION_CONFLICT", "Storage revision has changed")
            if any(request["limits"][key] < row[key] for key in ("size_mib", "max_inodes")):
                raise QuotaError("QUOTA_SHRINK_FORBIDDEN", "Hard limits cannot shrink")
            row["pending"] = {**request["limits"], "operation_id": request["operation_id"], "kind": "expand"}
            row["state"] = "expanding"
            self._admit(db, row)
            self._begin(db, request, row, fingerprint)
        elif row.get("pending", {}).get("operation_id") != request["operation_id"]:
            raise QuotaError("QUOTA_OPERATION_CONFLICT", "A different operation is pending")
        if row["state"] == "blocked":
            raise QuotaError("QUOTA_BLOCKED", "Storage requires explicit revalidation and unblock")
        with self._mutation(db, row):
            self.backend.set_limits(row, request["limits"]["size_mib"], request["limits"]["max_inodes"])
            actual = self.backend.inspect(row)
            if any(actual[key] != request["limits"][key] for key in ("size_mib", "max_inodes")):
                raise QuotaError("QUOTA_LIMIT_MISMATCH", "Expanded limits were not confirmed")
        row.update(request["limits"])
        row.update(state="ready", revision=row["revision"] + 1)
        row.pop("pending", None)
        return self._complete(db, request, row, self._view(row, actual, request))

    @contextmanager
    def _storage_operation(self, storage_id):
        # Lock names derive exclusively from validated ledger IDs. Never hold the
        # global admission lock while waiting for another deletion of this data.
        path = self.state_dir / ("storage-" + storage_id + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o022 or info.st_nlink != 1):
                raise QuotaError("QUOTA_CONFIG_UNTRUSTED", "Storage operation lock ownership is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _delete_prepare(self, db, request):
        op, fingerprint = self._operation(db, request)
        if op and op["state"] == "done":
            return None, json.loads(op["result"])
        row = self._lookup(db, request["expected"], deleted=True)
        if self._writers(db, row):
            raise QuotaError("QUOTA_WRITERS_ACTIVE", "Registered writers must be confirmed stopped and retired")
        if row["state"] == "deleted":
            result = {"deleted": True, "reference": _ref(row)}
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)",
                       (request["operation_id"], fingerprint, row["storage_id"], row["generation"], "done", _json(result)))
            db.commit()
            return None, result
        if not op:
            if row.get("pending", {}).get("kind") == "delete":
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "A different deletion is pending")
            reserved = {key: row.get("pending", {}).get(key, row[key]) for key in ("size_mib", "max_inodes")}
            row["pending"] = {**reserved, "kind": "delete", "operation_id": request["operation_id"]}
            row["state"] = "deleting"
            self._begin(db, request, row, fingerprint)
        elif row.get("pending", {}).get("operation_id") != request["operation_id"]:
            raise QuotaError("QUOTA_OPERATION_CONFLICT", "A different deletion is pending")
        return row, None

    def _delete_current(self, db, request, prepared):
        current = self._lookup(db, request["expected"])
        pending = current.get("pending", {})
        if (current["revision"] != prepared["revision"] or current.get("directory") != prepared.get("directory")
                or pending.get("kind") != "delete" or pending.get("operation_id") != request["operation_id"]
                or current["state"] not in ("deleting", "blocked")):
            raise QuotaError("QUOTA_OPERATION_CONFLICT", "Deletion identity or operation changed before confirmation")
        if self._writers(db, current):
            raise QuotaError("QUOTA_WRITERS_ACTIVE", "A writer appeared before deletion confirmation")
        return current

    def _delete_operation(self, request):
        before = None
        with self._storage_operation(request["expected"]["storage_id"]):
            try:
                with self.admission(), self._connect() as db:
                    before = self._snapshot(db, request)
                    prepared, completed = self._delete_prepare(db, request)
                    if completed is not None:
                        self._audit(db, request, before, self._snapshot(db, request), "success")
                        db.commit()
                        return completed
                # A deleting/blocked allocation cannot admit a new writer. Its
                # full reservation remains committed while recursive IO runs.
                if not prepared.get("directory"):
                    directory = self.backend.create(prepared)
                    with self.admission(), self._connect() as db:
                        current = self._delete_current(db, request, prepared)
                        current["directory"] = directory
                        self._put(db, current)
                        prepared = current
                if not self.backend.delete(prepared):
                    raise QuotaError("QUOTA_DELETE_PENDING", "Project data or quota usage remain; reservation retained")
                with self.admission(), self._connect() as db:
                    current = self._delete_current(db, request, prepared)
                    self._checked_pool(db)
                    current["state"] = "deleted"
                    current.pop("reason", None)
                    current.pop("pending", None)
                    result = self._complete(db, request, current, {"deleted": True, "reference": _ref(current)})
                    self._audit(db, request, before, current, "success")
                    db.commit()
                    return result
            except QuotaError as exc:
                with self.admission(), self._connect() as db:
                    current = self._snapshot(db, request)
                    if current and current["state"] != "deleted" and confirmed(exc):
                        self._fence(db, current, exc)
                    self._audit(db, request, before, self._snapshot(db, request), "error", exc.code)
                    db.commit()
                raise

    def _retry_operation(self, request):
        with self.admission(), self._connect() as db:
            row = self._lookup(db, request["expected"], deleted=True)
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?",
                                   (request["operation_id"],)).fetchone()
            if not operation or (operation["storage_id"], operation["generation"]) != (
                    row["storage_id"], row["generation"]):
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "Original operation does not match this exact generation")
            saved = db.execute("""SELECT request_json FROM audit WHERE operation_id=? AND result='intent'
                AND operation IN ('ensure','expand','delete') ORDER BY id LIMIT 1""", (request["operation_id"],)).fetchone()
            if not saved:
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "Original operation request is unavailable")
            original = json.loads(saved[0])
            validate(original)
            if original["op"] not in ("ensure", "expand", "delete"):
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "Original operation is not recoverable")
            self._operation(db, original)  # Verify durable request fingerprint before replay.
            if operation["state"] != "done" and (
                    row.get("pending", {}).get("operation_id") != request["operation_id"]
                    or row.get("pending", {}).get("kind") != original["op"]):
                raise QuotaError("QUOTA_OPERATION_CONFLICT", "Original operation is no longer the pending operation")
            self._audit(db, request, row, row, "intent")
            db.commit()
        # Original handlers retain all pool/record gates. Delete acquires its
        # own operation lock without keeping global writer admission blocked.
        return self.execute(original)

    def _guard(self, db, request):
        self._require_pool_ready(db)
        checked = []
        writers_seen = {}
        for item in request["items"]:
            row = self._lookup(db, item["expected"])
            writer = item["writer"]
            if writer["container_id"] in writers_seen and writers_seen[writer["container_id"]] != writer:
                raise QuotaError("QUOTA_WRITER_MISMATCH", "Guard contains conflicting writer identities")
            writers_seen[writer["container_id"]] = writer
            if writer["app_id"] != row["app_id"] or writer["service"] != row["service"]:
                raise QuotaError("QUOTA_WRITER_MISMATCH", "Writer does not own this allocation")
            actual = self._verified(db, row, require_ready=True)
            existing = db.execute("SELECT writer FROM writers WHERE container_id=?", (writer["container_id"],)).fetchall()
            if any(json.loads(result[0]) != writer for result in existing):
                raise QuotaError("QUOTA_WRITER_MISMATCH", "Container is registered with different identity")
            checked.append((row, writer, actual))
        for row, writer, _ in checked:
            db.execute("INSERT OR REPLACE INTO writers VALUES (?,?,?,?)",
                       (writer["container_id"], row["storage_id"], row["generation"], _json(writer)))
        db.commit()
        return {"writers": [writer for _, writer, _ in checked],
                "records": [self._view(row, actual) for row, _, actual in checked]}

    def _patrol(self, db, request):
        blocked, unknown = [], []
        pool = None
        pool_error = None
        try:
            pool = self._checked_pool(db)
        except QuotaError as exc:
            pool_error = exc
        for row in self._records(db):
            if row["state"] == "deleted":
                continue
            if pool_error and confirmed(pool_error):
                row.update(state="blocked", reason=pool_error.code)
                self._put(db, row)
            if row["state"] == "blocked":
                blocked.append({"reference": _ref(row), "writers": self._writers(db, row),
                                "reason": row.get("reason", "QUOTA_BLOCKED")})
                continue
            if pool_error:
                unknown.append({"reference": _ref(row), "reason": pool_error.code})
                continue
            if not row.get("directory") or row["state"] == "deleting":
                unknown.append({"reference": _ref(row), "reason": "QUOTA_PENDING"})
                continue
            try:
                if row.get("pending"):
                    self._pending_actual(db, row)
                    unknown.append({"reference": _ref(row), "reason": "QUOTA_PENDING"})
                else:
                    self._verified(db, row)
            except QuotaError as exc:
                if confirmed(exc):
                    blocked.append({"reference": _ref(row), "writers": self._writers(db, row), "reason": exc.code})
                else:
                    unknown.append({"reference": _ref(row), "reason": exc.code})
        result = {"blocked": blocked, "unknown": unknown, "pool": pool,
                  "budgets": {key: self.config[key] for key in BUDGET_KEYS},
                  "pool_blocked": self._pool_state(db)["blocked"],
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        if pool_error:
            result["pool_error"] = pool_error.code
        return result

    def _retire(self, db, request):
        db.execute("DELETE FROM writers WHERE container_id=?", (request["container_id"],))
        db.commit()
        return {"retired": request["container_id"]}

    def _unblock(self, db, request):
        if request.get("pool"):
            pool = self._checked_pool(db)
            before = self._pool_state(db)
            state = {**before, "blocked": False, "checked_at": datetime.now(timezone.utc).isoformat()}
            state.pop("reason", None)
            db.execute("UPDATE metadata SET value=? WHERE key='pool_state'", (_json(state),))
            self._audit(db, request, before, state, "intent")
            db.commit()
            return {"pool_blocked": False, "pool": pool}
        self._require_pool_ready(db)
        self._checked_pool(db)
        row = self._lookup(db, request["expected"])
        if not row.get("directory") and row.get("pending", {}).get("kind") == "ensure":
            with self._mutation(db, row):
                row["directory"] = self.backend.create(row)
                self._put(db, row)
        actual = self._pending_actual(db, row) if row.get("pending") else self._verified(db, row)
        kind = row.get("pending", {}).get("kind")
        row.update(state={"ensure": "allocating", "expand": "expanding", "delete": "deleting"}.get(kind, "ready"))
        row.pop("reason", None)
        self._put(db, row)
        return self._view(row, actual)
