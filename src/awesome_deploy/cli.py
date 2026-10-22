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
from .config import (Settings, validate_ai_settings, validate_git_proxy_settings, validate_storage_settings,
                     validate_identity_settings, validate_vault_settings)
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
    for key in ("state_dir", "auth_file", "rootlesskit_socket", "ai_api_key_file", "egress_docker_pid_file", "feishu_app_secret_file", "secret_kek_file"):
        if key in values and values[key] is not None:
            try:
                candidate = Path(values[key]).expanduser()
            except TypeError as exc:
                raise DeployError("INVALID_CONFIG", f"{key} 必须是路径。") from exc
            if key == "egress_docker_pid_file":
                if not candidate.is_absolute() or any(part == ".." for part in candidate.parts):
                    raise DeployError("INVALID_CONFIG", "egress_docker_pid_file 必须是绝对路径。")
                values[key] = candidate
            elif key in {"ai_api_key_file", "feishu_app_secret_file", "secret_kek_file"}:
                # Preserve the final path component so O_NOFOLLOW can reject symlinks.
                values[key] = Path(os.path.abspath(base / candidate))
            else:
                values[key] = (base / candidate).resolve() if not candidate.is_absolute() else candidate
    if "state_dir" not in values:
        values["state_dir"] = base / ".state"
    for key in ("git_hosts", "gateway_command", "storage_command", "reserved_names", "banned_words"):
        if key in values:
            if (
                not isinstance(values[key], list)
                or not values[key]
                or not all(isinstance(v, str) for v in values[key])
            ):
                raise DeployError("INVALID_CONFIG", f"{key} 必须是非空字符串数组。")
            values[key] = tuple(values[key])
    for key in ("runtime_dns", "runtime_blocked_ips"):
        if key in values:
            if not isinstance(values[key], list) or not all(isinstance(v, str) for v in values[key]):
                raise DeployError("INVALID_CONFIG", f"{key} 必须是字符串数组。")
            values[key] = tuple(values[key])
    if not values.get("auth_file"):
        values["auth_file"] = values["state_dir"] / "admin.json"
    try:
        settings = Settings(**values)
        validate_git_proxy_settings(settings)
        validate_ai_settings(settings)
        validate_storage_settings(settings)
        validate_identity_settings(settings)
        validate_vault_settings(settings)
        from .egress import policy

        try:
            runtime_policy = policy(settings)
        except DeployError as exc:
            raise ValueError("runtime network") from exc
        if runtime_policy is not None and not isinstance(settings.egress_docker_pid_file, Path):
            raise ValueError("public runtime requires an explicit Docker PID path")
        integers = (
            "listen_port",
            "port_start",
            "port_end",
            "max_memory_mib",
            "max_app_memory_mib",
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
        raise DeployError("INVALID_CONFIG", "运维配置中的域名、URL、端口、资源限制、网络或 AI 配置无效。") from exc
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Awesome 仓库到 HTTPS 部署平台")
    parser.add_argument("--config", type=Path, help="运维 JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="交互初始化本地运维账号")
    sub.add_parser("serve", help="仅在 127.0.0.1 启动网页/API")
    worker = sub.add_parser("worker", help="运行持久化部署执行器")
    worker.add_argument("--once", action="store_true")
    base_sync = sub.add_parser("base-sync", help="独立同步应用与部署记录到飞书多维表格")
    base_sync.add_argument("--base-config", type=Path, default=Path("/etc/awesome-deploy/base-sync.json"))
    base_sync.add_argument("--once", action="store_true")
    base_sync.add_argument("--status", action="store_true", help="仅查看本地同步状态")
    sub.add_parser("check", help="只读检查服务器部署前提")
    sub.add_parser("user-list", help="列出本平台已登录的飞书用户，不输出任何令牌")
    users = sub.add_parser("user-set", help="在可信服务器终端设置飞书用户权限，并撤销其旧会话及排队授权")
    users.add_argument("--user-id", required=True)
    for flag in ("enabled", "can-deploy", "admin"):
        users.add_argument("--" + flag, choices=("true", "false"))
    sub.add_parser("auth-revoke-all", help="注销所有平台会话与在途登录；恢复数据库后必须执行")
    api_token = sub.add_parser('api-token-create', help='在可信服务器终端为指定用户或来源签发自动化部署凭证')
    api_token.add_argument('--user-id', required=True, help='负责此凭证的已登录飞书用户 ID')
    api_token.add_argument('--name', required=True, help='凭证或自动化来源名称')
    api_token.add_argument('--kind', choices=('person', 'source'), required=True)
    api_token.add_argument('--days', type=int, default=30, help='有效天数，1–90；默认 30')
    scope = api_token.add_mutually_exclusive_group(required=True)
    scope.add_argument('--app-id', help='仅允许更新这个已有应用')
    scope.add_argument('--allow-create', action='store_true', help='仅允许创建新应用')
    scope.add_argument('--all-apps', action='store_true', help='允许创建应用并更新负责人有权限的任意应用')
    sub.add_parser('api-token-list', help='列出自动化部署凭证元数据，不显示凭证值')
    api_revoke = sub.add_parser('api-token-revoke', help='撤销自动化部署凭证')
    api_revoke.add_argument('--id', required=True, help='凭证 ID')
    secret = sub.add_parser("secret-set", help="安全输入应用秘密配置")
    secret.add_argument("--repository")
    secret.add_argument("--subdomain")
    secret.add_argument("--name", required=True)
    secret.add_argument("--app-id", help="加密模式使用稳定应用标识")
    secret.add_argument("--expected-current-version", help="加密模式普通轮换的当前版本")
    secret.add_argument("--reason", help="非敏感操作原因，普通轮换必填")
    from .vault_cli import add_arguments
    add_arguments(sub)
    schema = sub.add_parser("schema", help="输出当前支持的 JSON Schema")
    schema.add_argument("--output", type=Path)
    schema.add_argument("--version", type=int, choices=(1, 2), default=1)
    sub.add_parser("storage-check", help="核验 XFS 数据池并更新只读能力快照")
    status = sub.add_parser("storage-status", help="查询存储额度、用量及未决操作")
    status.add_argument("--app-id")
    sub.add_parser("storage-patrol", help="独立巡检配额并停止已确认失效的写入者")
    for name, help_text in (("storage-expand", "显式扩容，不自动缩容"),
                            ("storage-unblock", "核验修复结果后解除存储阻断"),
                            ("storage-retry", "重试账本中原先未决的操作（含删除）")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--storage-id", required=name != "storage-unblock")
        command.add_argument("--generation", required=name != "storage-unblock")
        if name == "storage-unblock":
            command.add_argument("--pool", action="store_true", help="仅解除全池阻断，随后逐项核验存储")
        if name == "storage-expand":
            command.add_argument("--size-mib", type=int, required=True)
            command.add_argument("--max-inodes", type=int, required=True)
            command.add_argument("--expected-revision", type=int, required=True)
        if name in ("storage-expand", "storage-retry"):
            command.add_argument("--operation-id", required=True)
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

            if settings.auth_mode == "basic" and not settings.auth_file.is_file():
                raise DeployError("AUTH_NOT_INITIALIZED", "请先运行 init 初始化运维账号。")
            uvicorn.run(
                create_app(settings), host="127.0.0.1", port=settings.listen_port, proxy_headers=False,
                access_log=False,  # OAuth callback query strings contain one-time credentials.
            )
        elif args.command in {"user-list", "user-set", "auth-revoke-all"}:
            if settings.auth_mode != "feishu":
                raise DeployError("INVALID_CONFIG", "该命令需要 auth_mode=feishu。")
            from .store import Store
            from .identity import IdentityStore
            Store(settings)  # Run legacy schema migrations before adding identity tables.
            identities = IdentityStore(settings)
            try:
                if args.command == "user-list":
                    print(json.dumps([{"userId": row["id"], "displayName": row["name"],
                                       "openId": row["open_id"], "enabled": bool(row["enabled"]),
                                       "canDeploy": bool(row["can_deploy"]), "admin": bool(row["is_admin"])}
                                      for row in identities.list_users()], ensure_ascii=False, indent=2))
                elif args.command == "user-set":
                    values = {name: None if value is None else value == "true" for name, value in
                              {"enabled": args.enabled, "can_deploy": args.can_deploy, "is_admin": args.admin}.items()}
                    identities.set_user(args.user_id, **values)
                    print("权限已更新；该用户旧会话和未开始执行的旧授权已失效。")
                else:
                    identities.revoke_all_sessions()
                    print("全部平台会话及在途登录已撤销。")
            finally:
                identities.provider.close()
        elif args.command in {'api-token-create', 'api-token-list', 'api-token-revoke'}:
            if settings.auth_mode != 'feishu':
                raise DeployError('INVALID_CONFIG', '自动化 API 凭证仅在飞书登录模式可用。')
            from .store import Store
            from .identity import IdentityStore
            from .api_credentials import ApiCredentialStore
            Store(settings)
            identities = IdentityStore(settings)
            try:
                credentials = ApiCredentialStore(settings)
                if args.command == 'api-token-create':
                    issued = credentials.issue(args.user_id, args.name, kind=args.kind, app_id=args.app_id,
                                               allow_create=args.allow_create, allow_any_app=args.all_apps,
                                               expires_days=args.days)
                    print(json.dumps(issued, ensure_ascii=False))
                elif args.command == 'api-token-list':
                    print(json.dumps(credentials.list(), ensure_ascii=False, indent=2))
                else:
                    credentials.revoke(args.id)
                    print('凭证已撤销。')
            finally:
                identities.provider.close()
        elif args.command == "secret-set":
            if settings.secret_backend == "encrypted":
                from .vault_cli import set_secret
                return set_secret(settings, args)
            if not args.repository or not args.subdomain or args.app_id or args.expected_current_version:
                raise DeployError("INVALID_REQUEST", "旧格式secret-set必须指定repository和subdomain。")
            from .deployment_secrets import DeploymentSecrets
            from .source import validate_repository
            from .store import Store

            repository = validate_repository(args.repository, settings)
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", args.subdomain):
                raise DeployError("INVALID_SUBDOMAIN", "域名前缀格式无效。")
            value = getpass.getpass("秘密配置值（输入不回显）：")
            # Lock acquisition only needs the protected filesystem. Constructing
            # Store is deliberately deferred until the worker lock is held.
            configuration = DeploymentSecrets(settings, None)
            with configuration._worker_lock():
                with Store(settings).secret_write_scope(repository, args.subdomain):
                    version = configuration.secrets.set(repository, args.subdomain, args.name, value)
            print(json.dumps({"name": args.name, "version": version}, ensure_ascii=False))
        elif args.command == "base-sync":
            if settings.auth_mode != "feishu":
                raise DeployError("INVALID_CONFIG", "多维表格同步需要飞书登录模式。")
            from .base_sync import BaseMirror, BaseRemote, BaseSyncError, load_base_config
            try:
                config = load_base_config(args.base_config)
                remote = None if args.status else BaseRemote(settings, config)
                mirror = BaseMirror(settings.database_path, remote, base_domain=settings.base_domain)
                try:
                    if args.status:
                        print(json.dumps(mirror.status(), ensure_ascii=False))
                    else:
                        while True:
                            result = mirror.run_once()
                            if args.once:
                                print(json.dumps(result, ensure_ascii=False))
                                return 1 if result["pending"] else 0
                            time.sleep(15)
                finally:
                    if remote:
                        remote.close()
            except BaseSyncError as exc:
                raise DeployError("BASE_SYNC_FAILED", str(exc)) from None
        elif args.command.startswith("vault-"):
            from .vault_cli import command
            return command(settings, args)
        elif args.command == "schema":
            from .manifest import schema_document

            value = json.dumps(schema_document(args.version), ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.write_text(value)
            else:
                print(value, end="")
        elif args.command.startswith("storage-"):
            return _storage_command(settings, args)
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


def _storage_command(settings, args):
    from .quota_client import QuotaClient
    from .storage_status import begin_quota_observation, record_quota_status

    if settings.storage_backend != "xfs-project":
        raise DeployError("QUOTA_NOT_ENABLED", "请先配置并验收 XFS 硬配额后端。")
    helper = QuotaClient(settings.storage_command)
    request = {}
    pool_unblock = args.command == "storage-unblock" and args.pool
    if pool_unblock:
        if args.storage_id or args.generation:
            raise DeployError("QUOTA_INVALID_REQUEST", "全池解除阻断不能同时指定单个存储。")
        request["pool"] = True
    if args.command in ("storage-expand", "storage-unblock", "storage-retry") and not pool_unblock:
        if (not re.fullmatch(r"[0-9a-f]{64}", args.storage_id or "")
                or not re.fullmatch(r"[0-9a-f]{32}", args.generation or "")):
            raise DeployError("QUOTA_INVALID_REQUEST", "必须提供完整存储标识和实例代次。")
        request["expected"] = {
            "backend": "xfs-project", "storage_id": args.storage_id, "generation": args.generation,
        }
    if args.command == "storage-expand":
        if (not 1 <= args.size_mib <= 1073741824 or not 1 <= args.max_inodes <= 2147483647
                or args.expected_revision < 1
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", args.operation_id)):
            raise DeployError("QUOTA_INVALID_REQUEST", "额度、版本和幂等操作标识无效。")
        request.update(limits={"size_mib": args.size_mib, "max_inodes": args.max_inodes},
                       expected_revision=args.expected_revision, operation_id=args.operation_id)
    if args.command == "storage-retry":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", args.operation_id):
            raise DeployError("QUOTA_INVALID_REQUEST", "原始操作标识无效。")
        request["operation_id"] = args.operation_id
    if args.command == "storage-status" and args.app_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.app_id):
            raise DeployError("QUOTA_INVALID_REQUEST", "应用标识无效。")
        request["app_id"] = args.app_id
    cache = args.command in ("storage-check", "storage-patrol")
    observation = begin_quota_observation(settings) if cache else None
    try:
        if args.command == "storage-patrol":
            from .runtime import DeploymentBackend
            result = DeploymentBackend(settings).quota_patrol()
        else:
            operation = {"storage-check": "check", "storage-status": "inventory",
                         "storage-expand": "expand", "storage-unblock": "unblock",
                         "storage-retry": "retry"}[args.command]
            result = helper.call(operation, **request)
    except DeployError as error:
        if cache:
            record_quota_status(settings, error=error, observation=observation)
        raise
    if cache:
        record_quota_status(settings, result, observation=observation)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "storage-patrol" and any(
            result.get(k) for k in ("pool_blocked", "blocked", "unknown", "stop_failures")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
