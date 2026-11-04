import logging
import re
import time
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import error_response
from app.core.error_codes import ErrorCode

logger = logging.getLogger("ows_community")
REQUEST_ID = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")


class RequestMiddleware:
    """Pure ASGI middleware: bounded JSON bodies, tracing and safe error boundary."""

    def __init__(self, app: ASGIApp, max_body_bytes: int, allowed_hosts: list[str]):
        self.app, self.max_body_bytes, self.allowed_hosts = app, max_body_bytes, allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        headers = Headers(scope=scope)
        incoming_id = headers.get("x-request-id", "")
        request_id = incoming_id if REQUEST_ID.fullmatch(incoming_id) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        request = Request(scope)
        status, response_started = 500, False

        async def traced_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Server-Timing"] = f"app;dur={(time.perf_counter()-started)*1000:.2f}"
                response_headers["Cache-Control"] = "no-store"
            await send(message)

        async def reject(code: ErrorCode):
            await error_response(request, code)(scope, receive, traced_send)

        try:
            host = request.url.hostname
            if host not in self.allowed_hosts:
                await reject(ErrorCode.INVALID_HOST)
                return
            length = headers.get("content-length")
            if length is not None:
                try:
                    declared = int(length)
                    if declared < 0:
                        raise ValueError
                except ValueError:
                    await reject(ErrorCode.INVALID_CONTENT_LENGTH)
                    return
                if declared > self.max_body_bytes:
                    await reject(ErrorCode.PAYLOAD_TOO_LARGE)
                    return
            # Enforce the limit for chunked bodies too; no dependency on Content-Length.
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    status = 499
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body_bytes:
                    await reject(ErrorCode.PAYLOAD_TOO_LARGE)
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body, consumed = b"".join(chunks), False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, traced_send)
        except Exception as exc:
            # Do not log exception strings, request bodies, tokens or query strings.
            logger.error("unhandled_exception", extra={"request_id": request_id,
                         "exception_type": type(exc).__name__})
            if response_started:
                raise  # A started stream cannot be replaced with a JSON response.
            await reject(ErrorCode.INTERNAL_ERROR)
        finally:
            route = scope.get("route")
            logger.info("request_completed", extra={"request_id": request_id,
                        "method": scope["method"], "route": getattr(route, "path", "unmatched"),
                        "status": status, "duration_ms": round((time.perf_counter()-started)*1000, 2)})
