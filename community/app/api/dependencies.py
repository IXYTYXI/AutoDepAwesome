from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.schemas.auth import Principal
from app.services.auth import AuthService
from app.services.browser_auth import browser_principal

bearer = HTTPBearer(auto_error=False, description="通过开发登录或后续飞书登录获得本站会话令牌")


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    service: Annotated[AuthService, Depends(auth_service)],
) -> Principal:
    return await browser_principal(request)


def require_roles(*roles: str):
    async def check(user: Annotated[Principal, Depends(current_user)]) -> Principal:
        if not set(roles).intersection(user.roles):
            raise AppError(ErrorCode.FORBIDDEN)
        return user
    return check


CurrentUser = Annotated[Principal, Depends(current_user)]
AdminUser = Annotated[Principal, Depends(require_roles("admin"))]


def check_source_tenant(request: Request, user: Principal, resource: str) -> None:
    """Shared content is scoped to the configured source tenant, not a user's cache key."""
    tenant = getattr(request.app.state.settings, resource + "_source_tenant")
    if tenant and user.identity_source != "development" and tenant != user.tenant_id:
        raise AppError(ErrorCode.FORBIDDEN)
