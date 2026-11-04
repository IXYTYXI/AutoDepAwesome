"""Application-owned Base reader. No CLI or browser credentials at runtime."""
import asyncio
from urllib.parse import quote, urlsplit, parse_qs

from app.core.errors import AppError
from app.core.error_codes import ErrorCode

FIELDS = ['人员', '形象', '个性签名', '团队', '定位', 'title', 'tag']


class FeishuDirectorySource:
    def __init__(self, client, settings, store, *, table_id=None, view_id=None, fields=None, automatic_fields=False):
        self.client, self.settings, self.store = client, settings, store
        self.table_id = settings.officers_table_id if table_id is None else table_id
        self.view_id = settings.officers_view_id if view_id is None else view_id
        self.fields = FIELDS if fields is None else fields
        self.automatic_fields = automatic_fields
        self._token_lock = asyncio.Lock()
        self.token_key = 'feishu:tenant-token:' + settings.feishu_app_id

    async def token(self):
        async with self._token_lock:
            cached = await self.store.get(self.token_key)
            if cached:
                return cached
            s = self.settings
            if not s.feishu_app_id or not s.feishu_app_secret.get_secret_value():
                raise AppError(ErrorCode.FEISHU_NOT_CONFIGURED)
            response = await self.client.post('https://accounts.feishu.cn/oauth/v3/token', data={
                'grant_type': 'client_credentials', 'client_id': s.feishu_app_id,
                'client_secret': s.feishu_app_secret.get_secret_value(),
                'scope': 'bitable:app:readonly',
            })
            data = self.payload(response)
            token, expires = data.get('access_token'), data.get('expires_in')
            if not isinstance(token, str) or not token or not isinstance(expires, int) or expires < 1:
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            await self.store.set(self.token_key, token, ttl=max(1, expires - 120))
            return token

    @staticmethod
    def payload(response):
        if not response.is_success:
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        try:
            data = response.json()
        except ValueError:
            raise AppError(ErrorCode.UPSTREAM_ERROR) from None
        if not isinstance(data, dict) or data.get('code', 0) != 0 or data.get('error'):
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        return data

    async def records(self):
        s = self.settings
        base = s.feishu_bitable_app_token.get_secret_value()
        if not base or not self.table_id or not self.view_id:
            raise AppError(ErrorCode.FEISHU_NOT_CONFIGURED)
        path = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{quote(base, safe="")}/tables/{quote(self.table_id, safe="")}/records/search'
        body = {'view_id': self.view_id, 'field_names': self.fields}
        if self.automatic_fields:
            body['automatic_fields'] = True
        items, seen = [], set()
        page = None
        for _ in range(100):
            params = {'page_size': 500, 'user_id_type': 'open_id'}
            if page:
                params['page_token'] = page
            response = await self.client.post(path, params=params,
                headers={'Authorization': 'Bearer ' + await self.token()},
                json=body)
            # Retry an explicitly expired token once; never retry permission failures.
            if response.status_code == 401:
                await self.store.delete(self.token_key)
                response = await self.client.post(path, params=params,
                    headers={'Authorization': 'Bearer ' + await self.token()},
                    json=body)
            data = self.payload(response).get('data', {})
            rows = data.get('items')
            if not isinstance(rows, list):
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            items.extend(rows)
            if not data.get('has_more'):
                return items
            page = data.get('page_token')
            if not page or page in seen:
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            seen.add(page)
        raise AppError(ErrorCode.UPSTREAM_ERROR)

    async def download(self, attachment):
        token = attachment['file_token']
        # Reconstruct an official API URL. Never fetch a URL supplied by a table cell.
        url = 'https://open.feishu.cn/open-apis/drive/v1/medias/' + quote(token, safe='') + '/download'
        params = {}
        extra = parse_qs(urlsplit(attachment.get('url', '')).query).get('extra')
        if extra:
            params['extra'] = extra[0]
        async with self.client.stream('GET', url, params=params,
                headers={'Authorization': 'Bearer ' + await self.token()}, timeout=30) as response:
            if not response.is_success:
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.settings.media_max_bytes:
                    raise AppError(ErrorCode.PAYLOAD_TOO_LARGE)
                chunks.append(chunk)
            return b''.join(chunks)
