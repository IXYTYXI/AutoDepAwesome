import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.error_codes import ErrorCode, from_http_status
from app.schemas.common import ErrorEnvelope, ErrorDetail, ErrorInfo

logger = logging.getLogger("ows_community")


class AppError(Exception):
    def __init__(self, code: ErrorCode, *, headers: dict[str, str] | None = None):
        if not isinstance(code, ErrorCode):
            raise TypeError("AppError requires a registered ErrorCode")
        super().__init__(code.definition.message)
        self.code = code
        self.headers = {**dict(code.definition.headers), **(headers or {})}


def error_response(request: Request, code: ErrorCode,
                   details: list[ErrorDetail] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    if not isinstance(code, ErrorCode):
        raise TypeError("error_response requires a registered ErrorCode")
    definition = code.definition
    body = ErrorEnvelope(code=code, message=definition.message, data=None,
                         error=ErrorInfo(details=details or []),
                         request_id=getattr(request.state, "request_id", "unavailable"))
    return JSONResponse(body.model_dump(mode="json"), status_code=definition.status,
                        headers={**dict(definition.headers), **(headers or {})})


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def business_error(request: Request, exc: AppError):
        return error_response(request, exc.code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo submitted values, credential fields, or validator context.
        details = [ErrorDetail(field=".".join(map(str, e["loc"])), reason=e["type"])
                   for e in exc.errors()]
        return error_response(request, ErrorCode.VALIDATION_ERROR, details)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, from_http_status(exc.status_code), headers=exc.headers)

    @app.exception_handler(RedisError)
    async def redis_error(request: Request, exc: RedisError):
        logger.warning("cache_unavailable", extra={"request_id": request.state.request_id})
        return error_response(request, ErrorCode.CACHE_UNAVAILABLE)

    @app.exception_handler(httpx.TimeoutException)
    async def upstream_timeout(request: Request, exc: httpx.TimeoutException):
        return error_response(request, ErrorCode.UPSTREAM_TIMEOUT)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request: Request, exc: httpx.HTTPError):
        return error_response(request, ErrorCode.UPSTREAM_ERROR)

    # Unhandled exceptions are caught by the outer request middleware so CORS,
    # request-id and the same error envelope also apply to HTTP 500 responses.
