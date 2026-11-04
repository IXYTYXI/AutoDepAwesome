import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OWS_COMMUNITY_", env_file=Path(os.environ.get("OWS_COMMUNITY_SETTINGS_FILE", PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8", extra="ignore",
    )
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    frontend_port: int = Field(default=8765, ge=1, le=65535)
    store_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    redis_prefix: str = Field(default="ows-community", pattern=r"^[a-zA-Z0-9_-]+$")
    cors_origins: list[str] = ["http://127.0.0.1:8765", "http://localhost:8765"]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    docs_enabled: bool = True
    dev_login_enabled: bool = False
    dev_login_key: SecretStr = SecretStr("")
    session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    max_body_bytes: int = Field(default=2097152, ge=1024, le=10485760)
    site_origin: str = "http://127.0.0.1:8765"
    cookie_secure: bool = False
    feishu_allowed_tenants: list[str] = []
    feishu_profile_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_bitable_app_token: SecretStr = SecretStr("")
    officers_table_id: str = ""
    officers_view_id: str = ""
    officers_cache_seconds: int = Field(default=300, ge=10, le=86400)
    officers_stale_seconds: int = Field(default=604800, ge=60, le=2592000)
    officers_source_tenant: str = ""
    cases_table_id: str = ""
    cases_view_id: str = ""
    cases_cache_seconds: int = Field(default=1800, ge=10, le=86400)
    cases_stale_seconds: int = Field(default=604800, ge=60, le=2592000)
    cases_source_tenant: str = ""
    banner_table_id: str = ""
    banner_view_id: str = ""
    banner_cache_seconds: int = Field(default=86400, ge=10, le=86400)
    banner_stale_seconds: int = Field(default=604800, ge=60, le=2592000)
    banner_source_tenant: str = ""
    faq_table_id: str = ""
    faq_view_id: str = ""
    faq_cache_seconds: int = Field(default=600, ge=10, le=86400)
    faq_stale_seconds: int = Field(default=86400, ge=60, le=2592000)
    faq_source_tenant: str = ""
    news_table_id: str = ""
    news_view_id: str = ""
    news_cache_seconds: int = Field(default=600, ge=10, le=86400)
    news_stale_seconds: int = Field(default=86400, ge=60, le=2592000)
    news_source_tenant: str = ""
    news_sync_enabled: bool = True
    news_sync_interval_seconds: int = Field(default=600, ge=60, le=86400)
    news_sync_state_root: Path = PROJECT_ROOT / "var/news-sync"
    media_max_bytes: int = Field(default=20971520, ge=1024, le=52428800)
    media_root: Path = PROJECT_ROOT / "var/media"

    assistant_enabled: bool = False
    assistant_agent_id: str = "agent_4mdqh9jvd7kfnnn"
    assistant_source_tenant: str = ""
    assistant_feedback_base: SecretStr = SecretStr("")
    assistant_feedback_table: str = ""

    deploy_enabled: bool = True
    deploy_execution_enabled: bool = False
    deploy_sync_enabled: bool = True
    deploy_state_root: Path = PROJECT_ROOT / "var/deployment"
    deploy_engine_config: Path | None = None
    deploy_database_gid: int | None = Field(default=None, ge=0)
    deploy_tenant_id: str = ""
    deploy_allowed_users: list[str] = []
    deploy_apps_table_id: str = ""
    deploy_jobs_table_id: str = ""

    @property
    def feishu_redirect_uri(self) -> str:
        return self.site_origin + "/api/v1/auth/feishu/callback"

    @model_validator(mode="after")
    def check_deployment(self):
        if self.assistant_enabled and (
            self.store_backend != 'redis' or not self.assistant_agent_id
            or not self.assistant_feedback_base.get_secret_value() or not self.assistant_feedback_table
            or not self.assistant_source_tenant
        ):
            raise ValueError('社区助手必须配置 Redis、Agent、反馈表和来源租户')
        if self.faq_stale_seconds <= self.faq_cache_seconds:
            raise ValueError("faq_stale_seconds 必须大于 faq_cache_seconds")
        if self.news_stale_seconds <= self.news_cache_seconds:
            raise ValueError("news_stale_seconds 必须大于 news_cache_seconds")
        if self.news_sync_enabled and (self.store_backend != 'redis' or not self.news_table_id or not self.news_view_id):
            raise ValueError("资讯定时同步必须配置 Redis 和飞书目标表")
        if not self.news_sync_state_root.is_absolute():
            self.news_sync_state_root = PROJECT_ROOT / self.news_sync_state_root
        if self.banner_stale_seconds <= self.banner_cache_seconds:
            raise ValueError("banner_stale_seconds 必须大于 banner_cache_seconds")
        if self.cases_stale_seconds <= self.cases_cache_seconds:
            raise ValueError("cases_stale_seconds 必须大于 cases_cache_seconds")
        if self.officers_stale_seconds <= self.officers_cache_seconds:
            raise ValueError("officers_stale_seconds 必须大于 officers_cache_seconds")
        origin = urlsplit(self.site_origin)
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username or origin.password:
            raise ValueError("site_origin 必须是明确的 HTTP/HTTPS 来源，不含路径")
        if self.environment == "production" and (origin.scheme != "https" or not self.cookie_secure):
            raise ValueError("生产环境必须启用 HTTPS 和 Secure Cookie")
        if self.dev_login_enabled and len(self.dev_login_key.get_secret_value()) < 24:
            raise ValueError("开发登录需要至少 24 字符的 OWS_COMMUNITY_DEV_LOGIN_KEY")
        if self.environment == "production":
            if self.dev_login_enabled or self.store_backend != "redis":
                raise ValueError("生产环境必须关闭开发登录并使用 Redis")
            if self.docs_enabled:
                raise ValueError("生产环境请显式关闭公开 API 文档")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("必须配置明确的 allowed_hosts")
        if any("*" in origin for origin in self.cors_origins):
            raise ValueError("CORS 必须配置明确来源")
        if not self.deploy_state_root.is_absolute():
            self.deploy_state_root = PROJECT_ROOT / self.deploy_state_root
        if self.deploy_engine_config and not self.deploy_engine_config.is_absolute():
            self.deploy_engine_config = PROJECT_ROOT / self.deploy_engine_config
        if not self.media_root.is_absolute():
            self.media_root = PROJECT_ROOT / self.media_root
        return self
