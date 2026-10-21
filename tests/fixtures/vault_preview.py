"""Local-only UI fixture: python3 tests/fixtures/vault_preview.py --port 18768.

Open /manage?fixture=normal|long|empty|maintainer|viewer|legacy|cas|uncertain|
reauth|read-error|read-loading|write-loading|retry. Dummy metadata only; writes never
leave this process and request bodies (including dummy secret values) are not logged.
Restart the process to reset a scenario. Not a production API implementation.
"""
from argparse import ArgumentParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote
import json
import time

STATIC = Path(__file__).resolve().parents[2] / "src/awesome_deploy/static"
ASSETS = {"app.css", "auth.css", "auth.js", "manage.css", "manage.js", "vault.css", "vault.js"}
STATES = {}
APP = {"id": "fixture-app-01", "subdomain": "chatstd", "status": "online", "activeReleaseId": "fixture-release", "repository": "https://github.com/example/model-studio.git", "ref": "main", "commit": "preview123", "canOffline": False, "permissions": {"deploy": True, "manageSecrets": True, "offline": False}}


def state(name):
    if name not in STATES:
        items = [{"name": "MODEL_API_KEY", "kind": "runtime_secret", "current_version": "runtime-v2", "versions": [{"version": "runtime-v2", "status": "active", "created_at": 1792530000}, {"version": "runtime-v1", "status": "active", "created_at": 1792440000}]}, {"name": "MASTER_KEY", "kind": "platform_managed_master", "data_generation": "studio-data-generation-1", "current_version": "master-v1", "versions": [{"version": "master-v1", "status": "active", "created_at": 1792440000}]}]
        if name == "long":
            items[0]["name"] = "MODEL_PROVIDER_CREDENTIAL_" + "LONG_SECRET_NAME_" * 8
            items[0]["current_version"] = "version-" + "abcdef0123456789" * 5
            items[0]["versions"] = [{"version": "version-" + "abcdef0123456789" * 5 + f"-{i}", "status": "revoked" if i > 4 else "active", "created_at": 1792530000-i*80000} for i in range(55)]
        STATES[name] = {"appId": APP["id"], "items": [] if name == "empty" else items, "operations": [], "can_rotate": True, "can_revoke": name != "maintainer", "writes": 0, "sessions": 0}
        if name == "retry":
            items[0]["versions"][1]["status"] = "revoked"
            STATES[name]["operations"] = [{"id": "fixture-revocation-retry", "app_id": APP["id"], "name": "MODEL_API_KEY", "version": "runtime-v1", "status": "recovery_required", "created_at": time.time(), "error": {"message": "模拟实例停用核验中断，请排除故障后重试。"}}]
    return STATES[name]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, status, value, content_type="application/json", cookie=None):
        body = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", "fixture=" + cookie + "; SameSite=Lax; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def scenario(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["fixture"].value if "fixture" in cookie else "normal"

    def do_GET(self):
        url = urlsplit(self.path)
        name = self.scenario()
        if url.path in {"/", "/manage"}:
            scenario = parse_qs(url.query).get("fixture", ["normal"])[0]
            if scenario not in {"normal", "long", "empty", "maintainer", "viewer", "legacy", "cas", "uncertain", "reauth", "read-error", "read-loading", "write-loading", "retry"}:
                scenario = "normal"
            return self.send(200, (STATIC / "manage.html").read_bytes(), "text/html; charset=utf-8", scenario)
        if url.path.startswith("/assets/") and url.path[8:] in ASSETS:
            asset = url.path[8:]
            return self.send(200, (STATIC / asset).read_bytes(), "text/css" if asset.endswith(".css") else "text/javascript")
        data = state(name)
        if url.path == "/auth/session":
            data["sessions"] += 1
            return self.send(200, {"authenticated": True, "mode": "feishu", "role": "member", "displayName": "界面验收（模拟账号）", "csrfToken": "fixture-csrf", "canDeploy": name != "viewer", "capabilities": {"vault_enabled": name != "legacy"}})
        if url.path == "/api/v1/management":
            app = {**APP, "permissions": {**APP["permissions"], "deploy": name != "viewer", "manageSecrets": name != "viewer"}}
            return self.send(200, {"apps": [app], "deployments": [], "offlineOperations": []})
        if url.path == f'/api/v1/apps/{APP["id"]}/secrets':
            if name == "read-error":
                return self.send(503, {"error": {"code": "SECRET_UNAVAILABLE", "message": "模拟读取失败，请刷新后再试。"}})
            if name == "read-loading":
                time.sleep(3)
            if data["operations"] and not (name == "retry" and data["writes"] == 0):
                op = data["operations"][0]
                age = time.time()-op.get("updated_at", op["created_at"])
                op["status"] = "succeeded" if age > 9 else "recovery_required" if age > 6 and name != "retry" else "running" if age > 2 else "queued"
            return self.send(200, {k: v for k, v in data.items() if k not in {"writes", "sessions"}})
        return self.send(404, {"error": {"message": "模拟页面不存在"}})

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return self.send(400, {"error": {"message": "请求格式错误"}})
        raw = None
        name = self.scenario()
        data = state(name)
        if name == "write-loading":
            time.sleep(4)
        data["writes"] += 1
        item = next((item for item in data["items"] if f'/secrets/{item["name"]}/' in path), None)
        if not item:
            return self.send(404, {"error": {"message": "找不到密钥"}})
        if name == "reauth" and data["sessions"] < 2:
            return self.send(403, {"error": {"code": "AUTH_REAUTH_REQUIRED", "message": "此操作需要近期认证。"}})
        if name == "cas" and data["writes"] == 1:
            item["current_version"] = "runtime-v3-changed-by-another-user"
            item["versions"].insert(0, {"version": item["current_version"], "status": "active", "created_at": time.time()})
            return self.send(409, {"error": {"code": "SECRET_VERSION_CONFLICT", "message": "其他成员已更新当前版本，请刷新并核对。"}})
        if path.endswith("/rotate"):
            item["current_version"] = "runtime-v" + str(data["writes"]+3)
            item["versions"].insert(0, {"version": item["current_version"], "status": "active", "created_at": time.time()})
            body.pop("value", None)
            if name == "uncertain" and data["writes"] == 1:
                return self.send(502, {"error": {"message": "模拟响应中断，无法确认请求结果。"}})
            return self.send(200, {"name": item["name"], "current_version": item["current_version"]})
        if path.endswith("/revoke"):
            existing = next((op for op in data["operations"] if op["name"] == item["name"] and op["version"] == body["version"]), None)
            if existing:
                existing.update(status="queued", updated_at=time.time(), error=None)
                return self.send(202, existing)
            operation = {"id": "fixture-revocation-01", "app_id": APP["id"], "name": item["name"], "version": body["version"], "status": "queued", "created_at": time.time()}
            for version in item["versions"]:
                if version["version"] == body["version"]:
                    version["status"] = "revoked"
            data["operations"].insert(0, operation)
            return self.send(202, operation)
        return self.send(404, {"error": {"message": "操作不存在"}})


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--port", type=int, default=18768)
    args = parser.parse_args()
    print(f"Vault UI fixture: http://127.0.0.1:{args.port}/manage?fixture=normal", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
