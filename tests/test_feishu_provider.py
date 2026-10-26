from concurrent.futures import ThreadPoolExecutor
import json
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from awesome_deploy.feishu import FeishuDenied, FeishuProvider, FeishuUnavailable


APP_SCOPES = {
    "contact:contact.base:readonly", "contact:user.employee:readonly",
    "application:application:self_manage",
}
OPEN_ID = "ou_test_user"


@pytest.fixture
def settings(tmp_path):
    secret = tmp_path / "feishu-secret"
    secret.write_text("test-app-secret\n")
    secret.chmod(0o600)
    return SimpleNamespace(
        public_url="https://deploy.example.com/", feishu_app_id="cli_testapp",
        feishu_tenant_key="tenant-1", feishu_app_secret_file=secret,
        feishu_eligibility_mode="continuous",
    )


def status(**changes):
    result = dict(is_activated=True, is_frozen=False, is_resigned=False,
                  is_exited=False, is_unjoin=False)
    result.update(changes)
    return result


class FeishuAPI:
    def __init__(self):
        self.calls = []
        self.employee_status = status()
        self.visibility = dict(user_id=OPEN_ID, in_white_list=True,
                               in_black_list=False, in_paid_list=False)
        self.token_ttl = 7200
        self.identity_tenant = "tenant-1"
        self.override = {}

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path in self.override:
            return self.override[path](request)
        if path in {"/oauth/v3/token", "/open-apis/authen/v2/oauth/token"}:
            if path == "/oauth/v3/token":
                body = parse_qs(request.content.decode())
                assert body["grant_type"] == ["client_credentials"]
                token = "tenant-token"
            else:
                body = json.loads(request.content)
                assert body["grant_type"] == "authorization_code"
                token = "user-token"
            return httpx.Response(200, json={"code": 0, "access_token": token,
                "expires_in": self.token_ttl, "token_type": "Bearer",
                "scope": " ".join(sorted(APP_SCOPES)) if token == "tenant-token" else "",
                "refresh_token": "must-not-persist-this"})
        if path == "/open-apis/authen/v1/user_info":
            assert request.headers["Authorization"] == "Bearer user-token"
            return httpx.Response(200, json={"code": 0, "data": {
                "open_id": OPEN_ID, "tenant_key": self.identity_tenant, "name": "测试成员",
                "email": "do-not-return@example.com", "mobile": "do-not-return"}})
        if path == f"/open-apis/contact/v3/users/{OPEN_ID}":
            assert request.headers["Authorization"] == "Bearer tenant-token"
            assert request.url.params["user_id_type"] == "open_id"
            return httpx.Response(200, json={"code": 0, "data": {
                "user": {"open_id": OPEN_ID, "status": self.employee_status}}})
        if path.endswith("/visibility/check_white_black_list"):
            assert request.headers["Authorization"] == "Bearer tenant-token"
            assert request.url.params["user_id_type"] == "open_id"
            assert json.loads(request.content) == {"user_ids": [OPEN_ID]}
            return httpx.Response(200, json={"code": 0, "data": {
                "user_visibility_list": [self.visibility]}})
        pytest.fail(f"unexpected API path {path}")


def provider(settings, api=None, **kwargs):
    api = api or FeishuAPI()
    return FeishuProvider(settings, client=httpx.Client(
        transport=httpx.MockTransport(api), trust_env=False), **kwargs), api


def test_authorization_uses_fixed_callback_and_s256_without_offline_access(settings):
    client, api = provider(settings)
    url = urlsplit(client.authorize_url("random-state", "random-challenge"))
    assert (url.scheme, url.netloc, url.path) == (
        "https", "accounts.feishu.cn", "/open-apis/authen/v1/authorize")
    query = parse_qs(url.query)
    assert query == {"client_id": ["cli_testapp"], "response_type": ["code"],
                     "redirect_uri": ["https://deploy.example.com/auth/feishu/callback"],
                     "state": ["random-state"], "code_challenge": ["random-challenge"],
                     "code_challenge_method": ["S256"]}
    assert api.calls == []


def test_exchange_checks_identity_employment_and_app_visibility(settings):
    client, api = provider(settings)
    assert client.exchange("valid-code", "v" * 43) == {
        "open_id": OPEN_ID, "tenant_key": "tenant-1", "name": "测试成员"}
    body = json.loads(api.calls[0].content)
    assert api.calls[0].url == "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    assert api.calls[0].headers["content-type"] == "application/json; charset=utf-8"
    assert body == {"grant_type": "authorization_code", "client_id": "cli_testapp",
                    "client_secret": "test-app-secret", "code": "valid-code",
                    "redirect_uri": "https://deploy.example.com/auth/feishu/callback",
                    "code_verifier": "v" * 43}
    assert api.calls[2].url == "https://accounts.feishu.cn/oauth/v3/token"
    assert api.calls[2].headers["content-type"].startswith("application/x-www-form-urlencoded")
    tenant_body = parse_qs(api.calls[2].content.decode())
    assert tenant_body["grant_type"] == ["client_credentials"]
    assert set(tenant_body["scope"][0].split()) == APP_SCOPES
    assert not hasattr(client, "refresh_token")
    assert "must-not-persist-this" not in repr(vars(client))


