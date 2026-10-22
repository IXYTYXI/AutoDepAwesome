"""Server-side Feishu OAuth and eligibility checks; no CLI user session is reused.

Login mode uses OAuth v2 and authen v1 only; continuous mode additionally uses
OAuth v3 for app tokens, contact v3 and application v6. A fresh platform session
must be created only after ``exchange`` succeeds. User access/refresh tokens are
never retained here.
"""

import json
import os
import re
import stat
import threading
import time
from urllib.parse import urlencode

import httpx


AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
USER_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
TENANT_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
API_ORIGIN = "https://open.feishu.cn"
APP_SCOPES = (
    "contact:contact.base:readonly", "contact:user.employee:readonly",
    "application:application:self_manage",
)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RESPONSE_LIMIT = 65536
OPEN_ID = re.compile(r"ou[-_][A-Za-z0-9_-]{1,128}\Z")
_DENIED_CODES = frozenset({20003, 20004, 20005, 20008, 20010, 20021, 20022,
                          20023, 20049, 20065, 20066})


class FeishuDenied(Exception):
    def __init__(self):
        super().__init__("飞书身份未通过验证，请重新登录或联系管理员。")


class FeishuUnavailable(Exception):
    def __init__(self):
        super().__init__("飞书身份核验暂不可用，请稍后重试或联系管理员。")


