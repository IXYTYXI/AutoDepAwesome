from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path(".state")
    base_domain: str = "awesome.guanghexinzhi.cn"
    public_url: str = "http://127.0.0.1:8088"
    listen_port: int = 8088
    git_hosts: tuple[str, ...] = ("github.com", "gitlab.com")
    git_askpass: str | None = None
    git_proxy_url: str | None = None
    git_timeout: int = 120
    port_start: int = 20000
    port_end: int = 29999
    max_cpu: float = 6.0
    max_memory_mib: int = 6144
    build_timeout: int = 900
    health_timeout: int = 180
    docker_host: str = "unix:///run/user/1001/docker.sock"
    rootlesskit_socket: Path | None = None
    docker_binary: str = "docker"
    build_network: str = "none"
    builder_name: str = "awesome-builder"
    gateway_command: tuple[str, ...] = ("sudo", "-n", "/usr/local/libexec/awesome-gateway")
    storage_backend: str = "local"
    storage_command: tuple[str, ...] = ("sudo", "-n", "/usr/local/libexec/awesome-storage")
    runtime_user: str = "65532:65532"
    runtime_network: str = "internal"
    runtime_dns: tuple[str, ...] = ("223.5.5.5", "1.1.1.1")
    runtime_blocked_ips: tuple[str, ...] = ()
    egress_docker_pid_file: Path | None = None
    required_free_mib: int = 2048
    docker_storage_limit_mib: int = 81920
    repo_size_limit_mib: int = 256
    auth_file: Path | None = None
    admin_user: str = "operator"
    auth_mode: str = "basic"
    feishu_app_id: str = ""
    feishu_tenant_key: str = ""
    feishu_app_secret_file: Path | None = None
    ai_base_url: str | None = None
    ai_model: str = ""
    ai_api_key_file: Path | None = None
    ai_reasoning_effort: str | None = None
    reserved_names: tuple[str, ...] = (
        "www",
        "api",
        "admin",
        "auth",
        "static",
        "assets",
        "mail",
        "ftp",
        "status",
    )
    # API and manifest validation intentionally reject any matching substring.
    banned_words: tuple[str, ...] = ("fuck", "shit")

    @property
    def database_path(self) -> Path:
        return self.state_dir / "platform.sqlite3"


def validate_identity_settings(settings: Settings) -> None:
    if settings.auth_mode not in {"basic", "feishu"}:
        raise ValueError("authentication mode")
    if settings.auth_mode == "basic":
        if settings.feishu_app_id or settings.feishu_tenant_key or settings.feishu_app_secret_file is not None:
            raise ValueError("Feishu settings require explicit Feishu mode")
        return
    if (not re.fullmatch(r"cli_[A-Za-z0-9]{1,80}", settings.feishu_app_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", settings.feishu_tenant_key)
            or not isinstance(settings.feishu_app_secret_file, Path)):
        raise ValueError("Feishu identity settings")
    public = urlsplit(settings.public_url)
    if (public.scheme != "https" or not public.hostname or public.username or public.password
            or public.query or public.fragment or public.path not in ("", "/")):
        raise ValueError("Feishu login requires a fixed HTTPS origin")


def validate_storage_settings(settings: Settings) -> None:
    """Quota support is opt-in and uses a fixed, root-owned helper entry point."""
    if settings.storage_backend not in ("local", "xfs-project"):
        raise ValueError("storage backend")
    if settings.storage_command != ("sudo", "-n", "/usr/local/libexec/awesome-storage"):
        raise ValueError("storage helper")
    if settings.storage_backend == "xfs-project":
        if not isinstance(settings.runtime_user, str) or not re.fullmatch(
            r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", settings.runtime_user
        ):
            raise ValueError("quota storage requires a fixed non-root numeric UID and GID")


def validate_git_proxy_settings(settings: Settings) -> None:
    """Accept only explicit, unauthenticated operator proxy endpoints."""
    value = settings.git_proxy_url
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Git proxy URL")
    match = re.fullmatch(r"(?:https?|socks5h)://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})/?", value)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ValueError("Git proxy URL")
    host = match[1]
    if host.startswith("[") or re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            raise ValueError("Git proxy host") from None
    elif len(host) > 253 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError("Git proxy host")


def validate_ai_settings(settings: Settings) -> None:
    """Validate operator configuration without opening the API key file."""
    base, model, key = settings.ai_base_url, settings.ai_model, settings.ai_api_key_file
    effort = settings.ai_reasoning_effort
    if effort is not None and (
        not isinstance(effort, str)
        or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    ):
        raise ValueError("AI reasoning effort")
    if base is None and model == "" and key is None:
        if effort is not None:
            raise ValueError("AI reasoning effort requires configured AI")
        return
    if (
        not isinstance(base, str)
        or not isinstance(model, str)
        or not isinstance(key, Path)
        or not 1 <= len(model) <= 128
        or model != model.strip()
        or not all(char.isprintable() for char in model)
        or not re.fullmatch(r"https://[A-Za-z0-9.\[\]:_~/-]+", base)
    ):
        raise ValueError("AI configuration")
    url = urlsplit(base)
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or (url.port is not None and not 1 <= url.port <= 65535)
        or any(part in (".", "..") for part in url.path.split("/"))
    ):
        raise ValueError("AI URL")