def test_login_mode_only_exchanges_code_and_reads_identity(settings):
    settings.feishu_eligibility_mode = "login"
    client, api = provider(settings)
    assert client.exchange("valid-code", "v" * 43) == {
        "open_id": OPEN_ID, "tenant_key": "tenant-1", "name": "测试成员"}
    assert [request.url.path for request in api.calls] == [
        "/open-apis/authen/v2/oauth/token", "/open-apis/authen/v1/user_info"]
    body = json.loads(api.calls[0].content)
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == "v" * 43
    assert "scope" not in body
    assert client._tenant_token_value is None
    assert not hasattr(client, "refresh_token")
    assert "must-not-persist-this" not in repr(vars(client))


def test_login_mode_cannot_accidentally_run_remote_eligibility_checks(settings):
    settings.feishu_eligibility_mode = "login"
    client, api = provider(settings)
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)
    assert api.calls == []


def test_unknown_eligibility_mode_cannot_create_provider(settings):
    settings.feishu_eligibility_mode = "fallback"
    with pytest.raises(FeishuUnavailable):
        provider(settings)


@pytest.mark.parametrize("mode", ["continuous", "login"])
def test_invalid_open_id_from_user_info_is_rejected(settings, mode):
    settings.feishu_eligibility_mode = mode
    client, api = provider(settings)
    api.override["/open-apis/authen/v1/user_info"] = lambda request: httpx.Response(200, json={
        "code": 0, "data": {"open_id": "", "tenant_key": "tenant-1", "name": "Test"}})
    with pytest.raises(FeishuUnavailable):
        client.exchange("code", "v" * 43)
    assert len(api.calls) == 2


@pytest.mark.parametrize("mode", ["continuous", "login"])
def test_foreign_tenant_never_uses_tenant_credentials(settings, mode):
    settings.feishu_eligibility_mode = mode
    client, api = provider(settings)
    api.identity_tenant = "another-tenant"
    with pytest.raises(FeishuDenied):
        client.exchange("code", "v" * 43)
    assert len(api.calls) == 2


@pytest.mark.parametrize("field,value", [
    ("is_activated", False), ("is_frozen", True), ("is_resigned", True),
    ("is_exited", True), ("is_unjoin", True),
])
def test_ineligible_employee_is_denied_before_visibility(settings, field, value):
    client, api = provider(settings)
    api.employee_status[field] = value
    assert client.check_eligible(OPEN_ID) is False
    assert len(api.calls) == 2
    with pytest.raises(FeishuDenied):
        client.exchange("code", "v" * 43)


@pytest.mark.parametrize("field", list(status()))
@pytest.mark.parametrize("invalid", [None, "false", 0, 1])
def test_unknown_employee_status_fails_closed(settings, field, invalid):
    client, api = provider(settings)
    api.employee_status[field] = invalid
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)
    del api.employee_status[field]
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)


@pytest.mark.parametrize("white,paid,black,expected", [
    (True, False, False, True), (False, True, False, True),
    (False, False, False, False), (True, True, True, False),
])
def test_visibility_must_allow_user_and_not_blacklist(settings, white, paid, black, expected):
    client, api = provider(settings)
    api.visibility.update(in_white_list=white, in_paid_list=paid, in_black_list=black)
    assert client.check_eligible(OPEN_ID) is expected


@pytest.mark.parametrize("field", ["in_white_list", "in_paid_list", "in_black_list", "user_id"])
def test_incomplete_visibility_is_unavailable(settings, field):
    client, api = provider(settings)
    del api.visibility[field]
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)


@pytest.mark.parametrize("entries", [[], [{"user_id": "ou_another"}],
                                      [{"user_id": OPEN_ID}, {"user_id": OPEN_ID}]])
def test_visibility_response_must_match_exactly_one_requested_user(settings, entries):
    client, api = provider(settings)
    api.override["/open-apis/application/v6/applications/cli_testapp/visibility/check_white_black_list"] = (
        lambda request: httpx.Response(200, json={"code": 0, "data": {"user_visibility_list": entries}}))
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)


