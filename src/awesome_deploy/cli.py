"""Operator commands. Secrets are prompted for, never accepted as CLI arguments."""

import argparse
import dataclasses
import getpass
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from .auth import initialize_credentials
from .config import Settings
from .errors import DeployError


def load_settings(path: Path | None) -> Settings:
    values = {}
    base = Path.cwd()
    if path:
        path = path.resolve()
        base = path.parent
        try:
            values = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise DeployError("INVALID_CONFIG", "无法读取运维配置 JSON。") from exc
    if not isinstance(values, dict) or set(values) - {f.name for f in dataclasses.fields(Settings)}:
        raise DeployError("INVALID_CONFIG", "运维配置包含未知字段或不是对象。")
    for key in ("state_dir", "auth_file", "rootlesskit_socket"):
        if key in values and values[key] is not None:
            try:
                candidate = Path(values[key]).expanduser()
            except TypeError as exc:
                raise DeployError("INVALID_CONFIG", f"{key} 必须是路径。") from exc
            values[key] = (base / candidate).resolve() if not candidate.is_absolute() else candidate
    if "state_dir" not in values:
        values["state_dir"] = base / ".state"
    for key in ("git_hosts", "gateway_command", "reserved_names", "banned_words"):
        if key in values:
            if (
                not isinstance(values[key], list)
                or not values[key]
                or not all(isinstance(v, str) for v in values[key])
            ):
                raise DeployError("INVALID_CONFIG", f"{key} 必须是非空字符串数组。")
            values[key] = tuple(values[key])
    if not values.get("auth_file"):
        values["auth_file"] = values["state_dir"] / "admin.json"
    try:
        settings = Settings(**values)
        integers = (
            "listen_port",
            "port_start",
            "port_end",
            "max_memory_mib",
            "build_timeout",
            "health_timeout",
            "git_timeout",
            "repo_size_limit_mib",
            "required_free_mib",
            "docker_storage_limit_mib",
        )
        if any(type(getattr(settings, k)) is not int or getattr(settings, k) <= 0 for k in integers):
            raise ValueError("limits")
        if (
            isinstance(settings.max_cpu, bool)
            or not isinstance(settings.max_cpu, (int, float))
            or not math.isfinite(settings.max_cpu)
            or settings.max_cpu <= 0
        ):
            raise ValueError("CPU limit")
        if not 1 <= settings.port_start <= settings.port_end <= 65535 or settings.listen_port > 65535:
            raise ValueError("ports")
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", settings.base_domain):
            raise ValueError("domain")
        if settings.build_network != "none":
            raise ValueError("build network")
        public = urlsplit(settings.public_url)
        if not public.hostname or (public.port is not None and not 1 <= public.port <= 65535):
            raise ValueError("public URL host and port")
        if (
            public.username
            or public.password
            or public.query
            or public.fragment
            or public.path not in ("", "/")
        ):
            raise ValueError("public URL")
        if public.scheme != "https" and not (
            public.scheme == "http" and public.hostname in ("127.0.0.1", "localhost")
        ):
            raise ValueError("HTTP only allowed for loopback")
        if not isinstance(settings.docker_host, str) or not settings.docker_host.startswith("unix:///"):
            raise ValueError("local rootless Docker socket required")
    except (TypeError, ValueError) as exc:
        raise DeployError("INVALID_CONFIG", "运维配置中的域名、URL、端口、资源限制或构建网络无效。") from exc
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Awesome 仓库到 HTTPS 部署平台")
    parser.add_argument("--config", type=Path, help="运维 JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="交互初始化本地运维账号")
    sub.add_parser("serve", help="仅在 127.0.0.1 启动网页/API")
    worker = sub.add_parser("worker", help="运行持久化部署执行器")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("check", help="只读检查服务器部署前提")
    secret = sub.add_parser("secret-set", help="安全输入应用秘密配置")
    secret.add_argument("--repository", required=True)
    secret.add_argument("--subdomain", required=True)
    secret.add_argument("--name", required=True)
    schema = sub.add_parser("schema", help="输出当前支持的 JSON Schema")
    schema.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        os.umask(0o077)
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if settings.state_dir.is_symlink() or settings.state_dir.stat().st_mode & 0o077:
            raise DeployError("UNSAFE_STATE_DIRECTORY", "状态目录必须仅允许运维服务账号访问（0700）。")
        if args.command == "init":
            password = getpass.getpass("设置运维密码（至少 12 位）：")
            if getpass.getpass("再次输入：") != password:
                raise DeployError("PASSWORD_MISMATCH", "两次密码不一致。")
            initialize_credentials(settings.auth_file, settings.admin_user, password)
            print("运维账号已初始化。密码不会写入仓库或日志。")
        elif args.command == "serve":
            from .api import create_app
            import uvicorn

            if not settings.auth_file.is_file():
                raise DeployError("AUTH_NOT_INITIALIZED", "请先运行 init 初始化运维账号。")
            uvicorn.run(
                create_app(settings), host="127.0.0.1", port=settings.listen_port, proxy_headers=False
            )
        elif args.command == "secret-set":
            from .secrets_store import SecretStore
            from .source import validate_repository

            repository = validate_repository(args.repository, settings)
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", args.subdomain):
                raise DeployError("INVALID_SUBDOMAIN", "域名前缀格式无效。")
            value = getpass.getpass("秘密配置值（输入不回显）：")
            version = SecretStore(settings).set(repository, args.subdomain, args.name, value)
            print(json.dumps({"name": args.name, "version": version}, ensure_ascii=False))
        elif args.command == "schema":
            from .manifest import schema_document

            value = json.dumps(schema_document(), ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.write_text(value)
            else:
                print(value, end="")
        else:
            from .runtime import DeploymentBackend

            backend = DeploymentBackend(settings)
            if args.command == "check":
                print(json.dumps(backend.check(), ensure_ascii=False, indent=2))
            else:
                from .source import GitSource
                from .store import Store
                from .worker import Worker

                runner = Worker(settings, Store(settings), GitSource(settings), backend)
                while True:
                    worked = runner.run_once()
                    if args.once:
                        print(json.dumps({"processed": worked}))
                        break
                    if not worked:
                        time.sleep(2)
    except DeployError as error:
        print(json.dumps({"error": error.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
