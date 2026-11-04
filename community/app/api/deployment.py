import asyncio
import json
import logging

from awesome_deploy.errors import DeployError
from fastapi import APIRouter, Header, Query, Request
from pydantic import Field
from redis.exceptions import RedisError
from starlette.responses import JSONResponse

from app.api.dependencies import CurrentUser
from app.schemas.common import Envelope, InputModel, success
from app.schemas.deployment import DeploymentJob, DeploymentOptions

router = APIRouter(prefix="/api/v1/deployment", tags=["一键部署"])


class Submit(InputModel):
    repository: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=7)
    app_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


def service(request):
    value = getattr(request.app.state, "deployment", None)
    if value is None:
        raise DeployError("EXECUTOR_UNAVAILABLE", "部署模块未启用。")
    return value


async def deploy_error(request, exc):
    from app.core.error_codes import ErrorCode
    from app.core.errors import error_response
    from app.schemas.common import ErrorDetail

    code = (
        ErrorCode.FORBIDDEN
        if exc.code == "ACCESS_DENIED"
        else ErrorCode.NOT_FOUND
        if exc.code == "NOT_FOUND"
        else ErrorCode.DEPLOY_UNAVAILABLE
        if exc.code in ["EXECUTOR_UNAVAILABLE", "IDENTITY_UNAVAILABLE"]
        else ErrorCode.DEPLOY_CONFLICT
        if "CONFLICT" in exc.code or exc.code == "RECOVERY_REQUIRED"
        else ErrorCode.DEPLOY_INVALID
    )
    response = error_response(
        request, code, [ErrorDetail(field=exc.path or "deployment", reason=exc.code)]
    )
    # Engine errors are controlled messages, never raw subprocess output.
    body = json.loads(response.body)
    body["message"] = exc.message
    return JSONResponse(
        body,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k != "content-length"},
    )


@router.get("/options", response_model=Envelope[DeploymentOptions])
async def options(request: Request, user: CurrentUser):
    s = service(request)
    ready = await asyncio.to_thread(s.executor_ready)
    return success(
        request,
        {
            "executionEnabled": ready,
            "baseDomain": s.engine.base_domain,
            "gitHosts": s.engine.git_hosts,
            "maxAppMemoryMiB": s.engine.max_app_memory_mib,
            "message": "" if ready else "部署执行环境待配置，暂未开放提交。",
        },
    )


@router.get("/name-availability")
async def name_availability(
    request: Request,
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=7),
    app_id: str | None = None,
):
    available = await asyncio.to_thread(
        service(request).name_available, user, name, app_id
    )
    return success(request, {"available": available})


@router.post("/jobs", status_code=202, response_model=Envelope[DeploymentJob])
async def submit(
    body: Submit,
    request: Request,
    user: CurrentUser,
    idempotency_key: str = Header(default=""),
):
    value = await asyncio.to_thread(
        service(request).submit,
        user,
        body.repository,
        body.name,
        idempotency_key,
        body.app_id,
    )
    return success(request, value)


@router.get("/jobs", response_model=Envelope[list[DeploymentJob]])
async def jobs(request: Request, user: CurrentUser):
    return success(request, await asyncio.to_thread(service(request).list_jobs, user))


@router.get("/jobs/{job_id}", response_model=Envelope[DeploymentJob])
async def job(job_id: str, request: Request, user: CurrentUser):
    s = service(request)
    cache = request.app.state.store
    key = "deploy:job:" + job_id
    try:
        saved = await cache.get(key)
    except (RedisError, TimeoutError, OSError):
        saved = None
    # A cache hit still requires current SQLite ownership and revision checks.
    actor = await asyncio.to_thread(s.actor, user)
    raw = await asyncio.to_thread(s.store.get, job_id)
    await asyncio.to_thread(s.access.require_job, actor, raw)
    if saved:
        try:
            data = json.loads(saved)
            if data["updatedAt"] == raw["updated_at"]:
                return success(request, data)
        except (ValueError, KeyError, TypeError):
            pass
    data = await asyncio.to_thread(s.project_job, raw)
    try:
        await cache.set(key, json.dumps(data), ttl=5)
    except (RedisError, TimeoutError, OSError):
        logging.getLogger("ows_community").warning("deployment_cache_write_failed")
    return success(request, data)
