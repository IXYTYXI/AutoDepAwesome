"""Application identity writer. Never borrows a developer's CLI login."""

import asyncio
from urllib.parse import quote

from app.core.error_codes import ErrorCode
from app.core.errors import AppError
from app.integrations.feishu_directory import FeishuDirectorySource


class BitableWriter(FeishuDirectorySource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_key += ":bitable-writer"

    async def token(self):
        async with self._token_lock:
            cached = await self.store.get(self.token_key)
            if cached:
                return cached
            s = self.settings
            response = await self.client.post(
                "https://accounts.feishu.cn/oauth/v3/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": s.feishu_app_id,
                    "client_secret": s.feishu_app_secret.get_secret_value(),
                    "scope": "bitable:app",
                },
            )
            data = self.payload(response)
            token, expires = data.get("access_token"), data.get("expires_in")
            if not isinstance(token, str) or not isinstance(expires, int):
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            await self.store.set(self.token_key, token, ttl=max(1, expires - 120))
            return token

    async def records(self):
        # Synchronization must see hidden/filtered rows too; a view is presentation only.
        return await self.all_records()

    async def all_records(self):
        base = quote(self.settings.feishu_bitable_app_token.get_secret_value(), safe="")
        path = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{self.table_id}/records/search"
        rows, page, seen = [], None, set()
        for _ in range(200):
            params = {"page_size": 500}
            if page:
                params["page_token"] = page
            response = await self.client.post(
                path,
                params=params,
                json={"field_names": self.fields},
                headers={"Authorization": "Bearer " + await self.token()},
                timeout=40,
            )
            data = self.payload(response)["data"]
            rows.extend(data["items"])
            if not data.get("has_more"):
                return rows
            page = data.get("page_token")
            if not page or page in seen:
                raise ValueError("Invalid Feishu pagination")
            seen.add(page)
        raise ValueError("Feishu pagination limit exceeded")

    async def write(self, records, *, update=False):
        if not records:
            return []
        base = quote(self.settings.feishu_bitable_app_token.get_secret_value(), safe="")
        action = "batch_update" if update else "batch_create"
        response = await self.client.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{self.table_id}/records/{action}",
            headers={"Authorization": "Bearer " + await self.token()},
            json={"records": records},
            timeout=40,
        )
        data = self.payload(response)["data"]["records"]
        if len(data) != len(records):
            raise ValueError("Incomplete Feishu batch acknowledgement")
        await asyncio.sleep(0.3)
        return data
