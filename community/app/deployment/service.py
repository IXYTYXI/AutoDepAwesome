import re
import time
from dataclasses import replace

from awesome_deploy.access import AccessStore
from awesome_deploy.cli import load_settings
from awesome_deploy.config import Settings as EngineSettings
from awesome_deploy.errors import DeployError
from awesome_deploy.identity import IdentityStore
from awesome_deploy.management import public_application
from awesome_deploy.source import validate_repository

from app.deployment.store import CommunityDeploymentStore


class LazyEligibility:
    def __init__(self, settings):
        self.settings = settings

    def check_eligible(self, open_id):
        from awesome_deploy.feishu import FeishuProvider

        provider = FeishuProvider(self.settings)
        try:
            return provider.check_eligible(open_id)
        finally:
            provider.close()


class DeploymentService:
    def __init__(self, settings):
        self.settings = settings
        base = (
            load_settings(settings.deploy_engine_config)
            if settings.deploy_engine_config
            else EngineSettings(
                state_dir=settings.deploy_state_root,
                max_cpu=2,
                max_memory_mib=2048,
                required_free_mib=4096,
                port_start=22000,
                port_end=22999,
                builder_name="ows-deploy-builder",
            )
        )
        # Local development never executes Docker. Production config retains continuous
        # eligibility checks; the community login replaces only the OAuth entry point.
        tenant = settings.deploy_tenant_id or (
            settings.feishu_allowed_tenants[0]
            if settings.feishu_allowed_tenants
            else ""
        )
        if settings.deploy_engine_config and (
            base.auth_mode != "feishu"
            or base.feishu_app_id != settings.feishu_app_id
            or base.feishu_tenant_key != tenant
            or base.feishu_eligibility_mode not in ("continuous", "app_visibility")
        ):
            raise ValueError("部署引擎身份必须与社区一致，并启用 continuous 或 app_visibility 资格校验")
        self.engine = replace(
            base,
            auth_mode="feishu",
            feishu_app_id=settings.feishu_app_id,
            feishu_tenant_key=tenant,
        )
        if (
            base.port_start <= settings.port <= base.port_end
            or base.port_start <= settings.frontend_port <= base.port_end
            or any(base.port_start <= p <= base.port_end for p in (80, 443, 6379))
        ):
            raise ValueError("部署容器端口范围不能包含社区服务端口")
        self.store = CommunityDeploymentStore(self.engine)
        self.identity = IdentityStore(
            self.engine, provider=LazyEligibility(self.engine),
            shared_database_gid=settings.deploy_database_gid,
        )
        self.access = AccessStore(self.engine, identity=self.identity)
        self.install_outbox()

    def install_outbox(self):
        with self.store._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS community_sync (
              kind TEXT NOT NULL, entity_id TEXT NOT NULL, target TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, done_revision INTEGER NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(kind,entity_id,target));
            CREATE INDEX IF NOT EXISTS community_sync_due ON community_sync(retry_at);
            CREATE TABLE IF NOT EXISTS community_sync_records (
              kind TEXT NOT NULL, entity_id TEXT NOT NULL, table_id TEXT NOT NULL, record_id TEXT NOT NULL,
              PRIMARY KEY(kind,entity_id,table_id));
            """)
            for table, kind in [("jobs", "job"), ("apps", "app")]:
                for event in ["INSERT", "UPDATE"]:
                    db.executescript(f"""
                    BEGIN IMMEDIATE;
                    DROP TRIGGER IF EXISTS community_{table}_{event.lower()};
                    CREATE TRIGGER community_{table}_{event.lower()}
                    AFTER {event} ON {table} BEGIN
                      INSERT INTO community_sync(kind,entity_id,target) VALUES('{kind}',NEW.id,'redis')
                      ON CONFLICT(kind,entity_id,target) DO UPDATE SET revision=revision+1;
                      INSERT INTO community_sync(kind,entity_id,target) VALUES('{kind}',NEW.id,'feishu')
                      ON CONFLICT(kind,entity_id,target) DO UPDATE SET revision=revision+1;
                    END;
                    COMMIT;""")
                for target in ["redis", "feishu"]:
                    db.execute(
                        f"INSERT OR IGNORE INTO community_sync(kind,entity_id,target) SELECT ?,id,? FROM {table}",
                        (kind, target),
                    )

    def actor(self, user):
        if (
            user.identity_source != "feishu"
            or not self.engine.feishu_tenant_key
            or user.tenant_id != self.engine.feishu_tenant_key
        ):
            raise DeployError("ACCESS_DENIED", "部署需要本组织的飞书身份。")
        if (
            self.settings.deploy_allowed_users
            and user.user_id not in self.settings.deploy_allowed_users
        ):
            raise DeployError("ACCESS_DENIED", "当前用户未开通部署权限。")
        prefix = f"feishu:{self.engine.feishu_app_id}:"
        if not user.user_id.startswith(prefix):
            raise DeployError("ACCESS_DENIED", "飞书应用身份不匹配。")
        open_id = user.user_id[len(prefix) :]
        with self.identity.connection() as db:
            row = db.execute(
                "SELECT * FROM auth_users WHERE app_id=? AND tenant_key=? AND open_id=?",
                (self.engine.feishu_app_id, user.tenant_id, open_id),
            ).fetchone()
        if row:
            actor = dict(row)
            if not actor["enabled"] or not actor["eligible"]:
                raise DeployError("ACCESS_DENIED", "部署账号已停用。")
            return actor
        return self.identity.login_user(
            {
                "open_id": open_id,
                "tenant_key": user.tenant_id,
                "name": user.display_name,
            }
        )

    def executor_ready(self):
        if not self.settings.deploy_execution_enabled:
            return False
        with self.store._connection() as db:
            row = (
                db.execute(
                    "SELECT value FROM community_runtime WHERE key='heartbeat'"
                ).fetchone()
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='community_runtime'"
                ).fetchone()
                else None
            )
        return bool(row and 0 <= time.time() - float(row[0]) < 30)

    def validate_name(self, name):
        if (
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", name)
            or name in self.engine.reserved_names
            or any(w in name for w in self.engine.banned_words)
        ):
            raise DeployError("INVALID_SUBDOMAIN", "项目名称不符合规则或属于保留名称。")

    def name_available(self, user, name, app_id=None):
        actor = self.actor(user)
        self.validate_name(name)
        if app_id:
            self.access.require_app(actor, app_id, "deploy")
        with self.store._connection() as db:
            row = db.execute(
                "SELECT app_id FROM community_names WHERE name=?", (name,)
            ).fetchone()
            legacy = db.execute(
                "SELECT id FROM apps WHERE subdomain=?", (name,)
            ).fetchone()
        owner = row[0] if row else legacy[0] if legacy else None
        return owner is None or owner == app_id

    def submit(self, user, repository, name, key, app_id=None):
        actor = self.actor(user)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise DeployError("INVALID_IDEMPOTENCY_KEY", "缺少有效的请求编号。")
        self.validate_name(name)
        payload = {
            "repository": validate_repository(repository, self.engine),
            "subdomain": name,
            "ref": "main",
        }
        if app_id:
            payload["appId"] = app_id
        # Retrying an accepted request still returns its durable result while the
        # executor is stopped; readiness gates only creation of new work.
        with self.store._connection() as db:
            prior = db.execute(
                "SELECT id FROM jobs WHERE idempotency_key=?",
                (AccessStore.scoped_key(actor, "job", key),),
            ).fetchone()
        if prior:
            return self.project_job(self.store.enqueue(payload, key, actor=actor))
        if not self.settings.deploy_execution_enabled:
            raise DeployError(
                "EXECUTOR_UNAVAILABLE", "部署执行环境尚未启用，未创建任务。"
            )
        if not self.executor_ready():
            raise DeployError(
                "EXECUTOR_UNAVAILABLE", "部署 Worker 未就绪，请稍后重试。"
            )
        return self.project_job(self.store.enqueue(payload, key, actor=actor))

    def project_job(self, job):
        active = self.store.active_release(job["app_id"]) if job.get("app_id") else None
        return {
            "id": job["id"],
            "appId": job["app_id"],
            "status": job["status"],
            "phase": job["phase"],
            "terminal": job["status"]
            in ["succeeded", "failed", "recovery_required", "cancelled"],
            "repository": job["input"]["repository"],
            "subdomain": job["input"]["subdomain"],
            "commit": (job.get("snapshot") or {}).get("commit"),
            "updatedAt": job["updated_at"],
            "url": f"https://{job['input']['subdomain']}.{self.engine.base_domain}"
            if job["status"] == "succeeded" or (active and active["id"] == job["id"])
            else None,
            "error": {
                "code": job["error"]["code"],
                "message": job["error"].get("message", "部署失败"),
                "field": job["error"].get("path", ""),
                "suggestion": job["error"].get("suggestion", ""),
            }
            if job.get("error")
            else None,
        }

    def get_job(self, user, job_id):
        actor = self.actor(user)
        job = self.store.get(job_id)
        self.access.require_job(actor, job)
        return self.project_job(job)

    def list_jobs(self, user):
        return [
            self.project_job(j) for j in self.store.list_jobs(actor=self.actor(user))
        ]

    def snapshot(self, kind, entity_id):
        if kind == "job":
            return self.project_job(self.store.get(entity_id))
        app = self.store.application_snapshot(entity_id)
        return public_application(app, self.engine.base_domain) if app else None
