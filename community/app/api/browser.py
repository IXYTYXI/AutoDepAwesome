from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from redis.exceptions import RedisError
from starlette.responses import RedirectResponse

from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.schemas.common import success
from app.services.browser_auth import (FLOW_COOKIE, FLOW_PATH, SESSION_COOKIE, begin_flow,
    browser_principal, clear_flow, consume_flow, csrf_token, safe_next, session_token, set_session)

browser = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@browser.get("/options")
async def options(request: Request):
    settings = request.app.state.settings
    return success(request, {"feishu_enabled": bool(settings.feishu_app_id and settings.feishu_app_secret.get_secret_value()),
                             "dev_enabled": settings.dev_login_enabled})


@browser.get("/session")
async def session(request: Request):
    user = await browser_principal(request)
    token, _ = session_token(request)
    return success(request, {"user": user.model_dump(), "csrf_token": csrf_token(token)})


@browser.get("/feishu/start")
async def start(request: Request, next: str = "/index.html"):
    try:
        url, binding = await begin_flow(request, next)
    except AppError as exc:
        return RedirectResponse("/login.html?" + urlencode({"error": exc.code.value, "next": safe_next(next)}), 303)
    response = RedirectResponse(url, 303)
    response.set_cookie(FLOW_COOKIE, binding, max_age=300, httponly=True,
                        secure=request.app.state.settings.cookie_secure, samesite="lax", path=FLOW_PATH)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@browser.get("/feishu/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    next_page = "/index.html"
    try:
        flow = await consume_flow(request, state)
        next_page = flow["next"]
        if error:
            raise AppError(ErrorCode.FEISHU_AUTH_FAILED)
        if not code or len(code) > 2048:
            raise AppError(ErrorCode.FEISHU_AUTH_FAILED)
        user = await request.app.state.feishu.exchange_code(code, flow["verifier"])
        # Rotate any pre-existing local session on a successful login.
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            await request.app.state.auth.revoke(old)
        result = await request.app.state.auth.issue(user)
        response = RedirectResponse(next_page, 303)
        set_session(response, request, result.access_token)
    except (AppError, httpx.HTTPError, RedisError) as exc:
        failure = exc.code.value if isinstance(exc, AppError) else "SERVICE_UNAVAILABLE"
        response = RedirectResponse("/login.html?" + urlencode({"error": failure, "next": next_page}), 303)
    clear_flow(response, request)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
