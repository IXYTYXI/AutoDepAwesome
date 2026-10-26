"""Small shared checks for operator configuration, without I/O or secret values."""

import re
from urllib.parse import urlsplit


def validate_http_origin(value: object, *, allow_loopback_http: bool = False) -> None:
    # urlsplit strips some controls and accepts empty userinfo/query delimiters.
    # Reject those before parsing so the configured origin is never ambiguous.
    if (not isinstance(value, str) or not value
            or any(char.isspace() or not char.isprintable() for char in value)
            or any(char in value for char in ("\\", "?", "#"))):
        raise ValueError("invalid HTTP origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid HTTP origin") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("invalid HTTP origin")
    if parsed.scheme != "https" and not (
        allow_loopback_http and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
    ):
        raise ValueError("HTTPS origin required")


def is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and all(char.isprintable() for char in value)


def is_runtime_user(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", value) is not None


def is_basic_username(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 128 and ":" not in value
            and all(33 <= ord(char) <= 126 for char in value))
