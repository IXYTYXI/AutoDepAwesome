from app.api.assistant import router as assistant_router
from app.integrations.aily import AilyClient
from app.services.assistant import AssistantService
from app.api.deployment import router as deployment_router, deploy_error
from app.deployment.service import DeploymentService
from awesome_deploy.errors import DeployError
import asyncio
from contextlib import asynccontextmanager, suppress
from app.services.news import NewsService, NEWS_FIELDS
from app.services.news_sync import NewsSync
from app.integrations.news_table import NewsTable
from app.api.news import router as news_router
from app.api.faq import router as faq_router
from app.services.faq import FAQService, FAQ_FIELDS

import httpx
from fastapi import Depends, FastAPI
from redis.asyncio import Redis
from starlette.middleware.cors import CORSMiddleware

from app.api.routes import health, router
from app.api.access import require_identity_by_default
from app.api.browser import browser
from app.api.pages import pages
from app.api.officers import router as officers_router
from app.integrations.feishu_directory import FeishuDirectorySource
from app.services.officers import OfficerService
from app.services.cases import CaseService, CASE_FIELDS
from app.api.cases import router as cases_router
from app.api.banner import router as banner_router
from app.services.banner import BannerService, BANNER_FIELDS
from app.core.config import Settings
from app.core.errors import register_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestMiddleware
from app.integrations.feishu import FeishuIdentityProvider
from app.repositories.snapshots import SnapshotRepository
from app.repositories.store import MemoryStore, RedisStore, Store
from app.services.auth import AuthService


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = store is None
        selected = store
        if selected is None:
            if settings.store_backend == "redis":
                client = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True,
                                        socket_timeout=3, socket_connect_timeout=3,
                                        max_connections=30)
                selected = RedisStore(client, settings.redis_prefix)
            else:
                selected = MemoryStore()
        app.state.deployment = None
        if settings.deploy_enabled:
            try:
                app.state.deployment = await asyncio.to_thread(DeploymentService, settings)
            except Exception:
                import logging
                logging.getLogger("ows_community").error("deployment_initialization_failed")
                if settings.environment == "production":
                    if owned:
                        await selected.close()
                    raise
        app.state.settings = settings
        app.state.store = selected
        app.state.auth = AuthService(selected, settings.session_ttl_seconds)
        app.state.snapshots = SnapshotRepository(selected)
        # Shared transport; the optional synchronization task runs in the background.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3),
                                         limits=httpx.Limits(max_connections=20),
                                         follow_redirects=False) as client:
                app.state.feishu = FeishuIdentityProvider(client, settings)
                app.state.officers = OfficerService(selected, FeishuDirectorySource(client, settings, selected), settings)
                app.state.cases = CaseService(selected, FeishuDirectorySource(client, settings, selected,
                    table_id=settings.cases_table_id, view_id=settings.cases_view_id, fields=CASE_FIELDS), settings)
                app.state.banner = BannerService(selected, FeishuDirectorySource(client, settings, selected,
                    table_id=settings.banner_table_id, view_id=settings.banner_view_id, fields=BANNER_FIELDS, automatic_fields=True), settings)
                app.state.faq = FAQService(selected, FeishuDirectorySource(client, settings, selected,
                    table_id=settings.faq_table_id, view_id=settings.faq_view_id, fields=FAQ_FIELDS), settings)
                app.state.news = NewsService(selected, FeishuDirectorySource(client, settings, selected,
                    table_id=settings.news_table_id, view_id=settings.news_view_id, fields=NEWS_FIELDS), settings)
                app.state.news_sync = NewsSync(client, NewsTable(client, settings, selected,
                    table_id=settings.news_table_id, view_id=settings.news_view_id, fields=NEWS_FIELDS), app.state.news, settings)
                app.state.assistant = AssistantService(selected, AilyClient(client, settings, selected), settings, app.state.faq)
                task = asyncio.create_task(app.state.news_sync.run()) if settings.news_sync_enabled else None
                try:
                    yield
                finally:
                    if task:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
        finally:
            if owned:
                await selected.close()

    app = FastAPI(title="ows-community API", version="0.1.0", lifespan=lifespan,
                  dependencies=[Depends(require_identity_by_default)],
                  docs_url="/docs" if settings.docs_enabled else None,
                  redoc_url=None, openapi_url="/openapi.json" if settings.docs_enabled else None,
                  description="飞书 OAuth 登录、个人资料、AI 领航团目录与鉴权图片；Redis 优先缓存。")
    register_handlers(app)
    app.add_exception_handler(DeployError, deploy_error)
    app.include_router(assistant_router)
    app.include_router(deployment_router)
    app.include_router(health)
    app.include_router(router)
    app.include_router(browser)
    app.include_router(officers_router)
    app.include_router(cases_router)
    app.include_router(news_router)
    app.include_router(faq_router)
    app.include_router(banner_router)
    app.include_router(pages)
    app.add_middleware(RequestMiddleware, max_body_bytes=settings.max_body_bytes,
                       allowed_hosts=settings.allowed_hosts)
    # Last added middleware is outermost; CORS headers also wrap normalized 500s.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Dev-Login-Key", "X-CSRF-Token", "Idempotency-Key"],
                       expose_headers=["X-Request-ID", "Server-Timing"], max_age=600)
    return app


app = create_app()
