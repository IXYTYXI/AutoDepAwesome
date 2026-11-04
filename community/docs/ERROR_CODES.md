# ows-community 错误码目录

由 `app/core/error_codes.py` 自动生成。修改源码后运行 `uv run python -m scripts.export_error_codes`；不要手动修改本表。

## 规则

- 错误码使用大写英文和下划线，是稳定的接口契约；前端按 `code` 判断，不匹配 `message`。
- 每个错误码固定绑定一个 HTTP 状态、默认中文提示和可选默认响应头。
- 成功码 `OK` 不属于错误枚举；成功使用原有 Envelope，失败使用 ErrorEnvelope。
- 业务侧只传 `ErrorCode`，不得传裸字符串或覆盖 HTTP 状态、默认提示。
- 新的业务错误先加入枚举，再使用；不要复用旧码表达不同含义。
- 同一个 HTTP 状态可以有多个具体业务码，例如缓存故障与快照未就绪都返回 503。
- 业务错误默认使用目录中的响应头，可以显式补充或覆盖头部，例如动态 Retry-After。
- 声明错误码不代表对应业务功能已实现；例如 RATE_LIMITED 为通用 HTTP 429 映射，当前没有应用层限流器。

## 目录

| 错误码 | HTTP | 默认提示 | 默认响应头 |
| --- | --- | --- | --- |
| `BAD_REQUEST` | 400 | 请求无效 | — |
| `INVALID_HOST` | 400 | 请求域名不被允许 | — |
| `INVALID_CONTENT_LENGTH` | 400 | 请求长度无效 | — |
| `UNAUTHORIZED` | 401 | 登录已失效或尚未登录 | WWW-Authenticate: Bearer |
| `OAUTH_STATE_INVALID` | 400 | 登录请求已过期或校验失败，请重新登录 | — |
| `FEISHU_NOT_CONFIGURED` | 503 | 飞书登录尚未配置 | — |
| `FEISHU_AUTH_FAILED` | 401 | 飞书授权已失效，请重新登录 | — |
| `CSRF_FAILED` | 403 | 请求来源或校验信息无效 | — |
| `DEV_LOGIN_DENIED` | 401 | 开发登录凭据无效 | — |
| `FORBIDDEN` | 403 | 没有执行此操作的权限 | — |
| `NOT_FOUND` | 404 | 资源或接口不存在 | — |
| `METHOD_NOT_ALLOWED` | 405 | 请求方法不支持 | — |
| `REQUEST_TIMEOUT` | 408 | 请求超时 | — |
| `CONFLICT` | 409 | 资源状态冲突，请刷新后重试 | — |
| `GONE` | 410 | 资源已移除 | — |
| `PAYLOAD_TOO_LARGE` | 413 | 请求体超过大小限制 | — |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | 请求内容类型不支持 | — |
| `VALIDATION_ERROR` | 422 | 请求参数不符合要求 | — |
| `RATE_LIMITED` | 429 | 请求过于频繁，请稍后重试 | — |
| `DEPLOY_INVALID` | 400 | 部署请求不符合要求 | — |
| `DEPLOY_CONFLICT` | 409 | 部署状态或资源存在冲突 | — |
| `DEPLOY_UNAVAILABLE` | 503 | 部署执行环境暂不可用 | — |
| `INTERNAL_ERROR` | 500 | 服务暂时异常，请稍后重试 | — |
| `NOT_IMPLEMENTED` | 501 | 功能尚未接入 | — |
| `FEISHU_NOT_IMPLEMENTED` | 501 | 飞书身份认证尚未接入 | — |
| `UPSTREAM_ERROR` | 502 | 上游服务请求失败 | — |
| `SERVICE_UNAVAILABLE` | 503 | 服务暂不可用 | — |
| `CACHE_UNAVAILABLE` | 503 | 缓存服务暂不可用 | Retry-After: 5 |
| `SNAPSHOT_NOT_READY` | 503 | 内容尚未完成同步 | Retry-After: 10 |
| `UPSTREAM_TIMEOUT` | 504 | 上游服务响应超时 | — |

## 使用方式

```python
from app.core.error_codes import ErrorCode
from app.core.errors import AppError

raise AppError(ErrorCode.SNAPSHOT_NOT_READY)
# 有动态等待时间时，由调用方显式提供：
raise AppError(ErrorCode.RATE_LIMITED, headers={"Retry-After": "30"})
```

错误响应示例（HTTP 503）：

```json
{
  "code": "SNAPSHOT_NOT_READY",
  "message": "内容尚未完成同步",
  "data": null,
  "error": {"details": []},
  "request_id": "example-request-id",
  "timestamp": "2026-10-21T00:00:00Z"
}
```

## 框架异常与兼容变更

- 404（含关闭的开发登录）统一为 NOT_FOUND，405 统一为 METHOD_NOT_ALLOWED。
- HTTPException(401/403) 与业务身份/权限错误统一为 UNAUTHORIZED / FORBIDDEN。
- 其他已登记 HTTP 状态采用 HTTP_ERROR_CODES 中的通用映射，不生成 HTTP_数字 这样的动态业务码。
- 未登记的 4xx HTTPException 归一为 BAD_REQUEST / 400；其余未登记状态归一为 INTERNAL_ERROR / 500，不回传异常 detail。
- 参数错误保留字段位置和错误类型，不回显输入值；未知程序异常统一为 INTERNAL_ERROR。
- 原 HTTP_404 / HTTP_405 / HTTP_401 / HTTP_403 已删除，客户端应切换到相应语义码。
- AppError 构造函数不再接受 code 字符串、message、status，项目内调用已迁移。

## 文档与范围

Swagger 错误模型的 code 字段使用 ErrorCode 枚举，响应说明及示例从同一目录生成。
路由当前共用错误响应集合，用于展示全局可用码，不意味着每条接口都会产生全部错误。

本约定覆盖应用内可返回 JSON 的 HTTP 错误。CORS 预检、Nginx/服务器在应用前拒绝的请求、连接中断及已开始发送的流式响应不保证使用本格式。

检查生成文件是否过期：`uv run python -m scripts.export_error_codes --check`。
