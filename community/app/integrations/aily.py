"""Aily application API; credentials and remote session IDs stay on the server."""
import asyncio
import logging
from urllib.parse import quote
from app.core.error_codes import ErrorCode
from app.core.errors import AppError
from app.integrations.bitable_writer import BitableWriter

logger = logging.getLogger('ows_community')


class AilyClient:
    def __init__(self, client, settings, store):
        self.client, self.settings, self.store = client, settings, store
        self.lock = asyncio.Lock()
        self.token_key = 'feishu:aily-token:' + settings.feishu_app_id
        self.root = 'https://open.feishu.cn/open-apis/aily/v1/agents/' + quote(settings.assistant_agent_id, safe='')
        self.writer = BitableWriter(client, settings, store)

    @staticmethod
    def payload(response):
        try:
            data = response.json()
        except ValueError:
            raise AppError(ErrorCode.UPSTREAM_ERROR) from None
        if not response.is_success or data.get('code', 0) != 0 or data.get('error'):
            logger.warning('assistant_upstream_failed', extra={'status': response.status_code, 'upstream_code': data.get('code')})
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        return data

    async def token(self):
        async with self.lock:
            cached = await self.store.get(self.token_key)
            if cached:
                return cached
            r = await self.client.post('https://accounts.feishu.cn/oauth/v3/token', data={
                'grant_type': 'client_credentials', 'client_id': self.settings.feishu_app_id,
                'client_secret': self.settings.feishu_app_secret.get_secret_value(),
                'scope': 'aily:agent_chat:read aily:agent_chat:write',
            })
            data = self.payload(r)
            token = data.get('access_token')
            if not token:
                raise AppError(ErrorCode.UPSTREAM_ERROR)
            await self.store.set(self.token_key, token, ttl=max(1, int(data['expires_in']) - 120))
            return token

    async def request(self, method, path, **kwargs):
        r = await self.client.request(method, self.root + path,
            headers={'Authorization': 'Bearer ' + await self.token()}, **kwargs)
        return self.payload(r)['data']

    async def start(self, message, session_id=None):
        body = {'user_message': {'content': [{'type': 'text', 'text': message}]}, 'stream': False}
        if session_id:
            body['session_id'] = session_id
        return await self.request('POST', '/chats', json=body)

    async def result(self, chat_id):
        return await self.request('GET', '/chats/' + quote(chat_id, safe=''))

    async def ticket(self, ticket_id, fields):
        s = self.settings
        base = quote(s.assistant_feedback_base.get_secret_value(), safe='')
        table = quote(s.assistant_feedback_table, safe='')
        r = await self.client.post(f'https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records',
            params={'client_token': ticket_id}, json={'fields': fields},
            headers={'Authorization': 'Bearer ' + await self.writer.token()})
        return self.payload(r)['data']['record']['record_id']