def _read_secret(path) -> str:
    """Open once, reject special files/links, and never include file contents in errors."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_nlink != 1 or not 0 < info.st_size <= 8192):
                raise ValueError
            value = source.read(8193)
        if len(value) > 8192:
            raise ValueError
        if value.endswith(b"\r\n"):
            value = value[:-2]
        elif value.endswith(b"\n"):
            value = value[:-1]
        if not value or not all(33 <= char <= 126 for char in value):
            raise ValueError
        return value.decode("ascii")
    except (OSError, TypeError, ValueError):
        raise FeishuUnavailable() from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _object(value):
    if not isinstance(value, dict):
        raise FeishuUnavailable() from None
    return value


class FeishuProvider:
    def __init__(self, settings, *, client: httpx.Client | None = None, clock=time.monotonic):
        if settings.feishu_eligibility_mode not in ("login", "continuous"):
            raise FeishuUnavailable() from None
        self.settings = settings
        self._clock = clock
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._token_lock = threading.Lock()
        self._tenant_token_value = None
        self._tenant_token_deadline = 0.0
        if not isinstance(settings.feishu_app_id, str) or not re.fullmatch(
            r"cli_[A-Za-z0-9_]{1,128}", settings.feishu_app_id
        ):
            raise FeishuUnavailable() from None

    @property
    def callback_url(self) -> str:
        return self.settings.public_url.rstrip("/") + "/auth/feishu/callback"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def authorize_url(self, state: str, challenge: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.settings.feishu_app_id,
            "response_type": "code", "redirect_uri": self.callback_url,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def _request(self, method: str, url: str, *, denied_codes=frozenset(), **kwargs) -> dict:
        try:
            # Never follow redirects, even when a caller injects a permissive client.
            with self._client.stream(method, url, timeout=TIMEOUT, follow_redirects=False, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    raise FeishuUnavailable() from None
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    content.extend(chunk)
                    if len(content) > RESPONSE_LIMIT:
                        raise FeishuUnavailable() from None
                body = _object(json.loads(content, object_pairs_hook=_unique_object))
                code = body.get("code")
                if type(code) is not int:
                    raise FeishuUnavailable() from None
                if code in denied_codes and response.status_code < 500:
                    raise FeishuDenied() from None
                if code != 0 or not 200 <= response.status_code < 300:
                    raise FeishuUnavailable() from None
                return body
        except (httpx.HTTPError, ValueError, UnicodeError, OSError):
            # The request/response may contain authorization codes or tokens.
            raise FeishuUnavailable() from None

    @staticmethod
    def _access_token(body: dict) -> tuple[str, int]:
        token, expires, token_type = body.get("access_token"), body.get("expires_in"), body.get("token_type")
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or not all(33 <= ord(char) <= 126 for char in token)
                or type(expires) is not int or not 0 < expires <= 86400
                or not isinstance(token_type, str) or token_type.lower() != "bearer"):
            raise FeishuUnavailable() from None
        return token, expires

    def _tenant_token(self) -> str:
        with self._token_lock:
            started = self._clock()
            if self._tenant_token_value is not None and started < self._tenant_token_deadline:
                return self._tenant_token_value
            body = self._request("POST", TENANT_TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": self.settings.feishu_app_id,
                "client_secret": _read_secret(self.settings.feishu_app_secret_file),
                "scope": " ".join(APP_SCOPES),
            })
            token, ttl = self._access_token(body)
            scope = body.get("scope")
            if not isinstance(scope, str) or not set(APP_SCOPES).issubset(scope.split()):
                raise FeishuUnavailable() from None
            deadline = started + ttl - min(60, ttl * 0.1)
            if self._clock() >= deadline:
                raise FeishuUnavailable() from None
            self._tenant_token_value, self._tenant_token_deadline = token, deadline
            return token

    def exchange(self, code: str, verifier: str) -> dict[str, str]:
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", code)
                or not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)):
            raise FeishuDenied() from None
        token_body = self._request("POST", USER_TOKEN_URL, denied_codes=_DENIED_CODES,
                                  headers={"Content-Type": "application/json; charset=utf-8"}, json={
            "grant_type": "authorization_code", "client_id": self.settings.feishu_app_id,
            "client_secret": _read_secret(self.settings.feishu_app_secret_file),
            "code": code, "redirect_uri": self.callback_url, "code_verifier": verifier,
        })
        user_token, _ = self._access_token(token_body)
        body = self._request("GET", API_ORIGIN + "/open-apis/authen/v1/user_info",
                             headers={"Authorization": "Bearer " + user_token}, denied_codes=_DENIED_CODES)
        identity = _object(body.get("data"))
        open_id, tenant_key, name = identity.get("open_id"), identity.get("tenant_key"), identity.get("name")
        if not isinstance(open_id, str) or OPEN_ID.fullmatch(open_id) is None:
            raise FeishuUnavailable() from None
        if not isinstance(tenant_key, str) or not tenant_key:
            raise FeishuUnavailable() from None
        if tenant_key != self.settings.feishu_tenant_key:
            raise FeishuDenied() from None
        if not isinstance(name, str) or not name or len(name) > 256 or not name.isprintable():
            name = open_id
        if self.settings.feishu_eligibility_mode == "continuous" and not self.check_eligible(open_id):
            raise FeishuDenied() from None
        return {"open_id": open_id, "tenant_key": tenant_key, "name": name}

    def check_eligible(self, open_id: str) -> bool:
        if self.settings.feishu_eligibility_mode != "continuous":
            raise FeishuUnavailable() from None
        if not isinstance(open_id, str) or OPEN_ID.fullmatch(open_id) is None:
            raise FeishuDenied() from None
        headers = {"Authorization": "Bearer " + self._tenant_token()}
        body = self._request("GET", API_ORIGIN + "/open-apis/contact/v3/users/" + open_id,
                             headers=headers, params={"user_id_type": "open_id"})
        user = _object(_object(body.get("data")).get("user"))
        employee_status = _object(user.get("status"))
        expected = {"is_activated": True, "is_frozen": False, "is_resigned": False,
                    "is_exited": False, "is_unjoin": False}
        if user.get("open_id") != open_id or any(
            type(employee_status.get(field)) is not bool for field in expected
        ):
            raise FeishuUnavailable() from None
        if any(employee_status[field] != value for field, value in expected.items()):
            return False
        body = self._request("POST", API_ORIGIN + "/open-apis/application/v6/applications/"
                             + self.settings.feishu_app_id + "/visibility/check_white_black_list",
                             headers=headers, params={"user_id_type": "open_id"},
                             json={"user_ids": [open_id]})
        entries = _object(body.get("data")).get("user_visibility_list")
        if not isinstance(entries, list) or len(entries) != 1:
            raise FeishuUnavailable() from None
        entry = _object(entries[0])
        if entry.get("user_id") != open_id or any(
            type(entry.get(field)) is not bool for field in ("in_white_list", "in_black_list", "in_paid_list")
        ):
            raise FeishuUnavailable() from None
        return (entry["in_white_list"] or entry["in_paid_list"]) and not entry["in_black_list"]
