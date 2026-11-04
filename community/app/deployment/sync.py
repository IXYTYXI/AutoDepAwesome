"""Durable, coalesced delivery. SQLite triggers commit intent with each mutation."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from app.integrations.bitable_writer import BitableWriter


class DeploymentSync:
    def __init__(self, service, cache, client):
        self.service, self.cache, self.client = service, cache, client
        self.tables = {
            kind: BitableWriter(
                client, service.settings, cache, table_id=table, view_id="", fields=[]
            )
            for kind, table in [
                ("app", service.settings.deploy_apps_table_id),
                ("job", service.settings.deploy_jobs_table_id),
            ]
            if table
        }

    async def deliver_feishu(self, kind, entity, data):
        table = self.tables.get(kind)
        if not table:
            raise RuntimeError("Deployment table not configured")
        s = self.service
        with s.store._connection() as db:
            owner = db.execute(
                "SELECT u.name FROM app_access a JOIN auth_users u ON u.id=a.owner_id WHERE a.app_id=?",
                (data.get("appId") or entity,),
            ).fetchone()
            if kind == "job":
                owner = db.execute(
                    "SELECT u.name FROM operation_auth a JOIN auth_users u ON u.id=a.user_id WHERE a.kind='job' AND a.id=?",
                    (entity,),
                ).fetchone()
            saved = db.execute(
                "SELECT record_id FROM community_sync_records WHERE kind=? AND entity_id=? AND table_id=?",
                (kind, entity, table.table_id),
            ).fetchone()
        key = "任务ID" if kind == "job" else "应用ID"
        stamp = datetime.fromtimestamp(data["updatedAt"], UTC).isoformat()
        if kind == "job":
            fields = {
                key: entity,
                "应用ID": data.get("appId") or "",
                "项目名称": data["subdomain"],
                "发起人": owner[0] if owner else "",
                "仓库地址": data["repository"],
                "版本": data.get("commit") or "",
                "阶段": data["phase"],
                "部署结果": data["status"],
                "错误摘要": " · ".join(
                    str(v) for v in (data.get("error") or {}).values() if v
                )[:4000],
                "访问地址": data.get("url") or "",
                "更新时间": stamp,
            }
        else:
            fields = {
                key: entity,
                "项目名称": data.get("subdomain") or data.get("lastSubdomain") or "",
                "负责人": owner[0] if owner else "",
                "仓库地址": data["repository"],
                "访问地址": data.get("url") or "",
                "当前版本": data.get("commit") or "",
                "应用状态": data["status"],
                "更新时间": stamp,
            }
        record_id = saved[0] if saved else None
        if not record_id:
            # Reconcile uncertain create acknowledgements before retrying creation.
            base = s.settings.feishu_bitable_app_token.get_secret_value()
            response = await self.client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table.table_id}/records/search",
                headers={"Authorization": "Bearer " + await table.token()},
                json={
                    "filter": {
                        "conjunction": "and",
                        "conditions": [
                            {"field_name": key, "operator": "is", "value": [entity]}
                        ],
                    }
                },
                params={"page_size": 100},
            )
            rows = table.payload(response)["data"].get("items", [])
            if len(rows) > 1:
                raise RuntimeError("Duplicate deployment key requires reconciliation")
            if rows:
                record_id = rows[0]["record_id"]
        results = await table.write(
            [{"record_id": record_id, "fields": fields}]
            if record_id
            else [{"fields": fields}],
            update=bool(record_id),
        )
        with s.store._connection(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO community_sync_records VALUES(?,?,?,?)",
                (kind, entity, table.table_id, results[0]["record_id"]),
            )

    async def once(self):
        s = self.service
        with s.store._connection() as db:
            pending = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM community_sync WHERE revision>done_revision AND retry_at<=? ORDER BY retry_at LIMIT 20",
                    (time.time(),),
                )
            ]
        for row in pending:
            try:
                data = await asyncio.to_thread(
                    s.snapshot, row["kind"], row["entity_id"]
                )
                if row["target"] == "redis":
                    await self.cache.set(
                        f"deploy:{row['kind']}:{row['entity_id']}",
                        json.dumps(data),
                        ttl=5,
                    )
                else:
                    await self.deliver_feishu(row["kind"], row["entity_id"], data)
                with s.store._connection(write=True) as db:
                    db.execute(
                        "UPDATE community_sync SET done_revision=?,attempts=0,retry_at=0 WHERE kind=? AND entity_id=? AND target=?",
                        (row["revision"], row["kind"], row["entity_id"], row["target"]),
                    )
            except Exception as exc:  # noqa: BLE001 — each outbox delivery must retry independently
                logging.getLogger("ows_community").warning(
                    "deployment_sync_retry",
                    extra={
                        "target": row["target"],
                        "kind": row["kind"],
                        "error_type": type(exc).__name__,
                    },
                )
                with s.store._connection(write=True) as db:
                    db.execute(
                        "UPDATE community_sync SET attempts=attempts+1,retry_at=? WHERE kind=? AND entity_id=? AND target=?",
                        (
                            time.time() + min(600, 5 * 2 ** min(row["attempts"], 7)),
                            row["kind"],
                            row["entity_id"],
                            row["target"],
                        ),
                    )

    async def run(self):
        while True:
            await self.once()
            await asyncio.sleep(3)
