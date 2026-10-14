"""HTTP submission is durable and quick; only the worker executes deployments."""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import verify_credentials
from .ai_advice import AdviceService
from .config import Settings
from .diagnostics import ERROR_CODES, PHASE_LABELS, localize_error, localize_event
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
    ref: StrictStr
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
            "仓库根目录必须包含 UTF-8 的 awesome.yaml，大小不超过 256 KiB，schemaVersion 为 1。",
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


def create_app(settings: Settings, store: Store | None = None) -> FastAPI:
    store = store or Store(settings)
    app = FastAPI(title="Awesome Deploy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.advice = AdviceService(settings)
    public = urlsplit(settings.public_url)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[public.hostname or "127.0.0.1"])
    basic = HTTPBasic(auto_error=False)

    def authenticated(credentials: HTTPBasicCredentials | None = Depends(basic)):
        if not credentials or not verify_credentials(
            settings.auth_file, credentials.username, credentials.password
        ):
            raise HTTPException(
                401,
                "请使用部署控制台账号登录。",
                headers={"WWW-Authenticate": 'Basic realm="Awesome Deploy", charset="UTF-8"'},
            )
        return credentials.username

    def write_access(request: Request, actor: str = Depends(authenticated)):
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.public_url.rstrip("/"):
            raise HTTPException(403, "请求来源不被允许。")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "拒绝跨站写操作。")
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise HTTPException(415, "请使用 application/json。")
        return actor

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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(DeployError)
    async def deployment_error(request, error):
        code = error.code
        status = (
            429
            if code in {"AI_BUSY", "AI_RATE_LIMITED"}
            else 504
            if code == "AI_TIMEOUT"
            else 503
            if code.startswith("AI_")
            else 409
            if code in {"DIAGNOSIS_NOT_AVAILABLE", "OFFLINE_RELEASE_CHANGED", "OFFLINE_STATE"}
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
        return {
            "id": job["id"],
            "appId": job.get("app_id"),
            "status": job["status"],
            "phase": job["phase"],
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
        }

    @app.get("/", dependencies=[Depends(authenticated)])
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/manage", dependencies=[Depends(authenticated)])
    def manage():
        return FileResponse(STATIC / "manage.html")

    @app.get("/assets/{name}", dependencies=[Depends(authenticated)])
    def asset(name: str):
        if name not in ("app.js", "app.css", "manage.js", "manage.css"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/api/v1/schema", dependencies=[Depends(authenticated)])
    def schema():
        return schema_document()

    @app.get("/api/v1/capabilities", dependencies=[Depends(authenticated)])
    def capabilities():
        runtime_mode = getattr(settings, "runtime_network", "internal")
        public_egress = runtime_mode == "public-https-v1"
        return {
            "schemaVersion": 1,
            "serviceKinds": ["web"],
            "approval": False,
            "codeReview": False,
            "persistentStorage": True,
            "storage": {"driver": "local", "mountPath": "/data", "perService": 1, "updates": "recreate"},
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
            "applicationManagement": {"offline": True, "retainsData": True},
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

    @app.post("/api/v1/deployments", status_code=202, dependencies=[Depends(write_access)])
    def submit(body: Submission, request: Request):
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
        return public_job(store.enqueue(payload, key))

    @app.get("/api/v1/deployments", dependencies=[Depends(authenticated)])
    def deployments():
        return {"items": [public_job(j) for j in store.list_jobs()]}

    @app.get("/api/v1/deployments/{job_id}", dependencies=[Depends(authenticated)])
    def deployment(job_id: str):
        return public_job(store.get(job_id))

    @app.get("/api/v1/deployments/{job_id}/events", dependencies=[Depends(authenticated)])
    def events(job_id: str):
        store.get(job_id)
        return {"items": [localize_event(event) for event in store.events(job_id)]}

    @app.post("/api/v1/deployments/{job_id}/analyze", dependencies=[Depends(write_access)])
    async def analyze(job_id: str, body: AnalysisRequest):
        job = store.get(job_id)
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

    @app.get("/api/v1/apps", dependencies=[Depends(authenticated)])
    def apps():
        return {"items": store.apps()}

    @app.get("/api/v1/management", dependencies=[Depends(authenticated)])
    def management():
        snapshot = store.management_snapshot()
        return {
            "apps": [public_application(item, settings.base_domain) for item in snapshot["apps"]],
            "deployments": [public_job(item) for item in snapshot["deployments"]],
            "offlineOperations": [public_operation(item) for item in snapshot["offline_operations"]],
        }

    @app.post("/api/v1/apps/{app_id}/offline", status_code=202, dependencies=[Depends(write_access)])
    def offline(app_id: str, body: OfflineRequest, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise HTTPException(400, "必须提供 1–128 字符的 Idempotency-Key。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id):
            raise DeployError("INVALID_APP_ID", "应用编号格式错误。", "appId")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body.expectedReleaseId):
            raise DeployError("OFFLINE_STATE", "下线目标版本格式错误。")
        return public_operation(store.enqueue_offline(app_id, body.expectedReleaseId, key))

    @app.get("/api/v1/offline-operations/{operation_id}", dependencies=[Depends(authenticated)])
    def offline_operation(operation_id: str):
        return public_operation(store.get_offline(operation_id))

    return app
