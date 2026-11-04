"""Release configuration checks: uv run python -m scripts.check_server [--executor]."""

import argparse
import json
import os
import sys
from html.parser import HTMLParser

from redis import Redis

from app.core.config import PROJECT_ROOT, Settings
from app.deployment.service import DeploymentService


class BuildAssets(HTMLParser):
    """Vite may deduplicate HTML entries into shared chunks in its manifest."""

    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = (
            attrs.get("src") if tag == "script"
            else attrs.get("href") if tag == "link"
            else None
        )
        if path and path.startswith("/assets/"):
            self.paths.append(path.lstrip("/"))


def check(settings, *, executor=False):
    if settings.environment != "production":
        raise ValueError("必须使用 production 配置运行上线检查")
    if not settings.feishu_app_id or not settings.feishu_app_secret.get_secret_value():
        raise ValueError("缺少飞书登录应用配置")
    origin_host = settings.site_origin.split("://", 1)[1].split(":", 1)[0]
    if origin_host not in settings.allowed_hosts:
        raise ValueError("正式域名必须包含在 ALLOWED_HOSTS 中")
    if not executor:
        build = PROJECT_ROOT / "frontend/dist"
        manifest = json.loads((build / ".vite/manifest.json").read_text())
        for name in (
            "index",
            "cases",
            "news",
            "skills",
            "services",
            "deploy",
            "profile",
            "detail",
            "officers",
            "login",
        ):
            document = build / (name + ".html")
            assets = BuildAssets()
            assets.feed(document.read_text())
            if not assets.paths or any(
                not (build / path).is_file() for path in assets.paths
            ):
                raise ValueError("前端构建产物不完整")
        for entry in manifest.values():
            if not (build / entry["file"]).is_file():
                raise ValueError("前端构建产物不完整")
    with Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=3,
        socket_connect_timeout=3,
    ) as client:
        client.ping()
    if settings.deploy_enabled:
        service = DeploymentService(settings)
        with service.store._connection() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("部署数据库完整性检查未通过")
        if executor:
            if (
                sys.platform != "linux"
                or not settings.deploy_execution_enabled
                or not settings.deploy_engine_config
            ):
                raise ValueError(
                    "执行器需要 Linux、真实引擎配置及 EXECUTION_ENABLED=true"
                )
            from awesome_deploy.runtime import DeploymentBackend

            DeploymentBackend(service.engine).check()
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1048576
            if memory < service.engine.max_memory_mib * 2 + 2048:
                raise ValueError("构建、应用预算和社区保留内存不足")
    elif executor:
        raise ValueError("部署模块未开启")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", action="store_true")
    args = parser.parse_args()
    try:
        check(Settings(), executor=args.executor)
    except Exception as exc:  # noqa: BLE001 — CLI boundary must not print credential-bearing exceptions
        # Configuration errors may include submitted secrets; output type only.
        print(
            f"FAIL: {type(exc).__name__}; check production configuration and dependencies.",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS: executor preflight"
        if args.executor
        else "PASS: community server preflight"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
