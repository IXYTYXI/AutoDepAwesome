"""Shared identifier rules accepted by the platform's Feishu configuration."""

import re


_APP_ID = re.compile(r"cli_[A-Za-z0-9]{1,80}")


def is_valid_feishu_app_id(value: object) -> bool:
    """Accept the existing platform format without coercion or trimming."""
    return isinstance(value, str) and _APP_ID.fullmatch(value) is not None


def is_valid_feishu_tenant_key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None