def test_tenant_token_is_cached_with_expiry_margin_and_thread_safe(settings):
    now = [100.0]
    client, api = provider(settings, clock=lambda: now[0])
    api.token_ttl = 100
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(client.check_eligible, [OPEN_ID] * 8))
    def count():
        return sum(request.url.path == "/oauth/v3/token" for request in api.calls)
    assert count() == 1
    now[0] = 191
    assert client.check_eligible(OPEN_ID)
    assert count() == 2


@pytest.mark.parametrize("payload", [
    {"code": 0, "tenant_access_token": "old-field", "expire": 7200},
    {"code": 0, "access_token": "token", "expires_in": True, "token_type": "Bearer"},
    {"code": 0, "access_token": "token", "expires_in": 0, "token_type": "Bearer"},
    {"code": 0, "access_token": "token", "expires_in": 7200, "token_type": "Basic"},
    {"code": 0, "access_token": "token", "expires_in": 7200, "token_type": "Bearer", "scope": ""},
    {"code": False, "access_token": "token", "expires_in": 7200, "token_type": "Bearer"},
])
def test_malformed_or_insufficient_tenant_token_is_unavailable(settings, payload):
    client, api = provider(settings)
    api.override["/oauth/v3/token"] = lambda request: httpx.Response(200, json=payload)
    with pytest.raises(FeishuUnavailable):
        client.check_eligible(OPEN_ID)


@pytest.mark.parametrize("mode", ["continuous", "login"])
@pytest.mark.parametrize("code", [20003, 20004, 20008, 20010, 20049, 20065, 20066])
def test_rejected_authorization_code_is_denied_without_leaking_error_or_fallback(settings, mode, code):
    settings.feishu_eligibility_mode = mode
    client, api = provider(settings)
    api.override["/open-apis/authen/v2/oauth/token"] = lambda request: httpx.Response(400, json={
        "code": code, "error_description": "sensitive upstream echo test-app-secret valid-code"})
    with pytest.raises(FeishuDenied) as caught:
        client.exchange("valid-code", "v" * 43)
    assert "test-app-secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert [request.url.path for request in api.calls] == ["/open-apis/authen/v2/oauth/token"]


@pytest.mark.parametrize("kind", ["redirect", "timeout", "server", "json", "oversize", "duplicate"])
def test_upstream_failure_is_sanitized_and_redirects_are_not_followed(settings, kind):
    requests = []
    def fail(request):
        requests.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("test-app-secret", request=request)
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.example/"})
        if kind == "server":
            return httpx.Response(500, json={"code": 20050, "error": "test-app-secret"})
        if kind == "json":
            return httpx.Response(200, content=b"not json test-app-secret")
        if kind == "duplicate":
            return httpx.Response(200, content=b'{"code":20050,"code":0}')
        return httpx.Response(200, content=b"x" * (65536 + 1))
    client = FeishuProvider(settings, client=httpx.Client(
        transport=httpx.MockTransport(fail), follow_redirects=True, trust_env=False))
    with pytest.raises(FeishuUnavailable) as caught:
        client.exchange("code", "v" * 43)
    assert "test-app-secret" not in str(caught.value)
    assert len(requests) == 1
    assert requests[0].url.path == "/open-apis/authen/v2/oauth/token"
    assert requests[0].extensions["timeout"]["read"] <= 10


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory", "fifo", "empty", "multiline", "oversize"])
def test_secret_file_must_be_private_single_line_regular_file(settings, tmp_path, kind):
    path = settings.feishu_app_secret_file
    if kind == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, tmp_path / "second-link")
    elif kind == "public":
        path.chmod(0o644)
    elif kind in {"directory", "fifo"}:
        path.unlink()
        path.mkdir() if kind == "directory" else os.mkfifo(path, 0o600)
    else:
        path.write_text({"empty": "", "multiline": "first\nsecond", "oversize": "a" * 8193}[kind])
    client, api = provider(settings)
    with pytest.raises(FeishuUnavailable):
        client.exchange("code", "v" * 43)
    assert api.calls == []


def test_secret_owner_must_match_service_user(settings, monkeypatch):
    client, api = provider(settings)
    monkeypatch.setattr(os, "geteuid", lambda: settings.feishu_app_secret_file.stat().st_uid + 1)
    with pytest.raises(FeishuUnavailable):
        client.exchange("code", "v" * 43)
    assert api.calls == []


@pytest.mark.parametrize("open_id", ["../etc/passwd", "ou_a/b", "ou_?a", "", "x" * 300])
def test_unsafe_identity_cannot_change_api_path(settings, open_id):
    client, api = provider(settings)
    with pytest.raises(FeishuDenied):
        client.check_eligible(open_id)
    assert api.calls == []
