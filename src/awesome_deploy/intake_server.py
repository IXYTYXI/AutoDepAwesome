"""Loopback-only HTTP intake, intended to run on the caller's backend host."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request
import uvicorn

from .config import Settings
from .feishu import _read_secret
from .intake_relay import IntakeError, IntakeRelay
from .intake_remote import BaseIntake, PlatformClient

LOG = logging.getLogger(__name__)


async def _drain(relay):
    while True:
        try:
            await asyncio.to_thread(relay.run_once)
        except Exception as exc:
            LOG.error('接入服务后台处理失败：%s', type(exc).__name__)
        await asyncio.sleep(5)


def create_app(relay, intake_token, *, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            try:
                await asyncio.to_thread(relay.restore_from_base)
            except Exception:
                pass  # Existing local records remain authoritative while Base is unavailable.
            task = asyncio.create_task(_drain(relay))
        else:
            task = None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title='Awesome Deploy independent intake', lifespan=lifespan)

    def require_token(request):
        values = request.headers.getlist('authorization')
        if (len(values) != 1 or not values[0].startswith('Bearer ')
                or not values[0].isascii()
                or not hmac.compare_digest(values[0][7:], intake_token)
                or request.headers.get('origin') is not None
                or request.headers.get('sec-fetch-site') is not None):
            raise HTTPException(401, '接入凭证无效。')

    @app.post('/requests', status_code=202)
    async def submit(request: Request):
        require_token(request)
        request_id = request.headers.get('idempotency-key', '')
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 8192:
                raise HTTPException(413, '请求内容过大。')
        try:
            body = json.loads(chunks)
            return await asyncio.to_thread(relay.accept, request_id, body)
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, IntakeError) and '相同请求 ID' in str(exc):
                raise HTTPException(409, str(exc)) from None
            raise HTTPException(400, str(exc) if isinstance(exc, IntakeError) else '请求 JSON 无效。') from None

    @app.get('/requests/{request_id}')
    def get(request_id: str, request: Request):
        require_token(request)
        result = relay.get(request_id)
        if result is None:
            raise HTTPException(404, '请求不存在。')
        return result

    return app


def load_relay(config_path):
    values = json.loads(Path(config_path).read_text())
    required = {'database_path', 'platform_origin', 'platform_token_file', 'intake_token_file',
                'integrity_key_file', 'feishu_app_id', 'feishu_app_secret_file', 'base_token',
                'intake_table_id'}
    if not isinstance(values, dict) or set(values) != required:
        raise ValueError('接入服务配置字段错误。')
    for key in ('database_path', 'platform_token_file', 'intake_token_file',
                'integrity_key_file', 'feishu_app_secret_file'):
        if not isinstance(values[key], str) or not Path(values[key]).is_absolute():
            raise ValueError(f'{key} 必须是绝对路径。')
    if not isinstance(values['feishu_app_id'], str) or not re.fullmatch(
            r'cli_[A-Za-z0-9]{1,80}', values['feishu_app_id']):
        raise ValueError('飞书 App ID 无效。')
    token = _read_secret(Path(values['intake_token_file']))
    key = _read_secret(Path(values['integrity_key_file'])).encode()
    settings = Settings(feishu_app_id=values['feishu_app_id'],
                        feishu_app_secret_file=Path(values['feishu_app_secret_file']))
    platform = PlatformClient(values['platform_origin'], Path(values['platform_token_file']))
    base = BaseIntake(settings, values['base_token'], values['intake_table_id'])
    relay = IntakeRelay(Path(values['database_path']), platform, base, integrity_key=key)
    return relay, token


def main():
    parser = argparse.ArgumentParser(description='独立请求登记与投递服务；只监听本机。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()
    relay, token = load_relay(args.config)
    uvicorn.run(create_app(relay, token), host='127.0.0.1', port=args.port, workers=1)
