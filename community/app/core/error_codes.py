"""Single source of truth for public error codes; values are API contracts."""
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType


@dataclass(frozen=True)
class ErrorDefinition:
    status: int
    message: str
    headers: tuple[tuple[str, str], ...] = ()


@unique
class ErrorCode(str, Enum):
    def __new__(cls, code: str, status: int, message: str,
                headers: tuple[tuple[str, str], ...] = ()):
        obj = str.__new__(cls, code)
        obj._value_ = code
        obj.definition = ErrorDefinition(status, message, headers)
        return obj

    BAD_REQUEST = ("BAD_REQUEST", 400, "请求无效")
    INVALID_HOST = ("INVALID_HOST", 400, "请求域名不被允许")
    INVALID_CONTENT_LENGTH = ("INVALID_CONTENT_LENGTH", 400, "请求长度无效")
    UNAUTHORIZED = ("UNAUTHORIZED", 401, "登录已失效或尚未登录", (("WWW-Authenticate", "Bearer"),))
    OAUTH_STATE_INVALID = ("OAUTH_STATE_INVALID", 400, "登录请求已过期或校验失败，请重新登录")
    FEISHU_NOT_CONFIGURED = ("FEISHU_NOT_CONFIGURED", 503, "飞书登录尚未配置")
    FEISHU_AUTH_FAILED = ("FEISHU_AUTH_FAILED", 401, "飞书授权已失效，请重新登录")
    CSRF_FAILED = ("CSRF_FAILED", 403, "请求来源或校验信息无效")
    DEV_LOGIN_DENIED = ("DEV_LOGIN_DENIED", 401, "开发登录凭据无效")
    FORBIDDEN = ("FORBIDDEN", 403, "没有执行此操作的权限")
    NOT_FOUND = ("NOT_FOUND", 404, "资源或接口不存在")
    METHOD_NOT_ALLOWED = ("METHOD_NOT_ALLOWED", 405, "请求方法不支持")
    REQUEST_TIMEOUT = ("REQUEST_TIMEOUT", 408, "请求超时")
    CONFLICT = ("CONFLICT", 409, "资源状态冲突，请刷新后重试")
    GONE = ("GONE", 410, "资源已移除")
    PAYLOAD_TOO_LARGE = ("PAYLOAD_TOO_LARGE", 413, "请求体超过大小限制")
    UNSUPPORTED_MEDIA_TYPE = ("UNSUPPORTED_MEDIA_TYPE", 415, "请求内容类型不支持")
    VALIDATION_ERROR = ("VALIDATION_ERROR", 422, "请求参数不符合要求")
    RATE_LIMITED = ("RATE_LIMITED", 429, "请求过于频繁，请稍后重试")
    DEPLOY_INVALID = ("DEPLOY_INVALID", 400, "部署请求不符合要求")
    DEPLOY_CONFLICT = ("DEPLOY_CONFLICT", 409, "部署状态或资源存在冲突")
    DEPLOY_UNAVAILABLE = ("DEPLOY_UNAVAILABLE", 503, "部署执行环境暂不可用")
    INTERNAL_ERROR = ("INTERNAL_ERROR", 500, "服务暂时异常，请稍后重试")
    NOT_IMPLEMENTED = ("NOT_IMPLEMENTED", 501, "功能尚未接入")
    FEISHU_NOT_IMPLEMENTED = ("FEISHU_NOT_IMPLEMENTED", 501, "飞书身份认证尚未接入")
    UPSTREAM_ERROR = ("UPSTREAM_ERROR", 502, "上游服务请求失败")
    SERVICE_UNAVAILABLE = ("SERVICE_UNAVAILABLE", 503, "服务暂不可用")
    CACHE_UNAVAILABLE = ("CACHE_UNAVAILABLE", 503, "缓存服务暂不可用", (("Retry-After", "5"),))
    SNAPSHOT_NOT_READY = ("SNAPSHOT_NOT_READY", 503, "内容尚未完成同步", (("Retry-After", "10"),))
    UPSTREAM_TIMEOUT = ("UPSTREAM_TIMEOUT", 504, "上游服务响应超时")


# Generic HTTP exceptions have one canonical code per status. Business-specific
# codes sharing that status are selected explicitly by the service layer.
HTTP_ERROR_CODES = MappingProxyType({code.definition.status: code for code in (
    ErrorCode.BAD_REQUEST, ErrorCode.UNAUTHORIZED, ErrorCode.FORBIDDEN,
    ErrorCode.NOT_FOUND, ErrorCode.METHOD_NOT_ALLOWED, ErrorCode.REQUEST_TIMEOUT,
    ErrorCode.CONFLICT, ErrorCode.GONE, ErrorCode.PAYLOAD_TOO_LARGE,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE, ErrorCode.VALIDATION_ERROR, ErrorCode.RATE_LIMITED,
    ErrorCode.INTERNAL_ERROR, ErrorCode.NOT_IMPLEMENTED, ErrorCode.UPSTREAM_ERROR,
    ErrorCode.SERVICE_UNAVAILABLE, ErrorCode.UPSTREAM_TIMEOUT,
)})


def from_http_status(status: int) -> ErrorCode:
    """Unknown 4xx normalize to 400; other unknown statuses normalize to 500."""
    return HTTP_ERROR_CODES.get(status, ErrorCode.BAD_REQUEST if 400 <= status < 500
                                else ErrorCode.INTERNAL_ERROR)
