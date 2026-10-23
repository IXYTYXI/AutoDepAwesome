"""Bounded HTTP adapters for the independent intake relay."""

import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
import uuid

import httpx

from .base_sync import BASE_ORIGIN, BaseRemote, BaseSyncError
from .feishu import _read_secret


class PlatformRejected(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'平台拒绝部署请求 (HTTP {status_code})')


class PlatformClient:
    def __init__(self, origin: str, token_file: Path, *, client=None):
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path not in ('', '/') or parts.query or parts.fragment):
            raise ValueError('平台地址必须是 HTTPS 站点根地址。')
        self.origin = origin.rstrip('/')
        self.token_file = token_file
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=5),
                                              trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def _call(self, method, path, **kwargs):
        token = _read_secret(self.token_file)
        headers = {'Authorization': f'Bearer {token}'}
        headers.update(kwargs.pop('headers', {}))
        try:
            with self.client.stream(method, self.origin + path, headers=headers,
                                    follow_redirects=False, **kwargs) as response:
                if response.is_redirect:
                    raise ConnectionError('平台发生意外跳转')
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 131072:
                        raise ConnectionError('平台响应过大')
                if response.status_code == 202 or response.status_code == 200:
                    value = json.loads(data)
                    if isinstance(value, dict):
                        return value
                if method == 'POST' and response.status_code in (400, 409, 422):
                    raise PlatformRejected(response.status_code)
                raise ConnectionError(f'平台 HTTP {response.status_code}')
        except (httpx.HTTPError, ValueError):
            raise ConnectionError('平台暂不可达或响应无效') from None

    def post(self, request_id, body):
        return self._call('POST', '/api/v1/automation/deployments', json=body,
                          headers={'Idempotency-Key': request_id, 'Content-Type': 'application/json'})

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', job_id):
            raise ValueError('任务 ID 无效')
        return self._call('GET', '/api/v1/automation/deployments/' + quote(job_id, safe=''))


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, dict) and isinstance(part.get('text'), str)
                                      for part in value):
        return ''.join(part['text'] for part in value)
    return None


class BaseIntake(BaseRemote):
    def __init__(self, settings, base_token, table_id, *, client=None):
        if not re.fullmatch(r'[A-Za-z0-9]{16,80}', base_token) or not re.fullmatch(
                r'tbl[A-Za-z0-9]{8,80}', table_id):
            raise ValueError('多维表格标识无效')
        super().__init__(settings, {'base_token': base_token}, client=client)
        self.path = f'{BASE_ORIGIN}/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records'

    def _search(self, request_id):
        result = self._request('POST', self.path + '/search', token=self._token(), json={
            'filter': {'conjunction': 'and', 'conditions': [
                {'field_name': '请求 ID', 'operator': 'is', 'value': [request_id]}]}, 'page_size': 2})
        items = (result.get('data') or {}).get('items') or []
        if not isinstance(items, list) or len(items) > 1:
            raise BaseSyncError('请求表存在重复请求 ID')
        return items[0].get('record_id') if items else None

    def upsert(self, request_id, fields):
        record_id = self._search(request_id)
        token = self._token()
        if record_id:
            self._request('PUT', self.path + '/' + quote(record_id, safe=''), token=token,
                          json={'fields': fields})
        else:
            client_token = str(uuid.uuid5(uuid.NAMESPACE_URL, 'awesome-intake:' + request_id))
            self._request('POST', self.path + '?client_token=' + client_token,
                          token=token, json={'fields': fields})

    def list_records(self):
        result = []
        page_token = None
        seen = set()
        while True:
            payload = {'page_size': 100}
            if page_token:
                payload['page_token'] = page_token
            response = self._request('POST', self.path + '/search', token=self._token(), json=payload)
            data = response.get('data') or {}
            items = data.get('items') or []
            if not isinstance(items, list):
                raise BaseSyncError('请求表返回格式无效')
            for item in items:
                fields = item.get('fields') if isinstance(item, dict) else None
                if isinstance(fields, dict):
                    result.append({key: _text(value) if key in (
                        '请求 ID', '请求内容', '完整性签名', '状态', '部署 ID', '应用 ID')
                                   else value for key, value in fields.items()})
            if not data.get('has_more'):
                return result
            page_token = data.get('page_token')
            if not isinstance(page_token, str) or not page_token or page_token in seen or len(seen) >= 1000:
                raise BaseSyncError('请求表分页标识无效')
            seen.add(page_token)
