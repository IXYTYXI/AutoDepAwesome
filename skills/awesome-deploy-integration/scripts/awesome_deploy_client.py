#!/usr/bin/env python3
"""Standard-library backend client. CLI deliberately exposes read-only operations.

Credential order: AWESOME_INTAKE_TOKEN, AWESOME_INTAKE_TOKEN_FILE,
/etc/awesome-team/intake-token. Never print credentials or session identifiers.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


TERMINAL = frozenset({"succeeded", "failed", "recovery_required", "cancelled", "rejected"})
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_RESPONSE = 1024 * 1024


class ClientError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class WaitTimeout(ClientError):
    def __init__(self, request_id, last_request):
        super().__init__("等待已到期；平台任务未被取消，请保留原 requestId 继续查询。")
        self.request_id = request_id
        self.last_request = last_request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("适配服务地址无效。")
    try:
        parts = urlsplit(value)
        port = parts.port
        valid = (parts.scheme in {"http", "https"} and parts.hostname
                 and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and parts.path in {"", "/"}
                 and "?" not in value and "#" not in value and "\\" not in value
                 and (port is None or 1 <= port <= 65535)
                 and (parts.scheme == "https" or parts.hostname in {"127.0.0.1", "::1", "localhost"}))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("地址只允许 loopback HTTP 或有效 HTTPS origin，不含用户信息、路径、查询或片段。")
    return value.rstrip("/")


def _credential():
    token = os.environ.get("AWESOME_INTAKE_TOKEN")
    if token is None:
        path = Path(os.environ.get("AWESOME_INTAKE_TOKEN_FILE", "/etc/awesome-team/intake-token"))
        if not path.is_absolute():
            raise ClientError("AWESOME_INTAKE_TOKEN_FILE 必须是绝对路径。")
        try:
            with path.open("r", encoding="ascii") as stream:
                token = stream.read(8193).strip()
        except (OSError, UnicodeError):
            raise ClientError("无法读取内部凭证。请让运维将凭证安全注入 AWESOME_INTAKE_TOKEN，"
                              "或提供仅接入服务账号可读的凭证文件并设置 AWESOME_INTAKE_TOKEN_FILE；"
                              "不要 sudo 读取或输出凭证。") from None
    if (not token or len(token) > 8192 or not token.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)):
        raise ClientError("内部凭证为空或格式无效；请由运维检查安全注入。")
    return token


def _session(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ValueError("sessionId 必须取自可信后端保存的有效共享会话。")


def _request_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ValueError("requestId 必须为 1–128 位字母、数字、点、下划线、冒号或中划线。")


class AwesomeDeployClient:
    def __init__(self, origin=None, *, timeout=10):
        self.origin = _origin(origin or os.environ.get("AWESOME_INTAKE_ORIGIN", "http://127.0.0.1:8099"))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是有限正数秒。")
        self.timeout = timeout
        self._token = _credential()
        self._opener = build_opener(ProxyHandler({}), NoRedirect(),
                                    HTTPSHandler(context=ssl.create_default_context()))

    def _call(self, method, path, *, body=None, request_id=None, timeout=None):
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        if request_id is not None:
            headers["Idempotency-Key"] = request_id
        request = Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                if not 200 <= response.status < 300:
                    raise ClientError("接口返回非成功状态；未跟随重定向。", status=response.status)
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            message = ("接口重定向已阻止；请核对适配服务地址。" if 300 <= status < 400
                       else "接口返回 HTTP %d；请按 API 契约处理，未自动重试。" % status)
            raise ClientError(message, status=status) from None
        except (URLError, OSError, TimeoutError):
            raise ClientError("接口连接失败或超时；提交结果可能未知，请查询原 requestId，勿新建任务。") from None
        if len(raw) > MAX_RESPONSE:
            raise ClientError("接口响应超出大小限制。")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ClientError("接口未返回有效 JSON；请核对服务状态。") from None
        if not isinstance(result, dict):
            raise ClientError("接口返回格式无效。")
        return result

    def issue_session(self, verified_identity, *, ttl=28800):
        """Call only after backend authentication; this method does not verify OAuth."""
        if (not isinstance(verified_identity, dict)
                or set(verified_identity) != {"appId", "tenantKey", "openId", "name"}
                or any(not isinstance(v, str) or not v for v in verified_identity.values())):
            raise ValueError("identity 只能包含后端已验证的 appId、tenantKey、openId、name。")
        if type(ttl) is not int or not 1 <= ttl <= 28800:
            raise ValueError("ttl 必须为 1–28800 的整数，并不超过上游会话剩余时间。")
        result = self._call("POST", "/sessions", body={"identity": verified_identity, "ttl": ttl})
        if not isinstance(result.get("sessionId"), str) or not SESSION_ID.fullmatch(result["sessionId"]):
            raise ClientError("接口未返回有效共享会话。")
        return result

    def revoke_session(self, session_id):
        _session(session_id)
        result = self._call("POST", "/sessions/revoke", body={"sessionId": session_id})
        if result.get("revoked") is not True:
            raise ClientError("接口未确认共享会话已撤销。")
        return result

    def search_apps(self, session_id, *, query="", limit=20, offset=0):
        _session(session_id)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query 最多 200 字符。")
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError("limit 为 1–20、offset 为 0–1000000 的整数。")
        result = self._call("POST", "/apps/search", body={"sessionId": session_id, "query": query,
                                                          "limit": limit, "offset": offset})
        next_offset = result.get("nextOffset")
        if (not isinstance(result.get("items"), list) or "nextOffset" not in result
                or type(result.get("total")) is not int or result["total"] < 0
                or result.get("limit") != limit or result.get("offset") != offset
                or (next_offset is not None and (type(next_offset) is not int or not offset < next_offset <= 1000000))):
            raise ClientError("搜索返回格式或分页无效；未将错误当作空结果。")
        return result

    def submit(self, request_id, deployment, session_id):
        """One POST only. Caller persists identity/payload/key and authorizes mutation."""
        _request_id(request_id)
        _session(session_id)
        if (not isinstance(deployment, dict) or not {"repository", "subdomain"} <= set(deployment)
                or set(deployment) - {"repository", "subdomain", "ref", "appId"}
                or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 for v in deployment.values())):
            raise ValueError("deployment 字段无效。")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", deployment["subdomain"]):
            raise ValueError("subdomain 必须为 1–7 位小写字母、数字或中划线，首尾为字母或数字。")
        result = self._call("POST", "/requests", body={"deployment": {"ref": "main", **deployment},
                                                       "sessionId": session_id}, request_id=request_id)
        return self._request_result(result, request_id)

    @staticmethod
    def _request_result(result, request_id):
        if (result.get("requestId") != request_id
                or result.get("status") not in TERMINAL | {"pending", "submitted"}):
            raise ClientError("接口返回的请求 ID 或状态无效；保留原 ID 核查。")
        return result

    def get_request(self, request_id):
        _request_id(request_id)
        return self._request_result(self._call("GET", "/requests/" + request_id), request_id)

    def wait_for_request(self, request_id, *, max_seconds=60):
        """Bounded polling; raises WaitTimeout with last_request, never submits."""
        _request_id(request_id)
        if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError("max_seconds 必须是有限正数。")
        deadline, last = time.monotonic() + max_seconds, None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(request_id, last)
            last = self._request_result(self._call("GET", "/requests/" + request_id,
                                                   timeout=min(self.timeout, remaining)), request_id)
            if last["status"] in TERMINAL:
                return last
            delay = last.get("pollAfterSeconds", 5)
            if type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0:
                delay = 5
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(request_id, last)
            time.sleep(min(max(1, min(delay, 300)), remaining))


def main():
    parser = argparse.ArgumentParser(description="Awesome Deploy 只读查询（凭证从环境/受限文件读取）")
    parser.add_argument("--origin", help="受控适配服务 origin，默认读取 AWESOME_INTAKE_ORIGIN 或同机地址")
    parser.add_argument("--timeout", type=float, default=10, help="单次网络超时秒数")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="用 AWESOME_SHARED_SESSION_ID 查询一页可部署项目")
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    status = commands.add_parser("status", help="查询已受理请求")
    status.add_argument("request_id")
    wait = commands.add_parser("wait", help="有期限地等待已受理请求")
    wait.add_argument("request_id")
    wait.add_argument("--max-seconds", type=float, default=60)
    args = parser.parse_args()
    try:
        client = AwesomeDeployClient(args.origin, timeout=args.timeout)
        if args.command == "search":
            result = client.search_apps(os.environ.get("AWESOME_SHARED_SESSION_ID", ""),
                                        query=args.query, limit=args.limit, offset=args.offset)
        elif args.command == "status":
            result = client.get_request(args.request_id)
        else:
            result = client.wait_for_request(args.request_id, max_seconds=args.max_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WaitTimeout as error:
        print(str(error), file=sys.stderr)
        if error.last_request is not None:
            print(json.dumps(error.last_request, ensure_ascii=False, indent=2))
        return 2
    except (ClientError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
