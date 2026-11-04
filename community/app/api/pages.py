"""Serve Vue build artifacts; protect business HTML, bundles and private media."""
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, RedirectResponse

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.services.browser_auth import browser_principal, safe_next

pages = APIRouter(include_in_schema=False)
FRONTEND = PROJECT_ROOT / "frontend" / "dist"
PUBLIC = {"login.html", "assets/ows-community-logo.png", "assets/icons/lucide.svg"}
EXTENSIONS = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}
PAGE_NAMES = ("index", "cases", "news", "skills", "services", "profile", "detail", "officers", "deploy", "login")


def public_assets() -> set[str]:
    """Only the login entry's static dependency closure is public, never dynamic pages."""
    allowed = set(PUBLIC)
    try:
        manifest = json.loads((FRONTEND / ".vite/manifest.json").read_text())
        pending, visited = ["login.html"], set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            entry = manifest[key]
            allowed.add(entry["file"])
            allowed.update(entry.get("css", []))
            allowed.update(entry.get("assets", []))
            pending.extend(entry.get("imports", []))
    except (OSError, ValueError, KeyError):
        pass  # Incomplete builds never make business assets public.
    return allowed


async def frontend(request: Request):
    path = request.url.path.lstrip("/") or "index.html"
    target = (FRONTEND / path).resolve()
    if (not target.is_relative_to(FRONTEND) or any(p.startswith(".") for p in Path(path).parts)
            or not target.is_file() or target.suffix not in EXTENSIONS):
        raise AppError(ErrorCode.NOT_FOUND)
    if path not in public_assets():
        try:
            await browser_principal(request)
        except AppError as exc:
            if exc.code != ErrorCode.UNAUTHORIZED:
                raise
            if target.suffix == ".html":
                dest = safe_next(request.url.path + ("?" + request.url.query if request.url.query else ""))
                return RedirectResponse("/login.html?" + urlencode({"next": dest, "auto": "1"}), 303)
            raise
    return FileResponse(target, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"})


for route_path in ["/", "/assets/{path:path}", *(f"/{name}.html" for name in PAGE_NAMES)]:
    pages.add_api_route(route_path, frontend, methods=["GET", "HEAD"])
