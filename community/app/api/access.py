"""Default-deny authentication for every registered HTTP endpoint."""
from fastapi import Request

from app.api.pages import frontend
from app.services.browser_auth import browser_principal

# Match both method and path; never exempt an entire /auth or /health prefix.
PUBLIC_ENDPOINTS = frozenset({
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/api/v1/auth/options"),
    ("GET", "/api/v1/auth/feishu/start"),
    ("GET", "/api/v1/auth/feishu/callback"),
    ("POST", "/api/v1/auth/dev"),  # Separate development key, disabled in production.
    ("POST", "/api/v1/auth/feishu"),  # Legacy endpoint always rejects unbound codes.
})


async def require_identity_by_default(request: Request):
    # This exact handler owns the file allowlist and redirects anonymous HTML visits.
    # Exempting the handler (not its URL prefix) cannot expose new business routes.
    if request.scope.get("endpoint") is frontend:
        return
    if (request.method, request.url.path) in PUBLIC_ENDPOINTS:
        return
    await browser_principal(request)
