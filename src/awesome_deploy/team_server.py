"""Loopback adapter for A implementations in any language; A owns browser auth."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
import httpx
import uvicorn

from .feishu import FeishuUnavailable, _read_secret
from .intake_remote import PlatformClient
from .team_outbox import TeamOutbox
from .team_sessions import SessionError, SessionUnavailable, load_shared_sessions
from .team_api import TeamAppSearch


class TeamSearchError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('项目查询未完成。')


class TeamPlatformClient(PlatformClient):
    def __init__(self, origin, token_file, *, host=None, client=None):
        parts = urlsplit(origin)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in ('', '/')
                or parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', '::1', 'localhost')):
            raise ValueError('平台地址只允许本机 HTTP 或 HTTPS。')
        if host is not None and (not isinstance(host, str) or not host or any(c in host for c in '/\r\n :')):
            raise ValueError('Host 必须是纯域名。')
        self.origin, self.token_file, self.host = origin.rstrip('/'), token_file, host
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=3),
                                             trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def _call(self, method, path, **kwargs):
        if self.host:
            kwargs['headers'] = {**kwargs.get('headers', {}), 'Host': self.host}
        return super()._call(method, path, **kwargs)

    def post(self, request_id, body):
        from .intake_remote import PlatformRejected
        try:
            return self._call('POST', '/api/v1/integrations/deployments', json=body,
                             headers={'Idempotency-Key': request_id, 'Content-Type': 'application/json'})
        except ConnectionError as exc:
            # Local permission denial is definitive; 401 remains retryable for credential maintenance.
            if str(exc) == '平台 HTTP 403':
                raise PlatformRejected(403) from None
            raise

    def get(self, job_id):
        from .team_sessions import request_key
        request_key(job_id)
        return self._call('GET', '/api/v1/integrations/deployments/' + job_id)

    def search(self, body):
        from .intake_remote import PlatformRejected
        try:
            return self._call('POST', '/api/v1/integrations/apps/search', json=body,
                             headers={'Content-Type': 'application/json'})
        except PlatformRejected as exc:
            raise TeamSearchError(exc.status_code) from None
        except ConnectionError as exc:
            code = {'平台 HTTP 401': 401, '平台 HTTP 403': 403}.get(str(exc), 503)
            raise TeamSearchError(code) from None
        except (OSError, ValueError, FeishuUnavailable):
            raise TeamSearchError(503) from None


def create_team_app(outbox, token, *, start_worker=True):
    async def drain():
        while True:
            try:
                # Drain bursts without making all requests wait five seconds each.
                for _ in range(32):
                    if not await asyncio.to_thread(outbox.run_once):
                        break
            except Exception:
                import logging
                logging.getLogger(__name__).error('同组请求后台处理失败；已保存请求待恢复。')
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(drain()) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title='Awesome Deploy team intake', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(request):
        values = request.headers.getlist('authorization')
        if (len(values) != 1 or not values[0].startswith('Bearer ')
                or not values[0].isascii()
                or not hmac.compare_digest(values[0][7:], token)
                or request.headers.get('origin') is not None or request.headers.get('sec-fetch-site') is not None):
            raise HTTPException(401, '仅允许已认证的 A 后端调用。')

    async def session_body(request):
        authenticate(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, '请求过大。')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
            return body
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'JSON 无效。') from None

    @app.post('/sessions', status_code=201)
    async def issue_session(request: Request):
        body = await session_body(request)
        if set(body) - {'identity', 'ttl'} or 'identity' not in body:
            raise HTTPException(400, '会话字段无效。')
        try:
            sid = await asyncio.to_thread(outbox.sessions.issue, body['identity'], ttl=body.get('ttl', 28800))
            return {'sessionId': sid, 'expiresIn': body.get('ttl', 28800)}
        except SessionError:
            raise HTTPException(400, '共享身份或有效期无效。') from None
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用。') from None

    @app.post('/sessions/revoke')
    async def revoke_session(request: Request):
        body = await session_body(request)
        if set(body) != {'sessionId'}:
            raise HTTPException(400, '会话字段无效。')
        try:
            await asyncio.to_thread(outbox.sessions.logout, body['sessionId'])
            return {'revoked': True}
        except SessionError:
            raise HTTPException(400, '会话标识无效。') from None
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用，不能确认退出。') from None

    @app.post('/requests', status_code=202)
    async def submit(request: Request):
        authenticate(request)
        if len(request.headers.getlist('idempotency-key')) != 1:
            raise HTTPException(400, '必须提供唯一 Idempotency-Key。')
        if not request.headers.get('content-type', '').lower().startswith('application/json'):
            raise HTTPException(415, '请使用 application/json。')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise HTTPException(413, '请求过大。')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {'deployment', 'sessionId'}:
                raise HTTPException(400, '请求只能包含 deployment 和 sessionId。')
            return await asyncio.to_thread(outbox.accept, request.headers['idempotency-key'],
                                           body['deployment'], body['sessionId'])
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用，本次申请未受理。') from None
        except SessionError as exc:
            raise HTTPException(409 if '相同请求 ID' in str(exc) else 401, str(exc)) from None
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'JSON 无效。') from None

    @app.post('/apps/search')
    async def search_apps(request: Request):
        body = await session_body(request)
        if not request.headers.get('content-type', '').lower().startswith('application/json'):
            raise HTTPException(415, '请使用 application/json。')
        from pydantic import ValidationError
        try:
            query = TeamAppSearch.model_validate(body)
        except ValidationError:
            raise HTTPException(422, '项目查询字段无效，请检查会话、关键词和分页参数。') from None
        try:
            await asyncio.to_thread(outbox.sessions.read, query.sessionId)
            return await asyncio.to_thread(outbox.platform.search, query.model_dump())
        except SessionError:
            raise HTTPException(401, '共享会话已失效，请重新登录。') from None
        except SessionUnavailable:
            raise HTTPException(503, '共享登录服务暂不可用，请稍后重试查询。') from None
        except TeamSearchError as exc:
            messages = {400: '项目查询请求无效。', 401: '共享会话已失效，或来源凭证需要维护。',
                        403: '当前用户或来源无权查询可部署项目。', 409: '项目查询暂不可用，请重试。',
                        422: '项目查询字段无效，请检查关键词和分页参数。'}
            raise HTTPException(exc.status_code, messages.get(exc.status_code,
                '部署平台暂不可用，请稍后重试查询。')) from None

    @app.get('/requests/{request_id}')
    def get(request_id: str, request: Request):
        authenticate(request)
        try:
            result = outbox.get(request_id)
        except SessionError:
            raise HTTPException(400, '请求 ID 无效。') from None
        if result is None:
            raise HTTPException(404, '请求不存在。')
        return result

    return app


def load_team_outbox(path):
    cfg = json.loads(Path(path).read_text())
    fields = {'source', 'shared_sessions_file', 'database_path', 'platform_origin',
              'platform_host', 'platform_token_file', 'intake_token_file'}
    if not isinstance(cfg, dict) or set(cfg) != fields:
        raise ValueError('同组接入配置字段无效。')
    for name in ('shared_sessions_file', 'database_path', 'platform_token_file', 'intake_token_file'):
        if not Path(cfg[name]).is_absolute():
            raise ValueError('配置路径必须为绝对路径。')
    sessions = load_shared_sessions(cfg['shared_sessions_file'])[cfg['source']]
    platform = TeamPlatformClient(cfg['platform_origin'], Path(cfg['platform_token_file']), host=cfg['platform_host'])
    return TeamOutbox(Path(cfg['database_path']), sessions, platform), _read_secret(Path(cfg['intake_token_file']))


def main():
    parser = argparse.ArgumentParser(description='同组后端可靠接入，仅监听本机。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()
    outbox, token = load_team_outbox(args.config)
    uvicorn.run(create_team_app(outbox, token), host='127.0.0.1', port=args.port, workers=1, access_log=False)
