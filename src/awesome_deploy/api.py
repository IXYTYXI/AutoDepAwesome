"""HTTP submission is durable and quick; only the worker executes deployments."""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import verify_credentials
from .ai_advice import AdviceService
from .config import Settings, validate_identity_settings
from .diagnostics import ERROR_CODES, PHASE_LABELS, localize_error, localize_event
from .deployment_secrets import DeploymentSecrets
from .errors import DeployError
from .manifest import load_manifest, schema_document
from .management import public_application, public_operation
from .source import validate_ref, validate_repository
from .store import Store

STATIC = Path(__file__).parent / "static"
ANALYSIS_POLL_WAIT = 8.0


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: StrictStr
    ref: StrictStr = "main"
    subdomain: StrictStr
    appId: StrictStr | None = None


class Preflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: StrictStr


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll: StrictBool = False


class OfflineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedReleaseId: StrictStr
    dataPolicy: Literal["retain", "delete"] = "retain"
    deleteConfirmation: StrictStr | None = None


class SecretConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[StrictStr, StrictStr]


class SecretRotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: StrictStr
    expected_current_version: StrictStr


class SecretRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictStr
    expected_current_version: StrictStr
    reason: StrictStr


class MemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["maintainer", "viewer"]


class OwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    userId: StrictStr


class UserPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    canDeploy: StrictBool | None = None


class RepositoryCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: StrictBool


class ApiCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    appId: StrictStr | None = None
    allowCreate: StrictBool = False
    allowAnyApp: StrictBool = False
    days: StrictInt = 30


_RUNTIME_NETWORK_TEXT = {
    "internal": "运行服务使用 internal 隔离网络，不能直连外部模型 API。缺少策略字段的旧部署保持此模式，不会自动升级。",
    "public-https-v1": "允许直连公网 IPv4 的 TCP 443，并使用平台受控 DNS；不进行 TLS 检查，也不提供域名白名单。私网、宿主机、元数据地址、IPv6 及 HTTP 80 等其他业务外连端口仍禁止访问。",
    "unknown": "无法确认本次部署固定的运行网络模式，请运维核验；不要假定已开放外部网络访问。",
}


def _frozen_runtime_network(job):
    snapshot = job.get("snapshot")
    if snapshot is None or (isinstance(snapshot, dict) and "runtime_network" not in snapshot):
        return "internal"
    policy = snapshot.get("runtime_network") if isinstance(snapshot, dict) else None
    mode = policy.get("mode") if isinstance(policy, dict) else None
    return mode if isinstance(mode, str) and mode in {"internal", "public-https-v1"} else "unknown"


def advice_context(job):
    """Only fixed diagnostic text and public platform rules may leave this server."""
    runtime_network = _frozen_runtime_network(job)
    raw = job.get("error") or {}
    code = raw.get("code")
    safe_error = localize_error({**raw, "code": code if code in ERROR_CODES else "UNKNOWN_ERROR"})
    return {
        "runtime_network": {"mode": runtime_network},
        "failure": {
            "code": safe_error["code"],
            "phase": PHASE_LABELS.get(job.get("phase"), "未知阶段"),
            "summary": safe_error["message"],
            "field": safe_error.get("path", ""),
            "suggested_steps": safe_error["steps"],
        },
        "platform_rules": [
            "仓库根目录必须包含 UTF-8 的 awesome.yaml，大小不超过 256 KiB，支持 schemaVersion: 1 和 2。",
            "schemaVersion: 2 的 storage 必须声明正整数 sizeMiB 和 maxInodes，且主机启用并核验 XFS 后端；"
            "旧版清单不可声明新字段。不能为规避硬配额而降级清单；现有额度只通过显式扩容提高，回滚不缩容。",
            "当前支持 Web 服务和受控持久存储，服务必须为 HTTP 服务；桌面应用不能直接作为 Web 服务部署。",
            "构建命令默认不能访问网络；基础镜像由平台获取并固定摘要。运行外连能力不改变构建网络限制。",
            "以下运行网络说明仅依据本次部署固定的策略，不依据当前平台开关：" + _RUNTIME_NETWORK_TEXT[runtime_network],
            "不支持数据库服务类型、后台任务或任意宿主机访问。持久卷通过 services.<服务名>.storage 声明 name 和固定 mountPath: /data；每服务一个独立卷，禁止共享卷、任意驱动和主机挂载。",
            "已有 storage 不得删除、改名或移动；持久存储更新使用 Recreate，会短暂停机，先确认旧版停止才启动候选。",
            "持久存储恢复必须先确认候选已停止和清理，再恢复旧版；应用回滚不会撤销数据写入或回滚数据格式，需备份及向后兼容。",
            "应用以非 root 用户运行，根文件系统只读，必须监听 0.0.0.0 和清单指定的端口。",
            "healthcheck.path 应返回成功状态；路由可通过 stripPrefix 去除指定前缀。",
            "更新时旧版本与候选版本同时占用配额；不得绕过身份检查或直接删除保留资源。",
        ],
        "evidence_scope": "仅提供标准化错误摘要，未发送仓库内容、原始日志或环境变量；具体项目事实尚待核实。",
    }


