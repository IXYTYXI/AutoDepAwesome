"""Local identities, single-use OAuth attempts and revocable opaque sessions.

Only Feishu-verified identity attributes reach login_user. Platform tokens are
independent of Feishu tokens; neither provider access nor refresh tokens persist.
"""

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid

from .errors import DeployError
from .feishu import FeishuProvider, FeishuUnavailable, OPEN_ID

SESSION_COOKIE = "__Host-awesome_session"
OAUTH_COOKIE = "__Host-awesome_oauth"
SESSION_MAX_AGE = 8 * 60 * 60
SESSION_IDLE = 30 * 60
ELIGIBILITY_TTL = 5 * 60
FRESH_LOGIN = 10 * 60
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token):
    return hashlib.sha256(("awesome-csrf-v1:" + token).encode()).hexdigest()


class IdentityStore:
    def __init__(self, settings, *, provider=None, clock=time.time):
        self.settings, self.clock = settings, clock
        self.provider = provider if provider is not None else FeishuProvider(settings)
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if settings.database_path.is_symlink():
            raise DeployError("IDENTITY_UNAVAILABLE", "身份数据库路径不安全。")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, tenant_key TEXT NOT NULL,
                    open_id TEXT NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    can_deploy INTEGER NOT NULL DEFAULT 1, is_admin INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1, checked_at REAL NOT NULL,
                    eligible INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(app_id,tenant_key,open_id)
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id),
                    user_revision INTEGER NOT NULL, created_at REAL NOT NULL,
                    last_seen REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_oauth (
                    state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires_at REAL NOT NULL, return_path TEXT NOT NULL,
                    auth_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_meta (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO auth_meta VALUES(1,1);
                CREATE TABLE IF NOT EXISTS auth_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, action TEXT NOT NULL,
                    target_id TEXT, result TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_login_failures (
                    bucket INTEGER NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(bucket,reason)
                );
            """)
        os.chmod(settings.database_path, 0o600)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.settings.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
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

    def _audit(self, db, action, actor_id=None, target_id=None, result="success", details=None):
        db.execute("INSERT INTO auth_audit(actor_id,action,target_id,result,details,created_at) VALUES(?,?,?,?,?,?)",
                   (actor_id, action, target_id, result, json.dumps(details or {}, sort_keys=True), self.clock()))

    def audit(self, action, actor_id=None, target_id=None, result="success", details=None):
        with self.connection(write=True) as db:
            self._audit(db, action, actor_id, target_id, result, details)

    def login_failure(self, reason):
        """Aggregate verified-attempt failures; anonymous probes never grow the audit log."""
        if reason not in {"cancelled", "invalid", "denied", "unavailable", "expired"}:
            return
        bucket = int(self.clock() // 300)
        with self.connection(write=True) as db:
            db.execute("DELETE FROM auth_login_failures WHERE bucket<? OR bucket>?", (bucket - 287, bucket))
            db.execute("""INSERT INTO auth_login_failures VALUES(?,?,1)
                          ON CONFLICT(bucket,reason) DO UPDATE SET count=MIN(count+1,2147483647)""",
                       (bucket, reason))

    def _scope(self, row):
        return (row and row["app_id"] == self.settings.feishu_app_id
                and row["tenant_key"] == self.settings.feishu_tenant_key)

    def get_user(self, user_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
        if not self._scope(row):
            raise DeployError("ACCESS_DENIED", "无权访问该用户。")
        return dict(row)

    def list_users(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? ORDER BY name,id",
                (self.settings.feishu_app_id, self.settings.feishu_tenant_key))]

    def login_user(self, identity):
        if (identity.get("tenant_key") != self.settings.feishu_tenant_key
                or not OPEN_ID.fullmatch(identity.get("open_id", ""))):
            raise DeployError("ACCESS_DENIED", "飞书身份不属于本平台允许的组织。")
        name = identity.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name):
            raise DeployError("ACCESS_DENIED", "飞书用户资料无效。")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? AND open_id=?",
                             (self.settings.feishu_app_id, self.settings.feishu_tenant_key, identity["open_id"])).fetchone()
            if row:
                if not row["enabled"]:
                    raise DeployError("ACCESS_DENIED", "平台账号已停用，请联系管理员。")
                user_id = row["id"]
                db.execute("UPDATE auth_users SET name=?,eligible=1,checked_at=?,revision=revision+? WHERE id=?",
                           (name, self.clock(), 0 if row["eligible"] else 1, user_id))
            else:
                user_id = uuid.uuid4().hex
                db.execute("""INSERT INTO auth_users(id,app_id,tenant_key,open_id,name,checked_at)
                              VALUES(?,?,?,?,?,?)""",
                           (user_id, self.settings.feishu_app_id, self.settings.feishu_tenant_key,
                            identity["open_id"], name, self.clock()))
            self._audit(db, "login", user_id, user_id)
        return self.get_user(user_id)

    def authorize_user(self, user_id, expected_revision=None, force=False):
        user = self.get_user(user_id)
        if (not user["enabled"] or not user["eligible"]
                or expected_revision is not None and user["revision"] != expected_revision):
            raise DeployError("ACCESS_DENIED", "用户权限已失效，请重新获取授权。")
        age = self.clock() - user["checked_at"]
        if force or not 0 <= age < ELIGIBILITY_TTL:
            try:
                eligible = self.provider.check_eligible(user["open_id"])
            except FeishuUnavailable:
                raise DeployError("IDENTITY_UNAVAILABLE", "暂时无法核验飞书身份，请稍后重试。") from None
            with self.connection(write=True) as db:
                current = db.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
                # A slow provider response cannot overwrite a concurrent local revocation.
                if current["revision"] == user["revision"]:
                    db.execute("UPDATE auth_users SET eligible=?,checked_at=?,revision=revision+? WHERE id=?",
                               (int(eligible), self.clock(), 0 if eligible else 1, user_id))
                    if not eligible:
                        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
                        self._audit(db, "provider_revoked", None, user_id)
            user = self.get_user(user_id)
            if (not user["enabled"] or not user["eligible"]
                    or expected_revision is not None and user["revision"] != expected_revision):
                raise DeployError("ACCESS_DENIED", "用户已离职、停用或不在应用可用范围内。")
        return user

    def set_user(self, user_id, *, enabled=None, can_deploy=None, is_admin=None, actor_id=None,
                 expected_actor_revision=None):
        changes = {k: int(v) for k, v in {"enabled": enabled, "can_deploy": can_deploy,
                                        "is_admin": is_admin}.items() if type(v) is bool}
        if not changes:
            raise DeployError("INVALID_REQUEST", "至少指定一项账号设置。")
        with self.connection(write=True) as db:
            if actor_id is not None:
                actor = db.execute("SELECT * FROM auth_users WHERE id=?", (actor_id,)).fetchone()
                if (not self._scope(actor) or not actor["enabled"] or not actor["eligible"] or not actor["is_admin"]
                        or actor["revision"] != expected_actor_revision or is_admin is not None):
                    raise DeployError("ACCESS_DENIED", "管理员权限已失效。")
            row = db.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
            if not self._scope(row):
                raise DeployError("ACCESS_DENIED", "无权修改该用户。")
            if actor_id is not None and enabled is False and row["is_admin"] and row["enabled"]:
                remaining = db.execute("""SELECT 1 FROM auth_users WHERE app_id=? AND tenant_key=?
                                          AND id<>? AND is_admin=1 AND enabled=1 AND eligible=1 LIMIT 1""",
                                       (self.settings.feishu_app_id, self.settings.feishu_tenant_key,
                                        user_id)).fetchone()
                if remaining is None:
                    raise DeployError("LAST_ADMIN_REQUIRED", "不能停用最后一位有效管理员，请先设置其他管理员。")
            db.execute("UPDATE auth_users SET " + ",".join(k + "=?" for k in changes)
                       + ",revision=revision+1 WHERE id=?", (*changes.values(), user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self._audit(db, "user_permissions", actor_id, user_id, details=changes)
        return self.get_user(user_id)

    def begin_login(self, return_path="/"):
        attempt = {"state": secrets.token_urlsafe(32), "browser": secrets.token_urlsafe(32),
                   "verifier": secrets.token_urlsafe(48),
                   "return_path": return_path if return_path in {"/", "/manage", "/members"} else "/"}
        now = self.clock()
        with self.connection(write=True) as db:
            db.execute("DELETE FROM auth_oauth WHERE expires_at<=?", (now,))
            # Bound unauthenticated storage; admission fails instead of evicting live attempts.
            if db.execute("SELECT COUNT(*) FROM auth_oauth").fetchone()[0] >= 1000:
                raise DeployError("AUTH_BUSY", "登录请求较多，请稍后重试。")
            epoch = db.execute("SELECT epoch FROM auth_meta WHERE id=1").fetchone()[0]
            db.execute("INSERT INTO auth_oauth VALUES(?,?,?,?,?,?)",
                       (digest(attempt["state"]), digest(attempt["browser"]), attempt["verifier"],
                        now + 300, attempt["return_path"], epoch))
        return attempt

    def consume_login(self, state, browser):
        if not TOKEN.fullmatch(state or "") or not TOKEN.fullmatch(browser or ""):
            raise DeployError("AUTH_STATE_INVALID", "登录请求已失效，请重新登录。")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM auth_oauth WHERE state_hash=?", (digest(state),)).fetchone()
            if (not row or row["expires_at"] <= self.clock()
                    or not hmac.compare_digest(row["browser_hash"], digest(browser))):
                raise DeployError("AUTH_STATE_INVALID", "登录请求已失效，请重新登录。")
            db.execute("DELETE FROM auth_oauth WHERE state_hash=?", (digest(state),))
        return dict(row)

    def create_session(self, user_id, *, expected_epoch=None):
        token, now = secrets.token_urlsafe(32), self.clock()
        with self.connection(write=True) as db:
            if (expected_epoch is not None
                    and db.execute("SELECT epoch FROM auth_meta WHERE id=1").fetchone()[0] != expected_epoch):
                raise DeployError("AUTH_STATE_INVALID", "登录请求已撤销，请重新登录。")
            user = db.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
            if not self._scope(user) or not user["enabled"] or not user["eligible"]:
                raise DeployError("ACCESS_DENIED", "用户权限已失效。")
            db.execute("DELETE FROM auth_sessions WHERE expires_at<=? OR last_seen<=?", (now, now - SESSION_IDLE))
            # Limit abandoned browser sessions per user without truncating absolute expiry.
            db.execute("""DELETE FROM auth_sessions WHERE token_hash IN
                       (SELECT token_hash FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 19)""",
                       (user_id,))
            db.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?)",
                       (digest(token), user_id, user["revision"], now, now, now + SESSION_MAX_AGE))
        return token

    def session(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        now, token_hash = self.clock(), digest(token)
        with self.connection() as db:
            row = db.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if (not row or now >= row["expires_at"] or now - row["last_seen"] >= SESSION_IDLE
                or now < row["created_at"]):
            return None
        try:
            user = self.authorize_user(row["user_id"], row["user_revision"])
        except DeployError as error:
            if error.code == "IDENTITY_UNAVAILABLE":
                raise
            return None
        with self.connection(write=True) as db:
            # Recheck user revision and token existence atomically against logout/revocation.
            now = self.clock()
            found = db.execute("""SELECT s.* FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                                WHERE s.token_hash=? AND s.user_revision=u.revision AND u.enabled=1 AND u.eligible=1""",
                               (token_hash,)).fetchone()
            if (not found or now >= found["expires_at"] or now - found["last_seen"] >= SESSION_IDLE
                    or now < found["created_at"]):
                return None
            db.execute("UPDATE auth_sessions SET last_seen=MAX(last_seen,?) WHERE token_hash=?", (now, token_hash))
        return {**user, "authenticated_at": row["created_at"]}

    def logout(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return
        with self.connection(write=True) as db:
            row = db.execute("SELECT user_id FROM auth_sessions WHERE token_hash=?", (digest(token),)).fetchone()
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))
            if row:
                self._audit(db, "logout", row["user_id"], row["user_id"])

    def revoke_all_sessions(self):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM auth_sessions")
            db.execute("DELETE FROM auth_oauth")
            db.execute("UPDATE auth_meta SET epoch=epoch+1 WHERE id=1")
            self._audit(db, "sessions_reset")
