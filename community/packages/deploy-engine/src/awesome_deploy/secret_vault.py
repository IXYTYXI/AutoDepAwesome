"""Application-scoped authenticated encryption within the caller's transaction.

No method opens a database connection or commits the caller's work. Mutation
savepoints make a rejected operation atomic even if its caller catches the error.
Lifecycle authorization and worker locking belong to the service boundary.
"""

from contextlib import contextmanager
import hmac
import json
import os
import re
import sqlite3
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import DeployError
from .secrets_store import SecretStore
from .vault_keys import VaultKeyring, KEY_ID

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_VERSION = re.compile(r"[0-9a-f]{32}\Z")
_KINDS = {"runtime_secret", "platform_managed_master"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unavailable():
    return DeployError("SECRET_UNAVAILABLE", "密钥库记录不可用或完整性校验失败，请恢复原记录。")


class SecretVault:
    def __init__(self, settings):
        self.keyring = VaultKeyring(settings)

    @staticmethod
    def ensure_schema(db):
        # executescript() implicitly commits pending transactions; do not use it.
        statements = (
            """CREATE TABLE IF NOT EXISTS vault_apps (
                app_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, data_generation TEXT NOT NULL,
                dek_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','destroyed')),
                created_by TEXT NOT NULL, created_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS vault_app_keys (
                dek_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                format INTEGER NOT NULL, kek_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS vault_secrets (
                app_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                data_generation TEXT NOT NULL, current_version TEXT NOT NULL,
                PRIMARY KEY(app_id,name))""",
            """CREATE TABLE IF NOT EXISTS vault_secret_versions (
                app_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
                format INTEGER NOT NULL, kind TEXT NOT NULL, data_generation TEXT NOT NULL,
                dek_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                created_by TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(app_id,name,version))""",
            """CREATE TABLE IF NOT EXISTS vault_nonces (
                purpose TEXT NOT NULL, key_id TEXT NOT NULL, nonce BLOB NOT NULL,
                PRIMARY KEY(purpose,key_id,nonce))""",
            """CREATE TABLE IF NOT EXISTS vault_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                app_id TEXT, name TEXT, version TEXT, detail TEXT NOT NULL, created_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS vault_tombstones (
                app_id TEXT NOT NULL, data_generation TEXT NOT NULL, plan TEXT NOT NULL,
                actor_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(app_id,data_generation))""",
        )
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise _unavailable()
        return value

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise DeployError("INVALID_SECRET_NAME", "秘密配置名称格式不正确。")
        return value

    @staticmethod
    def _version(value):
        if not isinstance(value, str) or not _VERSION.fullmatch(value):
            raise _unavailable()
        return value

    @contextmanager
    def _mutation(self, db, actor_id):
        self._identity(actor_id)
        if not db.in_transaction:
            raise DeployError("VAULT_TRANSACTION_REQUIRED", "密钥库变更必须与授权及审计处于同一事务。")
        name = "vault_" + uuid.uuid4().hex
        db.execute("SAVEPOINT " + name)
        try:
            yield
        except BaseException as error:
            db.execute("ROLLBACK TO " + name)
            db.execute("RELEASE " + name)
            if isinstance(error, (sqlite3.Error, ValueError, TypeError, KeyError, InvalidTag)):
                raise _unavailable() from None
            raise
        else:
            db.execute("RELEASE " + name)

    @staticmethod
    def _audit(db, actor_id, action, app_id=None, name=None, version=None, detail=None):
        db.execute("INSERT INTO vault_audit(actor_id,action,app_id,name,version,detail,created_at) VALUES (?,?,?,?,?,?,?)",
                   (actor_id, action, app_id, name, version, _json(detail or {}), time.time()))

    @staticmethod
    def _nonce(db, purpose, key_id):
        for _ in range(8):
            nonce = os.urandom(12)
            try:
                db.execute("INSERT INTO vault_nonces VALUES (?,?,?)", (purpose, key_id, nonce))
                return nonce
            except sqlite3.IntegrityError:
                continue
        raise _unavailable()

    @staticmethod
    def _wrap_aad(app, dek_id):
        return _json({"format": 1, "purpose": "app-dek", "tenant": app["tenant_id"],
                      "app": app["app_id"], "dek_id": dek_id}).encode()

    @staticmethod
    def _value_aad(app, row):
        return _json({"format": 1, "purpose": "runtime-secret", "tenant": app["tenant_id"],
                      "app": app["app_id"], "name": row["name"], "version": row["version"],
                      "kind": row["kind"], "data_generation": row["data_generation"],
                      "dek_id": row["dek_id"]}).encode()

    def _app(self, db, app_id, *, allow_destroyed=False):
        self._identity(app_id)
        row = db.execute("SELECT * FROM vault_apps WHERE app_id=?", (app_id,)).fetchone()
        if row is None:
            raise DeployError("VAULT_APP_UNREGISTERED", "应用尚未完成密钥库登记。")
        for field in ("app_id", "tenant_id", "data_generation"):
            self._identity(row[field])
        self._version(row["dek_id"])
        if row["state"] not in {"active", "destroyed"} or row["state"] == "destroyed" and not allow_destroyed:
            raise _unavailable()
        return row

    def _dek(self, db, app):
        row = db.execute("SELECT * FROM vault_app_keys WHERE dek_id=?", (app["dek_id"],)).fetchone()
        if (row is None or row["app_id"] != app["app_id"] or row["tenant_id"] != app["tenant_id"]
                or row["format"] != 1 or not KEY_ID.fullmatch(str(row["kek_id"]))
                or not isinstance(row["nonce"], bytes) or len(row["nonce"]) != 12
                or not isinstance(row["ciphertext"], bytes) or len(row["ciphertext"]) != 48):
            raise _unavailable()
        try:
            return AESGCM(self.keyring.key(row["kek_id"])).decrypt(
                row["nonce"], row["ciphertext"], self._wrap_aad(app, app["dek_id"]))
        except (InvalidTag, ValueError, TypeError):
            raise _unavailable() from None

    def register_app(self, db, app_id, tenant_id, data_generation, *, actor_id):
        for identity in (app_id, tenant_id, data_generation):
            self._identity(identity)
        with self._mutation(db, actor_id):
            existing = db.execute("SELECT * FROM vault_apps WHERE app_id=?", (app_id,)).fetchone()
            if existing is None and any(db.execute(f"SELECT 1 FROM {table} WHERE app_id=?", (app_id,)).fetchone()
                                        for table in ("vault_app_keys", "vault_secrets", "vault_secret_versions", "vault_tombstones")):
                raise _unavailable()
            if existing is not None:
                old = self._app(db, app_id, allow_destroyed=True)
                if old["tenant_id"] != tenant_id:
                    raise _unavailable()
                if old["state"] == "active":
                    if old["data_generation"] != data_generation:
                        raise _unavailable()
                    self._dek(db, old)
                    return
                if old["data_generation"] == data_generation:
                    raise _unavailable()
                if (not db.execute("SELECT 1 FROM vault_tombstones WHERE app_id=? AND data_generation=?",
                                   (app_id, old["data_generation"])).fetchone()
                        or any(db.execute(f"SELECT 1 FROM {table} WHERE app_id=?", (app_id,)).fetchone()
                               for table in ("vault_app_keys", "vault_secrets", "vault_secret_versions"))):
                    raise _unavailable()
            if db.execute("SELECT 1 FROM vault_tombstones WHERE app_id=? AND data_generation=?",
                          (app_id, data_generation)).fetchone():
                raise _unavailable()
            dek_id, dek = uuid.uuid4().hex, os.urandom(32)
            app = {"app_id": app_id, "tenant_id": tenant_id, "data_generation": data_generation}
            nonce = self._nonce(db, "kek", self.keyring.active)
            encrypted = AESGCM(self.keyring.key(self.keyring.active)).encrypt(nonce, dek, self._wrap_aad(app, dek_id))
            db.execute("INSERT INTO vault_app_keys VALUES (?,?,?,?,?,?,?)",
                       (dek_id, app_id, tenant_id, 1, self.keyring.active, nonce, encrypted))
            db.execute("INSERT OR REPLACE INTO vault_apps VALUES (?,?,?,?,?,?,?)",
                       (app_id, tenant_id, data_generation, dek_id, "active", actor_id, time.time()))
            self._audit(db, actor_id, "app_registered", app_id, detail={"data_generation": data_generation})

    def _record(self, db, app, name):
        self._name(name)
        row = db.execute("SELECT * FROM vault_secrets WHERE app_id=? AND name=?", (app["app_id"], name)).fetchone()
        if row is None:
            if db.execute("SELECT 1 FROM vault_secret_versions WHERE app_id=? AND name=?",
                          (app["app_id"], name)).fetchone():
                raise _unavailable()
            raise DeployError("SECRET_NOT_FOUND", f"请先通过安全入口配置 {name}。", "secretRefs")
        if row["kind"] not in _KINDS or row["data_generation"] != app["data_generation"]:
            raise _unavailable()
        self._version(row["current_version"])
        return row

    def _read(self, db, app, record, version, *, allow_revoked=False):
        self._version(version)
        row = db.execute("SELECT * FROM vault_secret_versions WHERE app_id=? AND name=? AND version=?",
                         (app["app_id"], record["name"], version)).fetchone()
        if (row is None or row["format"] != 1 or row["kind"] != record["kind"]
                or row["data_generation"] != record["data_generation"] or row["dek_id"] != app["dek_id"]
                or row["status"] not in {"active", "revoked"}
                or not isinstance(row["nonce"], bytes) or len(row["nonce"]) != 12
                or not isinstance(row["ciphertext"], bytes) or not 17 <= len(row["ciphertext"]) <= 16400):
            raise _unavailable()
        try:
            value = AESGCM(self._dek(db, app)).decrypt(row["nonce"], row["ciphertext"], self._value_aad(app, row)).decode("utf-8")
            SecretStore.validate_value(value)
        except (InvalidTag, ValueError, TypeError, UnicodeError, DeployError):
            raise _unavailable() from None
        if row["status"] == "revoked" and not allow_revoked:
            raise DeployError("SECRET_REVOKED", "该固定密钥版本已吊销，不能启动、发布或恢复。")
        return value

    def _insert_version(self, db, app, name, value, kind, version, actor_id, *, status="active"):
        dek = self._dek(db, app)
        row = {"name": name, "version": version, "kind": kind,
               "data_generation": app["data_generation"], "dek_id": app["dek_id"]}
        nonce = self._nonce(db, "dek", app["dek_id"])
        ciphertext = AESGCM(dek).encrypt(nonce, value.encode("utf-8"), self._value_aad(app, row))
        db.execute("INSERT INTO vault_secret_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (app["app_id"], name, version, 1, kind, app["data_generation"], app["dek_id"],
                    nonce, ciphertext, status, actor_id, time.time()))

    def create_missing(self, db, app_id, name, value, *, kind="runtime_secret", actor_id, version=None):
        self._name(name)
        SecretStore.validate_value(value)
        if kind not in _KINDS:
            raise _unavailable()
        version = self._version(version) if version is not None else uuid.uuid4().hex
        with self._mutation(db, actor_id):
            app = self._app(db, app_id)
            try:
                record = self._record(db, app, name)
            except DeployError as error:
                if error.code != "SECRET_NOT_FOUND":
                    raise
            else:
                self._read(db, app, record, record["current_version"], allow_revoked=True)
                raise DeployError("SECRET_ALREADY_CONFIGURED", "秘密配置已经存在，不能通过此入口覆盖。")
            self._insert_version(db, app, name, value, kind, version, actor_id)
            db.execute("INSERT INTO vault_secrets VALUES (?,?,?,?,?)",
                       (app_id, name, kind, app["data_generation"], version))
            self._audit(db, actor_id, "secret_created", app_id, name, version, {"kind": kind})
            return version

    def metadata(self, db, app_id):
        app = self._app(db, app_id)
        self._dek(db, app)
        names = {row[0] for row in db.execute("SELECT name FROM vault_secrets WHERE app_id=?", (app_id,))}
        version_names = {row[0] for row in db.execute("SELECT DISTINCT name FROM vault_secret_versions WHERE app_id=?", (app_id,))}
        if names != version_names:
            raise _unavailable()
        result = []
        for name in sorted(names):
            record = self._record(db, app, name)
            versions = []
            for row in db.execute("SELECT version,status FROM vault_secret_versions WHERE app_id=? AND name=? ORDER BY created_at,version",
                                  (app_id, name)):
                self._read(db, app, record, row["version"], allow_revoked=True)
                versions.append({"version": row["version"], "status": row["status"]})
            if record["current_version"] not in {row["version"] for row in versions}:
                raise _unavailable()
            result.append({"name": name, "kind": record["kind"], "current_version": record["current_version"],
                           "data_generation": record["data_generation"], "versions": versions})
        return result

    def import_version(self, db, app_id, name, value, *, kind, version, actor_id,
                       make_current=False, status="active"):
        """Maintenance-only import; caller must hold the migration admission gate.

        Existing logical IDs are never overwritten, even during interrupted import.
        Equality is checked in memory without persisting a digest of secret bytes.
        """
        self._name(name)
        self._version(version)
        SecretStore.validate_value(value)
        if kind not in _KINDS or status not in {"active", "revoked"} or type(make_current) is not bool:
            raise _unavailable()
        with self._mutation(db, actor_id):
            app = self._app(db, app_id)
            try:
                record = self._record(db, app, name)
            except DeployError as error:
                if error.code != "SECRET_NOT_FOUND":
                    raise
                record = None
            if record is not None:
                if record["kind"] != kind:
                    raise _unavailable()
                existing = db.execute("SELECT status FROM vault_secret_versions WHERE app_id=? AND name=? AND version=?",
                                      (app_id, name, version)).fetchone()
                if existing:
                    original = self._read(db, app, record, version, allow_revoked=True)
                    if existing["status"] != status or not hmac.compare_digest(original.encode(), value.encode()):
                        raise _unavailable()
                    if make_current and record["current_version"] != version:
                        db.execute("UPDATE vault_secrets SET current_version=? WHERE app_id=? AND name=?",
                                   (version, app_id, name))
                        self._audit(db, actor_id, "import_current_selected", app_id, name, version)
                    return version
            self._insert_version(db, app, name, value, kind, version, actor_id, status=status)
            if record is None:
                db.execute("INSERT INTO vault_secrets VALUES (?,?,?,?,?)",
                           (app_id, name, kind, app["data_generation"], version))
            elif make_current:
                db.execute("UPDATE vault_secrets SET current_version=? WHERE app_id=? AND name=?",
                           (version, app_id, name))
            self._audit(db, actor_id, "secret_version_imported", app_id, name, version,
                        {"kind": kind, "status": status, "current": record is None or make_current})
            return version

    def validate_versions(self, db, app_id, versions):
        if not isinstance(versions, dict) or len(versions) > 2048:
            raise _unavailable()
        app = self._app(db, app_id)
        self._dek(db, app)
        for name, version in versions.items():
            self._read(db, app, self._record(db, app, name), version)

    def versions(self, db, app_id, refs):
        if not isinstance(refs, dict) or len(refs) > 2048:
            raise _unavailable()
        app = self._app(db, app_id)
        result = {}
        for name in refs.values():
            record = self._record(db, app, name)
            self._read(db, app, record, record["current_version"])
            result[name] = record["current_version"]
        return result

    def resolve(self, db, app_id, refs, versions):
        if (not isinstance(refs, dict) or not isinstance(versions, dict) or len(refs) > 2048
                or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in refs)):
            raise _unavailable()
        app = self._app(db, app_id)
        result = {}
        for variable, name in refs.items():
            result[variable] = self._read(db, app, self._record(db, app, name), versions.get(name))
        return result

    @staticmethod
    def _ordinary(record, expected_current_version):
        if record["kind"] == "platform_managed_master":
            raise DeployError("SECRET_MASTER_PROTECTED", "应用主密钥受保护，只能通过数据重加密专项迁移处理。")
        if record["current_version"] != expected_current_version:
            raise DeployError("SECRET_VERSION_CONFLICT", "当前密钥版本已经变化，请刷新后重试。")

    def rotate(self, db, app_id, name, value, expected_current_version, *, actor_id):
        SecretStore.validate_value(value)
        with self._mutation(db, actor_id):
            app = self._app(db, app_id)
            record = self._record(db, app, name)
            self._ordinary(record, expected_current_version)
            self._read(db, app, record, record["current_version"], allow_revoked=True)
            version = uuid.uuid4().hex
            self._insert_version(db, app, name, value, record["kind"], version, actor_id)
            db.execute("UPDATE vault_secrets SET current_version=? WHERE app_id=? AND name=?",
                       (version, app_id, name))
            self._audit(db, actor_id, "secret_rotated", app_id, name, version,
                        {"previous_version": expected_current_version})
            return version

    def revoke(self, db, app_id, name, version, expected_current_version, *, actor_id):
        with self._mutation(db, actor_id):
            app = self._app(db, app_id)
            record = self._record(db, app, name)
            self._ordinary(record, expected_current_version)
            self._read(db, app, record, version, allow_revoked=True)
            changed = db.execute("UPDATE vault_secret_versions SET status='revoked' WHERE app_id=? AND name=? AND version=? AND status='active'",
                                 (app_id, name, version)).rowcount
            if changed:
                self._audit(db, actor_id, "secret_revoked", app_id, name, version)

    def inventory(self, db, app_id):
        app = self._app(db, app_id)
        return {"format": 1, "app_id": app_id, "tenant_id": app["tenant_id"],
                "data_generation": app["data_generation"], "dek_id": app["dek_id"],
                "secrets": self.metadata(db, app_id)}

    def verify_deletion(self, db, plan):
        """Authenticate the full immutable scope before external data deletion."""
        conflict = DeployError("SECRET_DELETE_CONFLICT", "密钥库清理范围已经变化，已停止清理。")
        if (not isinstance(plan, dict) or set(plan) != {"format", "app_id", "tenant_id", "data_generation", "dek_id", "secrets"}
                or type(plan["format"]) is not int or plan["format"] != 1):
            raise conflict
        app = self._app(db, plan["app_id"], allow_destroyed=True)
        if (app["data_generation"] != plan["data_generation"] or app["tenant_id"] != plan["tenant_id"]
                or app["dek_id"] != plan["dek_id"]):
            raise conflict
        if app["state"] == "destroyed":
            old = db.execute("SELECT plan FROM vault_tombstones WHERE app_id=? AND data_generation=?",
                             (plan["app_id"], plan["data_generation"])).fetchone()
            if (old is None or old["plan"] != _json(plan)
                    or any(db.execute(f"SELECT 1 FROM {table} WHERE app_id=?", (app["app_id"],)).fetchone()
                           for table in ("vault_app_keys", "vault_secrets", "vault_secret_versions"))):
                raise conflict
            return
        if self.inventory(db, plan["app_id"]) != plan:
            raise conflict

    def delete(self, db, plan, *, actor_id):
        with self._mutation(db, actor_id):
            self.verify_deletion(db, plan)
            app = self._app(db, plan["app_id"], allow_destroyed=True)
            if app["state"] == "destroyed":
                return
            db.execute("INSERT INTO vault_tombstones VALUES (?,?,?,?,?)",
                       (app["app_id"], app["data_generation"], _json(plan), actor_id, time.time()))
            db.execute("DELETE FROM vault_secret_versions WHERE app_id=?", (app["app_id"],))
            db.execute("DELETE FROM vault_secrets WHERE app_id=?", (app["app_id"],))
            db.execute("DELETE FROM vault_app_keys WHERE app_id=?", (app["app_id"],))
            db.execute("UPDATE vault_apps SET state='destroyed' WHERE app_id=?", (app["app_id"],))
            self._audit(db, actor_id, "secrets_destroyed", app["app_id"], detail={"data_generation": app["data_generation"]})

    def rewrap(self, db, *, actor_id):
        with self._mutation(db, actor_id):
            for row in db.execute("SELECT app_id FROM vault_apps WHERE state='active' ORDER BY app_id").fetchall():
                app = self._app(db, row["app_id"])
                dek = self._dek(db, app)
                nonce = self._nonce(db, "kek", self.keyring.active)
                ciphertext = AESGCM(self.keyring.key(self.keyring.active)).encrypt(nonce, dek, self._wrap_aad(app, app["dek_id"]))
                db.execute("UPDATE vault_app_keys SET kek_id=?,nonce=?,ciphertext=? WHERE dek_id=?",
                           (self.keyring.active, nonce, ciphertext, app["dek_id"]))
                self._audit(db, actor_id, "dek_rewrapped", app["app_id"], detail={"kek_id": self.keyring.active})
