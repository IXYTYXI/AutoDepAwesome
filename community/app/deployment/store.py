"""Community submission constraints, enforced inside the engine transaction."""

import sqlite3
from collections import defaultdict

from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store


class CommunityDeploymentStore(Store):
    def __init__(self, settings):
        super().__init__(settings)
        with self._connection() as db:
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS community_names (
                name TEXT PRIMARY KEY, app_id TEXT NOT NULL UNIQUE);
            DROP TRIGGER IF EXISTS community_submit_guard;
            CREATE TRIGGER community_submit_guard BEFORE INSERT ON jobs
            WHEN NEW.app_id IS NOT NULL BEGIN
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM community_names WHERE name=json_extract(NEW.input,'$.subdomain') AND app_id<>NEW.app_id
                ) OR EXISTS (
                    SELECT 1 FROM apps WHERE subdomain=json_extract(NEW.input,'$.subdomain') AND id<>NEW.app_id
                ) THEN RAISE(ABORT,'COMMUNITY_NAME_CONFLICT') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM community_names WHERE app_id=NEW.app_id AND name<>json_extract(NEW.input,'$.subdomain')
                ) THEN RAISE(ABORT,'COMMUNITY_IDENTITY_CONFLICT') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM jobs WHERE app_id=NEW.app_id AND status IN ('queued','running','recovery_required')
                ) THEN RAISE(ABORT,'COMMUNITY_JOB_CONFLICT') END;
                SELECT CASE WHEN (SELECT count(*) FROM jobs WHERE status IN ('queued','running')) >= 100
                    THEN RAISE(ABORT,'COMMUNITY_QUEUE_FULL') END;
                INSERT INTO community_names(name,app_id) VALUES(json_extract(NEW.input,'$.subdomain'),NEW.app_id)
                    ON CONFLICT(name) DO NOTHING;
            END;
            """)
            # Preserve live bindings and quarantine ambiguous legacy names.
            # Existing live domain bindings are authoritative. Historical jobs may
            # include released domains or failed candidates owned by other apps.
            live = dict(db.execute("SELECT subdomain,id FROM apps WHERE subdomain IS NOT NULL"))
            candidates = db.execute("""
                WITH latest AS (
                    SELECT json_extract(input,'$.subdomain') AS name,app_id,MAX(created_at) AS last_job
                    FROM jobs WHERE app_id IS NOT NULL GROUP BY name,app_id
                )
                SELECT name,app_id,last_job FROM latest WHERE NOT EXISTS (
                    SELECT 1 FROM offline_operations o WHERE o.app_id=latest.app_id
                    AND o.status='succeeded' AND o.updated_at>=latest.last_job)
            """).fetchall()
            owners = defaultdict(set)
            for name, app_id, _ in candidates:
                if name and name not in live:
                    owners[name].add(app_id)
            # A reserved sentinel denies both new claims and updates by either
            # historical owner until an operator explicitly resolves the conflict.
            rows = list(live.items()) + [
                (name, next(iter(ids)) if len(ids) == 1 else "legacy-conflict:" + name)
                for name, ids in owners.items()
            ]
            for name, app_id in rows:
                prior = db.execute(
                    "SELECT app_id FROM community_names WHERE name=?", (name,)
                ).fetchone()
                if prior and prior[0] != app_id:
                    raise DeployError(
                        "DOMAIN_CONFLICT", "历史部署存在同名应用，需要先核对迁移。"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO community_names VALUES(?,?)", (name, app_id)
                )
            db.commit()

    def application_snapshot(self, app_id):
        """One application's consistent state, including its active release."""
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if row is None:
                return None
            app = self._app_view(db, row)
            operation = db.execute(
                "SELECT * FROM offline_operations WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (app_id,),
            ).fetchone()
            app.update(
                active_job=self._get(db, app["active_release_id"])
                if app["active_release_id"]
                else None,
                retained_job=self._retained_release(db, app_id),
                operation=self._offline(operation) if operation else None,
            )
            return app

    def before_enqueue(self, db, payload, actor):
        # Runs after idempotency lookup, before secret-scope acquisition. The SQL
        # trigger remains the final guard for any alternate engine writer.
        row = db.execute(
            "SELECT app_id FROM community_names WHERE name=?", (payload["subdomain"],)
        ).fetchone()
        if row and row[0] != payload.get("appId"):
            raise DeployError(
                "DOMAIN_CONFLICT",
                "项目名称已存在，请更换名称；更新应用请从已有应用中选择。",
            )

    def enqueue(self, *args, **kwargs):
        try:
            return super().enqueue(*args, **kwargs)
        except sqlite3.IntegrityError as exc:
            errors = {
                "COMMUNITY_IDENTITY_CONFLICT": (
                    "APP_IDENTITY_CONFLICT",
                    "更新应用不能更换项目名称。",
                ),
                "COMMUNITY_NAME_CONFLICT": (
                    "DOMAIN_CONFLICT",
                    "项目名称已存在，请更换名称；更新应用请从已有应用中选择。",
                ),
                "COMMUNITY_JOB_CONFLICT": (
                    "DEPLOYMENT_CONFLICT",
                    "该应用已有进行中或待恢复任务，请勿重复提交。",
                ),
                "COMMUNITY_QUEUE_FULL": (
                    "DEPLOYMENT_CONFLICT",
                    "部署队列已满，请稍后重试。",
                ),
            }
            if str(exc) in errors:
                raise DeployError(*errors[str(exc)]) from None
            raise
