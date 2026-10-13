from dataclasses import dataclass
from pathlib import Path


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