def create_app(settings: Settings, store: Store | None = None, *, identity_provider=None) -> FastAPI:
    validate_identity_settings(settings)
    store = store or Store(settings)
    identity = access = None
    if settings.auth_mode == "feishu":
        from .identity import IdentityStore
        from .access import AccessStore
        identity = IdentityStore(settings, provider=identity_provider)
        access = AccessStore(settings)
        access.identity = identity

    @asynccontextmanager
    async def lifespan(app):
        yield
        if identity:
            identity.provider.close()

    app = FastAPI(title="Awesome Deploy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.identity, app.state.access = identity, access
    if identity:
        from .api_credentials import ApiCredentialStore
        app.state.api_credentials = ApiCredentialStore(settings)
    else:
        app.state.api_credentials = None
    app.state.advice = AdviceService(settings)
    app.state.deployment_secrets = DeploymentSecrets(settings, store)
    app.state.vault_service = None
    if getattr(settings, "secret_backend", "legacy") == "encrypted":
        from .vault_service import VaultService
        app.state.vault_service = VaultService(settings, store)
    public = urlsplit(settings.public_url)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[public.hostname or "127.0.0.1"])
    basic = HTTPBasic(auto_error=False)

    def authenticated(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        if identity:
            from .identity import SESSION_COOKIE
            actor = identity.session(request.cookies.get(SESSION_COOKIE))
            if actor is None:
                raise HTTPException(401, "登录已失效，请重新使用飞书登录。")
            request.state.actor = actor
            return actor
        if not credentials or not verify_credentials(
            settings.auth_file, credentials.username, credentials.password
        ):
            raise HTTPException(
                401,
                "请使用部署控制台账号登录。",
                headers={"WWW-Authenticate": 'Basic realm="Awesome Deploy", charset="UTF-8"'},
            )
        request.state.actor = None
        return None

    def write_access(request: Request, actor=Depends(authenticated)):
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.public_url.rstrip("/"):
            raise HTTPException(403, "请求来源不被允许。")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "拒绝跨站写操作。")
        if identity:
            from .identity import SESSION_COOKIE, csrf_token
            token = request.headers.get("x-csrf-token", "")
            if (origin != settings.public_url.rstrip("/") or not re.fullmatch(r"[0-9a-f]{64}", token)
                    or not hmac.compare_digest(token, csrf_token(request.cookies.get(SESSION_COOKIE, "")))):
                raise HTTPException(403, "页面验证已失效，请刷新后重试。")
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise HTTPException(415, "请使用 application/json。")
        return actor

    def automation_access(request: Request):
        if app.state.api_credentials is None:
            raise HTTPException(404)
        values = request.headers.getlist('authorization')
        if len(values) != 1 or not values[0].startswith('Bearer '):
            raise HTTPException(401, '请提供有效的自动化 API 凭证。')
        authenticated_credential = app.state.api_credentials.authenticate(values[0][7:])
        if authenticated_credential is None:
            raise HTTPException(401, '自动化 API 凭证无效或已失效。')
        origin = request.headers.get('origin')
        if (origin is not None and origin != settings.public_url.rstrip('/')
                or request.headers.get('sec-fetch-site') == 'cross-site'):
            raise HTTPException(403, '请求来源不被允许。')
        actor, credential = authenticated_credential
        actor = identity.authorize_user(actor['id'], expected_revision=actor['revision'])
        request.state.actor, request.state.credential = actor, credential
        return actor, credential

    def fresh(actor):
        if identity and time.time() - actor["authenticated_at"] > 600:
            raise DeployError("AUTH_REAUTH_REQUIRED", "请重新使用飞书登录后再进行此操作。")

    def feishu_only():
        if identity is None:
            raise HTTPException(404)

    def vault_enabled():
        return identity is not None and getattr(settings, "secret_backend", "legacy") == "encrypted"

    def vault_management():
        if not vault_enabled() or app.state.vault_service is None:
            raise HTTPException(404)
        return app.state.vault_service

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        if identity and error.status_code == 401 and request.url.path in {
            "/", "/manage", "/members", "/document", "/api-access"
        }:
            return_path = request.url.path
            if return_path == "/document" and request.query_params.get("doc") in {
                "catalog", "overview", "api", "intake", "protocol", "protocol-legacy", "audit"
            } and len(request.query_params.getlist("doc")) == 1:
                return_path += "?doc=" + request.query_params["doc"]
            login_url = "/login?error=session"
            if return_path != "/":
                login_url += "&return=" + quote(return_path, safe="")
            return RedirectResponse(login_url, status_code=303)
        return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)

    @app.get("/login")
    def login_page():
        feishu_only()
        return FileResponse(STATIC / "login.html")

    @app.get("/auth/feishu/start")
    def start_login(request: Request):
        feishu_only()
        from .identity import OAUTH_COOKIE
        attempt = identity.begin_login(request.query_params.get("return", "/"))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt["verifier"].encode()).digest()).rstrip(b"=").decode()
        response = RedirectResponse(identity.provider.authorize_url(attempt["state"], challenge), status_code=303)
        response.set_cookie(OAUTH_COOKIE, attempt["browser"], max_age=300, secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/auth/feishu/callback")
    def finish_login(request: Request):
        feishu_only()
        from .feishu import FeishuDenied, FeishuUnavailable
        from .identity import OAUTH_COOKIE, SESSION_COOKIE, SESSION_MAX_AGE
        error = None
        verified_attempt = False
        try:
            # Reject duplicates instead of allowing proxy/framework parameter disagreement.
            if any(len(request.query_params.getlist(key)) > 1 for key in ("state", "code", "error")):
                raise DeployError("AUTH_STATE_INVALID", "登录参数无效。")
            attempt = identity.consume_login(request.query_params.get("state"), request.cookies.get(OAUTH_COOKIE))
            verified_attempt = True
            code = request.query_params.get("code", "")
            if request.query_params.get("error"):
                error = "cancelled"
            elif not code or len(code) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in code):
                error = "invalid"
            else:
                actor = identity.login_user(identity.provider.exchange(code, attempt["verifier"]))
                token = identity.create_session(actor["id"], expected_epoch=attempt["auth_epoch"])
                identity.logout(request.cookies.get(SESSION_COOKIE))
        except FeishuDenied:
            error = "denied"
        except FeishuUnavailable:
            error = "unavailable"
        except DeployError as exc:
            error = "expired" if exc.code == "AUTH_STATE_INVALID" else "denied"
        if error:
            if verified_attempt:
                identity.login_failure(error)
            response = RedirectResponse("/login?error=" + error, status_code=303)
        else:
            response = RedirectResponse(attempt["return_path"], status_code=303)
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                                secure=True, httponly=True, samesite="lax")
        response.delete_cookie(OAUTH_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/auth/session")
    def session_info(request: Request, actor=Depends(authenticated)):
        if identity is None:
            return {"mode": "basic", "authenticated": True, "role": "admin", "canDeploy": True,
                    "capabilities": {"vault_enabled": False}}
        from .identity import SESSION_COOKIE, csrf_token
        return {"mode": "feishu", "authenticated": True, "userId": actor["id"],
                "displayName": actor["name"], "role": "admin" if actor["is_admin"] else "member",
                "canDeploy": bool(actor["can_deploy"]), "csrfToken": csrf_token(request.cookies[SESSION_COOKIE]),
                "capabilities": {"vault_enabled": vault_enabled()}}

    @app.post("/auth/logout", dependencies=[Depends(write_access)])
    def logout(request: Request):
        feishu_only()
        from .identity import SESSION_COOKIE, OAUTH_COOKIE
        identity.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
        response.delete_cookie(OAUTH_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.middleware("http")
    async def headers_and_limits(request, call_next):
        try:
            if int(request.headers.get("content-length", "0")) > 131072:
                return JSONResponse(
                    {"error": {"code": "REQUEST_TOO_LARGE", "message": "请求超过 128 KiB。"}}, 413
                )
        except ValueError:
            return JSONResponse({"error": {"code": "INVALID_LENGTH", "message": "请求长度错误。"}}, 400)
        # Enforce the actual size too, including chunked input without Content-Length.
        if request.method in ("POST", "PUT", "PATCH"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 131072:
                    return JSONResponse(
                        {"error": {"code": "REQUEST_TOO_LARGE", "message": "请求超过 128 KiB。"}}, 413
                    )
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        policy = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Content-Security-Policy"] = policy
        return response

    @app.exception_handler(DeployError)
    async def deployment_error(request, error):
        code = error.code
        status = (
            403 if code in {"ACCESS_DENIED", "AUTH_REAUTH_REQUIRED"}
            else 503 if code == "IDENTITY_UNAVAILABLE"
            else 429 if code == "AUTH_BUSY"
            else 429
            if code in {"AI_BUSY", "AI_RATE_LIMITED"}
            else 504
            if code == "AI_TIMEOUT"
            else 503
            if code.startswith("AI_")
            else 409
            if code in {"DIAGNOSIS_NOT_AVAILABLE", "OFFLINE_RELEASE_CHANGED", "OFFLINE_STATE",
                        "SECRET_CONFIGURATION_NOT_AVAILABLE", "SECRET_ALREADY_CONFIGURED", "SECRET_UNAVAILABLE",
                        "STORAGE_IN_USE", "LAST_ADMIN_REQUIRED", "SECRET_REVOKED", "SECRET_REVOCATION_STATE"}
            else 404
            if code == "NOT_FOUND"
            else 409
            if any(x in code for x in ("CONFLICT", "EXISTS", "BUSY", "CAPACITY", "EXHAUSTED", "RECOVERY"))
            else 422
        )
        return JSONResponse({"error": localize_error(error.as_dict())}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request, error):
        # Pydantic errors can include the supplied values. Never echo them.
        return JSONResponse({"error": localize_error({"code": "INVALID_REQUEST"})}, status_code=422)

    def public_job(job):
        data = job["input"]
        current = store.active_release(job["app_id"]) if job.get("app_id") and job["status"] == "succeeded" else None
        active = bool(current and current["id"] == job["id"])
        terminal = job["status"] in {"succeeded", "failed", "recovery_required", "cancelled"}
        return {
            "id": job["id"],
            "appId": job.get("app_id"),
            "status": job["status"],
            "statusLabel": {"queued": "已排队", "running": "执行中", "succeeded": "部署成功",
                            "failed": "部署失败", "recovery_required": "需要处理恢复状态",
                            "cancelled": "已取消"}.get(job["status"], "处理中"),
            "phase": job["phase"],
            "phaseLabel": PHASE_LABELS.get(job["phase"], "处理中"),
            "terminal": terminal,
            "pollAfterSeconds": 0 if terminal else 5,
            "repository": data["repository"],
            "ref": data["ref"],
            "subdomain": data["subdomain"],
            "commit": (job.get("snapshot") or {}).get("commit") or job.get("execution", {}).get("commit"),
            "url": f"https://{data['subdomain']}.{settings.base_domain}"
            if active
            else None,
            "active": active,
            "error": localize_error(job.get("error"), job["phase"]),
            "createdAt": job.get("created_at"),
            "updatedAt": job.get("updated_at"),
        }

    def permissions(actor, app_id):
        if access is None:
            return None
        role = access.role(actor, app_id)
        writable = role in {"admin", "owner", "maintainer"} and bool(actor["can_deploy"])
        with access._connection() as db:
            assigned = db.execute("SELECT 1 FROM app_access WHERE app_id=?", (app_id,)).fetchone() is not None
        return {"viewDetails": role in {"admin", "owner", "maintainer"}, "deploy": writable,
                "configureSecrets": writable, "offline": writable and role in {"admin", "owner"},
                "manageSecrets": writable and vault_enabled(), "rotateSecrets": writable and vault_enabled(),
                "revokeSecrets": writable and vault_enabled() and role in {"admin", "owner"},
                "manageMembers": role in {"admin", "owner"} and assigned,
                "assignOwner": role == "admin" and not assigned,
                "manageRepositoryCredentials": role == "admin" and assigned}

    def visible_job(actor, job):
        if access is None:
            return True
        try:
            access.require_job(actor, job, "detail")
            return True
        except DeployError as error:
            if error.code != "ACCESS_DENIED":
                raise
            return False

    def operation_view(actor, operation):
        result = public_operation(operation)
        if access and result is not None:
            result["retryKey"] = access.retry_key(actor, operation) if result["retryable"] else None
            result["retryable"] = bool(result["retryKey"])
        return result

    def application_view(actor, item):
        result = public_application(item, settings.base_domain)
        if access:
            role = access.require_app(actor, item["id"])
            if role == "viewer":
                result = {key: result[key] for key in ("id", "subdomain", "lastSubdomain", "domainBound",
                                                      "status", "url", "updatedAt")}
            else:
                result["operation"] = operation_view(actor, item.get("operation"))
            result.update(role=role, permissions=permissions(actor, item["id"]))
        return result

    @app.get("/", dependencies=[Depends(authenticated)])
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/manage", dependencies=[Depends(authenticated)])
    def manage():
        return FileResponse(STATIC / "manage.html")

    @app.get('/api-access', dependencies=[Depends(authenticated)])
    def api_access():
        feishu_only()
        return FileResponse(STATIC / 'api-credentials.html')

    @app.get("/document", dependencies=[Depends(authenticated)])
    def document():
        return FileResponse(STATIC / "document.html")

    @app.get("/document/content/{doc_id}", dependencies=[Depends(authenticated)])
    def document_content(doc_id: str):
        if doc_id not in {"catalog", "overview", "api", "intake", "protocol", "protocol-legacy", "audit"}:
            raise HTTPException(404)
        return FileResponse(STATIC / f"manual-{doc_id}.html", media_type="text/html; charset=utf-8")

    @app.get("/document/deployment-guide.txt", dependencies=[Depends(authenticated)])
    def deployment_guide():
        from .storage_status import quota_capability

        quota = quota_capability(settings)
        limits = quota.get("limits") or {}
        storage = limits.get("app_budget_mib")
        pool = limits.get("global_budget_mib")
        resource_text = f"每应用全部服务内存上限：{settings.max_app_memory_mib} MiB，按需使用，不固定预占。"
        resource_text += (f"\n每应用持久数据合计上限：{storage} MiB。" if storage
                          else "\n存储上限暂未确认，请在部署页面核对当前配置。")
        if pool:
            resource_text += f"\n平台共享存储池预算：{pool} MiB；不是单应用可用空间。"
        if quota.get("configured") and not quota.get("enabled"):
            resource_text += "\n存储配额状态待确认，请刷新部署页面或联系管理员。"
        body = (STATIC / "deployment-guide.txt").read_text().replace("{{RESOURCE_LIMITS}}", resource_text)
        return Response(body, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="awesome-deploy-guide.txt"',
        })

    @app.get("/members", dependencies=[Depends(authenticated)])
    def members_page():
        feishu_only()
        return FileResponse(STATIC / "members.html")

    @app.get("/assets/{name}")
    def asset(name: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        if identity is None:
            authenticated(request, credentials)
        if name not in ("app.js", "app.css", "manage.js", "manage.css", "auth.js", "auth.css",
                        "login.js", "members.js", "members.css", "vault.js", "vault.css",
                        "document.js", "document.css", "api-credentials.js", "api-credentials.css", "theme.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/api/v1/schema", dependencies=[Depends(authenticated)])
    def schema(version: int = 1):
        return schema_document(version)

    @app.get("/api/v1/capabilities", dependencies=[Depends(authenticated)])
    def capabilities():
        from .storage_status import quota_capability

        runtime_mode = getattr(settings, "runtime_network", "internal")
        public_egress = runtime_mode == "public-https-v1"
        return {
            "schemaVersion": 1,
            "schemaVersions": [1, 2],
            "serviceKinds": ["web"],
            "approval": False,
            "codeReview": False,
            "persistentStorage": True,
            "storage": {"driver": settings.storage_backend, "mountPath": "/data",
                        "perService": 1, "updates": "recreate"},
            "storageHardQuota": quota_capability(settings),
            "resourceLimits": {"maxAppMemoryMiB": settings.max_app_memory_mib},
            "workers": False,
            "customEgress": False,
            "buildNetwork": settings.build_network,
            "runtimeNetwork": {
                "mode": runtime_mode,
                "enabled": public_egress,
                "publicIPv4TcpPorts": [443] if public_egress else [],
                "controlledDns": public_egress,
                "tlsInspection": False,
                "domainAllowlist": False,
                "appliesTo": "new-deployments",
                "description": _RUNTIME_NETWORK_TEXT.get(runtime_mode, _RUNTIME_NETWORK_TEXT["unknown"]),
            },
            "baseDomain": settings.base_domain,
            "aiAdvice": {"configured": app.state.advice.configured},
            "secretVault": {"enabled": vault_enabled(), "versionManagement": vault_enabled()},
            "applicationManagement": {"offline": True, "retainsData": True,
                                      "dataPolicies": ["retain", "delete"], "deletesStoredSecrets": True},
        }

    @app.get("/api/v1/openapi.json", dependencies=[Depends(authenticated)])
    def openapi():
        return app.openapi()

    @app.post("/api/v1/preflight", dependencies=[Depends(write_access)])
    def preflight(body: Preflight):
        return {
            "manifest": load_manifest(body.manifest, settings),
            "scope": "configuration_only",
            "message": "仅完成配置校验；仓库、镜像、秘密、容量和服务器能力由部署执行器继续检查。",
        }

    def deployment_input(body: Submission, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise HTTPException(400, "必须提供 1–128 字符的 Idempotency-Key。")
        domain = body.subdomain
        if (
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,5}[a-z0-9])?", domain)
            or domain in settings.reserved_names
            or any(word in domain for word in settings.banned_words)
        ):
            raise DeployError(
                "INVALID_SUBDOMAIN",
                "域名前缀须为 1–7 位小写字母、数字或中划线，且不能使用禁词或保留名称。",
                "subdomain",
            )
        if body.appId is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", body.appId):
            raise DeployError("INVALID_APP_ID", "应用编号格式错误。", "appId")
        payload = body.model_dump(exclude_none=True)
        payload["repository"] = validate_repository(body.repository, settings)
        payload["ref"] = validate_ref(body.ref)
        return payload, key

    @app.post("/api/v1/deployments", status_code=202, dependencies=[Depends(write_access)])
    def submit(body: Submission, request: Request):
        payload, key = deployment_input(body, request)
        return public_job(store.enqueue(payload, key, **({"actor": request.state.actor} if access else {})))

    @app.post('/api/v1/automation/deployments', status_code=202)
    def automation_submit(body: Submission, request: Request, identity_and_credential=Depends(automation_access)):
        if not request.headers.get('content-type', '').lower().startswith('application/json'):
            raise HTTPException(415, '请使用 application/json。')
        actor, credential = identity_and_credential
        payload, key = deployment_input(body, request)
        return public_job(store.enqueue(payload, key, actor=actor, credential_id=credential['id']))

    from .team_api import install_team_routes
    install_team_routes(app, settings, automation_access=automation_access, deployment_input=deployment_input,
                        submission_model=Submission, public_job=public_job)

    @app.get('/api/v1/automation/deployments/{job_id}')
    def automation_deployment(job_id: str, identity_and_credential=Depends(automation_access)):
        actor, credential = identity_and_credential
        job = store.get(job_id)
        access.require_credential_job(actor, credential['id'], job)
        return public_job(job)

    @app.get("/api/v1/deployments", dependencies=[Depends(authenticated)])
    def deployments(actor=Depends(authenticated)):
        jobs = store.list_jobs(actor=actor) if access else store.list_jobs()
        return {"items": [public_job(j) for j in jobs if visible_job(actor, j)]}

    @app.get("/api/v1/deployments/{job_id}", dependencies=[Depends(authenticated)])
    def deployment(job_id: str, actor=Depends(authenticated)):
        job = store.get(job_id)
        if access:
            access.require_job(actor, job)
        return public_job(job)

    @app.get('/api/v1/api-credentials')
    def own_api_credentials(actor=Depends(authenticated)):
        feishu_only()
        return {'items': app.state.api_credentials.list(owner_user_id=actor['id'])}

    @app.post('/api/v1/api-credentials', status_code=201)
    def create_api_credential(body: ApiCredentialRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        return app.state.api_credentials.issue(
            actor['id'], body.name, kind='person', app_id=body.appId,
            allow_create=body.allowCreate, allow_any_app=body.allowAnyApp, self_managed=True,
            expires_days=body.days)

    @app.delete('/api/v1/api-credentials/{credential_id}')
    def revoke_api_credential(credential_id: str, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        app.state.api_credentials.revoke(credential_id, actor=actor)
        return {'ok': True}

    @app.get("/api/v1/deployments/{job_id}/events", dependencies=[Depends(authenticated)])
    def events(job_id: str, actor=Depends(authenticated)):
        job = store.get(job_id)
        if access:
            access.require_job(actor, job)
        return {"items": [localize_event(event) for event in store.events(job_id)]}

    @app.get("/api/v1/deployments/{job_id}/secrets", dependencies=[Depends(authenticated)])
    def deployment_secret_requirements(job_id: str, actor=Depends(authenticated)):
        if access:
            access.require_job(actor, store.get(job_id), "secrets")
        return app.state.deployment_secrets.describe(job_id, **({"actor": actor} if access else {}))

    @app.post("/api/v1/deployments/{job_id}/secrets", dependencies=[Depends(write_access)])
    def save_deployment_secrets(job_id: str, body: SecretConfigurationRequest, actor=Depends(write_access)):
        if access:
            access.require_job(actor, store.get(job_id), "secrets")
        return app.state.deployment_secrets.save(job_id, body.values, **({"actor": actor} if access else {}))

    @app.post("/api/v1/deployments/{job_id}/analyze", dependencies=[Depends(write_access)])
    async def analyze(job_id: str, body: AnalysisRequest, actor=Depends(write_access)):
        job = store.get(job_id)
        if access:
            access.require_job(actor, job, "deploy")
        if job["status"] not in {"failed", "recovery_required"} or not job.get("error"):
            raise DeployError("DIAGNOSIS_NOT_AVAILABLE", "只有失败或需要恢复的任务可以分析。")
        try:
            return await asyncio.wait_for(
                app.state.advice.analyze(advice_context(job), **({"poll_only": True} if body.poll else {})),
                timeout=ANALYSIS_POLL_WAIT,
            )
        except TimeoutError:
            # AdviceService shields and coalesces the provider task across short polls.
            return JSONResponse({"status": "pending"}, status_code=202, headers={"Retry-After": "1"})

    @app.get("/api/v1/apps/{app_id}/secrets")
    def app_secrets(app_id: str, actor=Depends(authenticated)):
        return vault_management().describe(app_id, actor=actor)

    @app.post("/api/v1/apps/{app_id}/secrets/{name}/rotate")
    def rotate_secret(app_id: str, name: str, body: SecretRotationRequest, actor=Depends(write_access)):
        service = vault_management()
        fresh(actor)
        return service.rotate(app_id, name, body.value, body.expected_current_version, actor=actor)

    @app.post("/api/v1/apps/{app_id}/secrets/{name}/revoke", status_code=202)
    def revoke_secret(app_id: str, name: str, body: SecretRevocationRequest, actor=Depends(write_access)):
        service = vault_management()
        fresh(actor)
        return service.public_operation(service.revoke(app_id, name, body.version,
            body.expected_current_version, body.reason, actor=actor))

    @app.get("/api/v1/apps", dependencies=[Depends(authenticated)])
    def apps(actor=Depends(authenticated)):
        items = store.apps()
        if access:
            items = [{**access.project_app(actor, item), "permissions": permissions(actor, item["id"])}
                     for item in items if access.can_view(actor, item["id"])]
        return {"items": items}

    @app.get("/api/v1/management", dependencies=[Depends(authenticated)])
    def management(actor=Depends(authenticated)):
        snapshot = store.management_snapshot()
        return {
            "apps": [application_view(actor, item) for item in snapshot["apps"]
                     if access is None or access.can_view(actor, item["id"])],
            "deployments": [public_job(item) for item in snapshot["deployments"] if visible_job(actor, item)],
            "offlineOperations": [operation_view(actor, item) for item in snapshot["offline_operations"]
                                  if access is None or access.role(actor, item["app_id"]) in {"admin", "owner", "maintainer"}],
        }

    @app.post("/api/v1/apps/{app_id}/offline", status_code=202, dependencies=[Depends(write_access)])
    def offline(app_id: str, body: OfflineRequest, request: Request):
        if access:
            access.require_app(request.state.actor, app_id, "delete" if body.dataPolicy == "delete" else "offline")
            if body.dataPolicy == "delete":
                fresh(request.state.actor)
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise HTTPException(400, "必须提供 1–128 字符的 Idempotency-Key。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id):
            raise DeployError("INVALID_APP_ID", "应用编号格式错误。", "appId")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body.expectedReleaseId):
            raise DeployError("OFFLINE_STATE", "下线目标版本格式错误。")
        return operation_view(request.state.actor, store.enqueue_offline(
            app_id, body.expectedReleaseId, key,
            data_policy=body.dataPolicy, delete_confirmation=body.deleteConfirmation,
            **({"actor": request.state.actor} if access else {}),
        ))

    @app.get("/api/v1/offline-operations/{operation_id}", dependencies=[Depends(authenticated)])
    def offline_operation(operation_id: str, actor=Depends(authenticated)):
        operation = store.get_offline(operation_id)
        if access:
            access.require_offline(actor, operation)
        return operation_view(actor, operation)

    @app.get("/api/v1/apps/{app_id}/members")
    def members(app_id: str, actor=Depends(authenticated)):
        feishu_only()
        return {"items": access.list_members(actor, app_id), "permissions": permissions(actor, app_id),
                "repositoryCredentialsApproved": access.repository_credentials_allowed(app_id),
                "repositoryCredentialsConfigured": bool(settings.git_askpass)}

    @app.put("/api/v1/apps/{app_id}/repository-credentials")
    def repository_credentials(app_id: str, body: RepositoryCredentialsRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        if body.approved and not settings.git_askpass:
            raise HTTPException(409, "请先由运维配置平台仓库读取凭证。")
        access.set_credentials_approved(actor, app_id, body.approved)
        return {"ok": True}

    @app.put("/api/v1/apps/{app_id}/members/{user_id}")
    def set_member(app_id: str, user_id: str, body: MemberRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        access.set_member(actor, app_id, user_id, body.role)
        return {"ok": True}

    @app.delete("/api/v1/apps/{app_id}/members/{user_id}")
    def remove_member(app_id: str, user_id: str, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        access.remove_member(actor, app_id, user_id)
        return {"ok": True}

    @app.post("/api/v1/apps/{app_id}/transfer")
    def transfer(app_id: str, body: OwnerRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        access.transfer_owner(actor, app_id, body.userId)
        return {"ok": True}

    @app.post("/api/v1/apps/{app_id}/owner")
    def assign_owner(app_id: str, body: OwnerRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        access.assign_owner(actor, app_id, body.userId)
        return {"ok": True}

    @app.get("/api/v1/users")
    def users(actor=Depends(authenticated)):
        feishu_only()
        if not actor["is_admin"]:
            raise HTTPException(403, "需要管理员权限。")
        return {"items": [{"userId": row["id"], "displayName": row["name"], "enabled": bool(row["enabled"]),
                           "canDeploy": bool(row["can_deploy"]), "role": "admin" if row["is_admin"] else "member"}
                          for row in identity.list_users()]}

    @app.patch("/api/v1/users/{user_id}")
    def set_user(user_id: str, body: UserPermissionsRequest, actor=Depends(write_access)):
        feishu_only()
        fresh(actor)
        if not actor["is_admin"]:
            raise HTTPException(403, "需要管理员权限。")
        identity.set_user(user_id, enabled=body.enabled, can_deploy=body.canDeploy, actor_id=actor["id"],
                          expected_actor_revision=actor["revision"])
        return {"ok": True}

    return app
