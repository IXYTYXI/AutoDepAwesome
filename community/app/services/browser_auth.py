"""Browser-bound, single-use OAuth transactions and first-party cookie sessions."""
import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from starlette.responses import Response

from app.core.error_codes import ErrorCode
from app.core.errors import AppError
from app.integrations.feishu import PROFILE_SCOPES

SESSION_COOKIE = "ows_session"
FLOW_COOKIE = "ows_oauth"
FLOW_PATH = "/api/v1/auth/feishu"
PAGES = {"/deploy.html", "/officers.html", "/index.html", "/cases.html", "/news.html", "/skills.html", "/services.html", "/profile.html", "/detail.html"}


def safe_next(value: str) -> str:
    if len(value) > 2048 or any(ord(c) < 32 for c in value) or "\\" in value:
        return "/index.html"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path not in PAGES:
        return "/index.html"
    return value


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"ows-community-csrf-v1", hashlib.sha256).hexdigest()


def validate_origin(request: Request, required: bool = True):
    origin = request.headers.get("origin")
    expected = request.app.state.settings.site_origin
    if (required or origin is not None) and origin != expected:
        raise AppError(ErrorCode.CSRF_FAILED)


def session_token(request: Request) -> tuple[str, bool]:
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AppError(ErrorCode.UNAUTHORIZED)
        return token, False
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise AppError(ErrorCode.UNAUTHORIZED)
    return token, True


async def browser_principal(request: Request):
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token, cookie = session_token(request)
    principal = await request.app.state.auth.authenticate(token)
    if cookie and request.method not in {"GET", "HEAD", "OPTIONS"}:
        validate_origin(request)
        provided = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(provided.encode(), csrf_token(token).encode()):
            raise AppError(ErrorCode.CSRF_FAILED)
    request.state.principal = principal
    return principal


def set_session(response: Response, request: Request, token: str):
    settings = request.app.state.settings
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.session_ttl_seconds,
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")


def clear_flow(response: Response, request: Request):
    response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH, httponly=True,
                           secure=request.app.state.settings.cookie_secure, samesite="lax")


async def begin_flow(request: Request, next_page: str):
    settings = request.app.state.settings
    request.app.state.feishu.check_config()
    state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    key = "oauth:" + hashlib.sha256(state.encode()).hexdigest()
    await request.app.state.store.set(key, json.dumps({
        "binding": hashlib.sha256(binding.encode()).hexdigest(),
        "next": safe_next(next_page), "verifier": verifier,
    }), ttl=300)
    query = {"client_id": settings.feishu_app_id, "response_type": "code",
             "redirect_uri": settings.feishu_redirect_uri, "state": state,
             "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
             "code_challenge_method": "S256"}
    if settings.feishu_profile_enabled:
        query["scope"] = " ".join(PROFILE_SCOPES)
    return "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(query), binding


async def consume_flow(request: Request, state: str):
    binding = request.cookies.get(FLOW_COOKIE, "")
    if not state or len(state) > 128 or not binding or len(binding) > 128:
        raise AppError(ErrorCode.OAUTH_STATE_INVALID)
    key = "oauth:" + hashlib.sha256(state.encode()).hexdigest()
    raw = await request.app.state.store.get(key)
    if raw is None:
        raise AppError(ErrorCode.OAUTH_STATE_INVALID)
    saved = json.loads(raw)
    if not secrets.compare_digest(saved["binding"], hashlib.sha256(binding.encode()).hexdigest()):
        raise AppError(ErrorCode.OAUTH_STATE_INVALID)
    # Atomic consume prevents two callbacks exchanging the same transaction.
    if await request.app.state.store.take(key) is None:
        raise AppError(ErrorCode.OAUTH_STATE_INVALID)
    return saved
