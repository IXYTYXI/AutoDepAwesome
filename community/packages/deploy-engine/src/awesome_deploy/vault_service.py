"""Application-authorized secret lifecycle and durable, fixed-scope revocation intents."""
from contextlib import contextmanager
import json
import math
import re
import time
import uuid

from .access import AccessStore, denied
from .errors import DeployError
from .secrets_store import SecretStore


class VaultService:
    def __init__(self, settings, store, *, vault=None, clock=time.time):
        self.settings, self.store, self.clock = settings, store, clock
        if vault is None:
            from .secret_vault import SecretVault
            vault = SecretVault(settings)
        self.vault = vault
        with store._connection(write=True) as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db):
        # executescript would commit the caller's authorization/migration transaction.
        db.execute("""CREATE TABLE IF NOT EXISTS vault_revocations (
            id TEXT PRIMARY KEY, app_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','recovery_required','succeeded')),
            execution TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(app_id,name,version))""")

    def _ready(self, db):
        from .vault_format import require_ready
        require_ready(self.settings, db)

    def _authorize(self, db, actor, app_id, action, *, recent=False):
        if (getattr(self.settings, "auth_mode", "basic") != "feishu"
                or not isinstance(actor, dict)
                or actor.get("app_id") != self.settings.feishu_app_id
                or actor.get("tenant_key") != self.settings.feishu_tenant_key):
            raise denied()
        role = AccessStore._require(db, actor, app_id, action)
        if recent:
            issued = actor.get("authenticated_at")
            if (type(issued) not in (int, float) or not math.isfinite(issued)
                    or not 0 <= self.clock() - issued <= 600):
                raise DeployError("AUTH_REAUTH_REQUIRED", "请重新使用飞书登录后再进行此操作。")
        return role

    @contextmanager
    def _transaction(self, *, write=False):
        try:
            with self.store._connection(write=write) as db:
                if not write:
                    db.execute("BEGIN")
                self._ready(db)
                yield db
        except DeployError:
            raise
        except Exception:
            # Dependency errors may contain submitted values or decrypted material.
            raise DeployError("SECRET_UNAVAILABLE", "无法安全访问密钥库，请检查后重试。") from None

    @staticmethod
    def _version(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise DeployError("INVALID_REQUEST", "请提供当前密钥版本。")

    def _ordinary(self, db, app_id, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise DeployError("INVALID_SECRET_NAME", "秘密配置名称格式不正确。")
        item = next((item for item in self.vault.metadata(db, app_id) if item["name"] == name), None)
        if item is None:
            raise DeployError("SECRET_NOT_FOUND", "该应用不存在指定的密钥。")
        if item["kind"] != "runtime_secret":
            raise DeployError("SECRET_MASTER_PROTECTED", "应用数据主密钥只能通过专项迁移处理。")
        return item

    @staticmethod
    def public_operation(operation):
        return {key: operation[key] for key in
                ("id", "app_id", "name", "version", "status", "error", "created_at", "updated_at")}

    def describe(self, app_id, *, actor=None):
        with self._transaction() as db:
            role = self._authorize(db, actor, app_id, "secrets")
            items = self.vault.metadata(db, app_id)
            operations = [self.public_operation(self._decode(row)) for row in db.execute(
                "SELECT * FROM vault_revocations WHERE app_id=? ORDER BY created_at DESC,id", (app_id,))]
            return {"appId": app_id, "items": items, "operations": operations,
                    "can_rotate": True, "can_revoke": role in {"owner", "admin"}}

    def rotate(self, app_id, name, value, expected_current_version, *, actor=None):
        from .deployment_secrets import DeploymentSecrets
        SecretStore.validate_value(value)
        self._version(expected_current_version)
        with DeploymentSecrets(self.settings, self.store)._worker_lock():
            with self._transaction(write=True) as db:
                self._authorize(db, actor, app_id, "secrets_rotate", recent=True)
                self._require_no_offline(db, app_id)
                self._ordinary(db, app_id, name)
                version = self.vault.rotate(db, app_id, name, value, expected_current_version,
                                            actor_id=actor["id"])
                return {"name": name, "current_version": version}

    @staticmethod
    def _require_no_offline(db, app_id):
        if db.execute("""SELECT 1 FROM offline_operations WHERE app_id=?
                AND status IN ('queued','running','recovery_required') LIMIT 1""", (app_id,)).fetchone():
            raise DeployError('SECRET_CONFIGURATION_BUSY', '该应用正在下线或销毁，请完成清理后再变更密钥状态。')

    @staticmethod
    def _affected_jobs(db, app_id, name, version):
        affected = []
        for row in db.execute("SELECT id,snapshot,execution FROM jobs WHERE app_id=? ORDER BY id", (app_id,)):
            snapshot, execution = json.loads(row["snapshot"] or "{}"), json.loads(row["execution"] or "{}")
            if execution.get("resources_released") or execution.get("data_deleted"):
                continue
            if any(versions.get(name) == version for source in (snapshot, execution)
                   for versions in source.get("secret_versions", {}).values()):
                affected.append(row["id"])
        return affected

    def revoke(self, app_id, name, version, expected_current_version, reason, *, actor=None):
        self._version(version)
        self._version(expected_current_version)
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 1024
                or any(c in reason for c in "\r\n\0")):
            raise DeployError("INVALID_REQUEST", "请填写 1–1024 字符的单行吊销原因。")
        # Do not acquire worker.lock: revocation must invalidate a version during a long build.
        with self._transaction(write=True) as db:
            self._authorize(db, actor, app_id, "secrets_revoke", recent=True)
            self._require_no_offline(db, app_id)
            item = self._ordinary(db, app_id, name)
            if item["current_version"] != expected_current_version:
                raise DeployError("SECRET_VERSION_CONFLICT", "当前密钥版本已变化，请刷新后重试。")
            existing = db.execute("SELECT * FROM vault_revocations WHERE app_id=? AND name=? AND version=?",
                                  (app_id, name, version)).fetchone()
            if existing:
                operation = self._decode(existing)
                if operation["status"] == "recovery_required":
                    db.execute("UPDATE vault_revocations SET status='queued',error=NULL,updated_at=? WHERE id=?",
                               (self.clock(), operation["id"]))
                    AccessStore._audit(db, actor["id"], "secret_revoke_retry", app_id, operation["id"])
                return self._get(db, operation["id"])
            self.vault.revoke(db, app_id, name, version, expected_current_version, actor_id=actor["id"])
            affected = self._affected_jobs(db, app_id, name, version)
            operation_id, now = uuid.uuid4().hex, self.clock()
            execution = {"job_ids": affected, "reason": reason, "actor_id": actor["id"]}
            db.execute("INSERT INTO vault_revocations VALUES(?,?,?,?,?,?,?,?,?)",
                       (operation_id, app_id, name, version, "queued", json.dumps(execution, sort_keys=True),
                        None, now, now))
            AccessStore._audit(db, actor["id"], "secret_revoke_accepted", app_id, operation_id,
                               {"name": name, "version": version, "job_ids": affected})
            return self._get(db, operation_id)

    @staticmethod
    def _decode(row):
        return {**dict(row), "execution": json.loads(row["execution"]),
                "error": json.loads(row["error"]) if row["error"] else None}

    @classmethod
    def _get(cls, db, operation_id):
        row = db.execute("SELECT * FROM vault_revocations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise DeployError("NOT_FOUND", "未找到密钥吊销操作。")
        return cls._decode(row)

    def next_revocation(self):
        with self._transaction() as db:
            row = db.execute("""SELECT * FROM vault_revocations WHERE status IN ('queued','running')
                                ORDER BY created_at,id LIMIT 1""").fetchone()
            return self._decode(row) if row else None

    def get_revocation(self, operation_id):
        with self._transaction() as db:
            return self._get(db, operation_id)

    def claim_revocation(self, operation_id):
        with self._transaction(write=True) as db:
            operation = self._get(db, operation_id)
            if operation["status"] not in {"queued", "running"}:
                raise DeployError("SECRET_REVOCATION_STATE", "吊销任务需要显式重试或已完成。")
            db.execute("UPDATE vault_revocations SET status='running',updated_at=? WHERE id=?",
                       (self.clock(), operation_id))
            return self._get(db, operation_id)

    def checkpoint_revocation(self, operation_id, delta):
        with self._transaction(write=True) as db:
            operation = self._get(db, operation_id)
            if operation["status"] != "running" or not isinstance(delta, dict) or set(delta) & {
                "job_ids", "reason", "actor_id",
            }:
                raise DeployError("SECRET_REVOCATION_STATE", "吊销任务的已受理范围不能更改。")
            if "confirmed_job_ids" in delta:
                confirmed = delta["confirmed_job_ids"]
                if (not isinstance(confirmed, list) or not all(isinstance(item, str) for item in confirmed)
                        or len(set(confirmed)) != len(confirmed)
                        or not set(confirmed).issubset(operation["execution"]["job_ids"])):
                    raise DeployError("SECRET_REVOCATION_STATE", "只能确认已受理范围内的实例。")
            execution = {**operation["execution"], **delta}
            db.execute("UPDATE vault_revocations SET execution=?,updated_at=? WHERE id=?",
                       (json.dumps(execution, sort_keys=True), self.clock(), operation_id))
            return self._get(db, operation_id)

    def finish_revocation(self, operation_id):
        with self._transaction(write=True) as db:
            operation = self._get(db, operation_id)
            if operation["status"] not in {"running", "succeeded"}:
                raise DeployError("SECRET_REVOCATION_STATE", "吊销任务尚未执行。")
            if set(operation["execution"].get("confirmed_job_ids", [])) != set(operation["execution"]["job_ids"]):
                raise DeployError("SECRET_REVOCATION_STATE", "尚未确认所有受影响实例已停用。")
            db.execute("UPDATE vault_revocations SET status='succeeded',error=NULL,updated_at=? WHERE id=?",
                       (self.clock(), operation_id))
            return self._get(db, operation_id)

    def fail_revocation(self, operation_id, error):
        with self._transaction(write=True) as db:
            operation = self._get(db, operation_id)
            if operation["status"] not in {"running", "recovery_required"}:
                raise DeployError("SECRET_REVOCATION_STATE", "吊销任务状态不允许记录失败。")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
                code = "SECRET_REVOCATION_FAILED"
            safe = {"code": code, "message": "尚未确认所有受影响实例已停用，请检查并重试吊销。"}
            db.execute("UPDATE vault_revocations SET status='recovery_required',error=?,updated_at=? WHERE id=?",
                       (json.dumps(safe), self.clock(), operation_id))
            return self._get(db, operation_id)
