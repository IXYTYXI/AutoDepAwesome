from dataclasses import dataclass
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
    runtime_user: str = "65532:65532"
    required_free_mib: int = 2048
    docker_storage_limit_mib: int = 81920
    repo_size_limit_mib: int = 256
    auth_file: Path | None = None
    admin_user: str = "operator"
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
