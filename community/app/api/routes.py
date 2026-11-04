import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.api.dependencies import AdminUser, CurrentUser, current_user
from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.services.browser_auth import SESSION_COOKIE, session_token, set_session, validate_origin
from app.schemas.auth import FeishuLoginInput, LoginResult, LogoutResult, Principal
from app.schemas.common import Envelope, success
from app.schemas.content import ContentSnapshot

from app.api.error_docs import error_responses

ERROR_RESPONSES = error_responses()
router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)
health = APIRouter(prefix="/health", tags=["健康检查"], responses=ERROR_RESPONSES)
login_key = APIKeyHeader(name="X-Dev-Login-Key", auto_error=False)


class HealthResult(BaseModel):
    status: str
    store: str | None = None


class SyncStatus(BaseModel):
    snapshot_ready: bool
    version: str | None = None
    synced_at: str | None = None
    worker_connected: bool = False


@health.get("/live", response_model=Envelope[HealthResult])
async def live(request: Request):
    return success(request, HealthResult(status="alive"))


@health.get("/ready", response_model=Envelope[HealthResult])
async def ready(request: Request):
    if not await request.app.state.store.ping():
        raise AppError(ErrorCode.CACHE_UNAVAILABLE)
    return success(request, HealthResult(status="ready", store=request.app.state.settings.store_backend))


@router.post("/auth/dev", tags=["认证"], response_model=Envelope[LoginResult])
async def development_login(request: Request, response: Response, key: Annotated[str | None, Security(login_key)]):
    settings = request.app.state.settings
    validate_origin(request, required=False)
    if not settings.dev_login_enabled:
        raise AppError(ErrorCode.NOT_FOUND)
    if key is None or not secrets.compare_digest(key.encode(), settings.dev_login_key.get_secret_value().encode()):
        raise AppError(ErrorCode.DEV_LOGIN_DENIED)
    # Fixed server-owned identity. Clients cannot choose user IDs or roles.
    user = Principal(user_id="dev-member", tenant_id="development", display_name="本地开发用户",
                     identity_source="development", roles=["member"])
    result = await request.app.state.auth.issue(user)
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        await request.app.state.auth.revoke(old)
    set_session(response, request, result.access_token)
    return success(request, result)


@router.post("/auth/feishu", tags=["认证"], response_model=Envelope[LoginResult])
async def feishu_login(request: Request, body: FeishuLoginInput):
    # Raw codes without a browser-bound OAuth transaction must never create sessions.
    raise AppError(ErrorCode.OAUTH_STATE_INVALID)


@router.post("/auth/logout", tags=["认证"], response_model=Envelope[LogoutResult])
async def logout(request: Request, response: Response, user: CurrentUser):
    token, _ = session_token(request)
    await request.app.state.auth.revoke(token)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True,
                           secure=request.app.state.settings.cookie_secure, samesite="lax")
    return success(request, LogoutResult())


# All business routes require identity, including new routes added to this router.
protected = APIRouter(dependencies=[Depends(current_user)])


@protected.get("/me", tags=["个人信息"], response_model=Envelope[Principal])
async def me(request: Request, user: CurrentUser):
    return success(request, user)


@protected.get("/content/snapshot", tags=["内容"], response_model=Envelope[ContentSnapshot])
async def content(request: Request):
    return success(request, await request.app.state.snapshots.read())


@protected.get("/admin/sync/status", tags=["管理"], response_model=Envelope[SyncStatus])
async def sync_status(request: Request, admin: AdminUser):
    try:
        snapshot = await request.app.state.snapshots.read()
    except AppError as exc:
        if exc.code != ErrorCode.SNAPSHOT_NOT_READY:
            raise
        return success(request, SyncStatus(snapshot_ready=False))
    return success(request, SyncStatus(snapshot_ready=True, version=snapshot.version,
                                      synced_at=snapshot.synced_at.isoformat()))


router.include_router(protected)
