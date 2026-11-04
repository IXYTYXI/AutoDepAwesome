# 飞书网页登录

本阶段使用飞书网页 OAuth 授权，普通浏览器中已有飞书登录态时由飞书复用。
是否需要用户确认由飞书决定。客户端内 JSAPI 免登尚未接入。

## 本地运行

`uv run python -m app` 同时启动 8000 后端和 8765 同源网关。
浏览器统一访问 `http://127.0.0.1:8765/`，不要混用 localhost。

`.env` 中配置 `OWS_COMMUNITY_FEISHU_APP_ID`、`OWS_COMMUNITY_FEISHU_APP_SECRET` 和
`OWS_COMMUNITY_SITE_ORIGIN=http://127.0.0.1:8765`。
凭证仅保存在后端，不进入前端 JS、响应或日志。`.env` 不纳入版本控制。

飞书安全设置的重定向 URL 必须为：
`http://127.0.0.1:8765/api/v1/auth/feishu/callback`。
本机地址仅适用于这台电脑上的浏览器测试，不能作为其他同事或手机的部署入口。

## 路由与会话

- `GET /api/v1/auth/feishu/start?next=/cases.html`：创建 5 分钟有效的一次性 state、浏览器绑定 Cookie 和 PKCE S256 校验；跳转飞书授权。
- `GET /api/v1/auth/feishu/callback`：核对 Cookie 与 state、原子消费授权事务、服务端换取用户凭证和基本身份；建立本站会话，重定向回允许的站内页面。
- `GET /api/v1/auth/session`：返回当前用户和用于本站写操作的 CSRF 校验值。不会返回会话令牌或飞书 token。
- `POST /api/v1/auth/logout`：撤销服务端会话并删除 Cookie；不退出用户的飞书账号。
- `POST /api/v1/auth/dev`：仅在启用开发登录时可用，仍需服务端配置的开发密钥，支持 Cookie 和原有 Bearer 联调。
- 旧的 `POST /api/v1/auth/feishu` 不接受无 state 的裸授权码，返回 `OAUTH_STATE_INVALID`。请从 start 接口发起完整流程。

本站 Cookie 为 HttpOnly + SameSite=Lax；生产环境还必须 Secure。
写操作使用 Cookie 时同时校验配置的 Origin 和 `X-CSRF-Token`。
OAuth 事务通过 MemoryStore 锁 / Redis GETDEL 原子消费；Redis 需 6.2 或以上。
本站会话与 OAuth 事务共用现有存储适配器，开发默认内存，进程重启后需要重新登录；生产强制 Redis。

飞书用户以 `feishu:{app_id}:{open_id}` 为本站 ID，角色固定为 member。
仅使用基本身份字段，不申请手机号、邮箱、通讯录读取或离线访问权限。
自建应用用户可用范围由飞书 token 端点校验；可配置 `FEISHU_ALLOWED_TENANTS` 做额外企业隔离。
没有配置该列表时仅适用于已经核对归属和可用范围的企业自建应用。

## 页面保护

业务 HTML、JS、CSS 和图片必须经过 FastAPI 鉴权，不能由 Nginx 公开静态直出。
公开资源仅含登录页面及其必要脚本、样式、Logo、Lucide 图标。
未登录业务页面重定向登录页；未登录业务 API / 资源返回统一 401。
拒绝授权、校验失败和上游故障回登录页供重试，不无限自动跳转。
登录页面自动跳转只发生于访问受保护页面后的 `auto=1` 流程；主动退出后停留在登录页。

前端内容和发布仍使用示例/本机数据适配器；这次仅接入登录身份，不声称已接入多维表格或真实积分。
本机内容存储按用户 ID 分开；旧版匿名示例缓存保留原键，未删除。

## 部署

生产使用同域 HTTPS + Nginx 转发全部页面和 API 至 FastAPI，参考 `deploy/nginx.conf.example`。
配置 site_origin、allowed_hosts、Redis、cookie_secure=true、docs_enabled=false、dev_login_enabled=false。
更新飞书回调和主页为部署域名，验证后移除不再使用的本机回调。
不要在代理访问日志中记录 OAuth code/state，不缓存身份接口、个人数据或 Set-Cookie 响应。

## 官方接口

- [获取授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)
- [获取 user_access_token v2（PKCE 配套端点）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)
- [获取用户信息](https://open.feishu.cn/document/server-docs/authentication-management/login-state-management/get)

授权码文档目前注明 PKCE 暂时搭配 v2 token 端点。实际测试 v3 返回 20049，因此本项目采用文档指定的 v2 JSON 端点，保留成对的 PKCE challenge/verifier；飞书访问 token 仅用于本次获取身份，随后丢弃。

## 本机验收记录（2026-10-21）

- 飞书自建应用：组织效能（cli_aa04dd8797f8dbd1）。
- 版本 1.0.2 已发布；权限无变更，原有所有员工可用范围、默认机器人能力和两个既有回调地址保留。
- 新增回调 `http://127.0.0.1:8765/api/v1/auth/feishu/callback`，桌面网页地址 `http://127.0.0.1:8765/`。
- Chrome 中已有的飞书网页登录态被识别；飞书要求确认此前已授予的用户身份标识权限，无需重新输入密码或扫码。
- 确认授权后返回原目标个人页，真实姓名正确；刷新保持本站会话；退出后停留在登录页。
- v3 端点在真实 PKCE 流程返回 20049；采用授权码官方文档注明的 v2 PKCE 配套端点后验证成功。

## 个人资料和默认鉴权（2026-10-21）

`Principal`、`GET /api/v1/me` 和 `GET /api/v1/auth/session` 新增 `email`、`departments`（部门 ID、名称列表）、`profile_status`。邮箱优先采用飞书企业邮箱，未提供时使用通讯录邮箱；缺少数据返回 null/空列表，前端显示“未提供”。多部门全部展示，不推断主部门。字段仅用于展示，身份仍使用应用 ID + open_id，不能以邮箱作为登录凭据。

登录换码后使用当前用户的 user_access_token 查询自己的通讯录信息及所属部门名称；不提供任意用户查询入口。资料随本站会话存储，不在每次业务请求时查询飞书。权限缺失或资料接口暂时失败不影响已经验证的身份，`profile_status` 标记 partial/unavailable。权限调整或资料变更后，重新登录刷新；旧会话字段兼容为空。飞书 token 仍不存入会话或前端。

需要用户身份权限：
- `contact:contact.base:readonly`（用户/部门查询接口权限）
- `contact:user.email:readonly`（邮箱字段）
- `contact:user.department:readonly`（所属部门字段）
- `contact:department.base:readonly`（部门名称字段）

所有通过 FastAPI 或 include_router 新注册的 HTTP 业务接口都经过 `app/api/access.py` 的全局鉴权依赖，包括不同版本路径。公开入口严格按 HTTP 方法 + 完整路径列出：GET /health/live、GET /health/ready、GET /api/v1/auth/options、GET /api/v1/auth/feishu/start、GET /api/v1/auth/feishu/callback、POST /api/v1/auth/dev（另需开发密钥且生产关闭）、POST /api/v1/auth/feishu（兼容接口，始终拒绝无绑定授权码）。不能通过放进 /auth 或 /health 路径绕过认证。

静态页面由唯一的 frontend handler 自己执行资源白名单、文件安全校验和登录跳转；开发环境文档由框架生成，生产强制关闭。CORS 预检允许通过，不执行业务逻辑。不要用 Starlette Route、mount 子应用或中间件直接新增业务响应绕过 FastAPI 依赖；这些机制若确需引入，应单独接入同样的认证保护。

未登录、伪造、过期、已撤销会话返回 401 UNAUTHORIZED；已登录但角色不足返回 403 FORBIDDEN；Cookie 写请求仍校验 Origin 和 CSRF。服务端从认证结果取 user_id/tenant_id，不能信任客户端传来的身份字段。默认鉴权不代替数据归属和角色授权检查。

权限发布后设置 `OWS_COMMUNITY_FEISHU_PROFILE_ENABLED=true` 并重新登录启用资料读取；默认 false，未开通权限时保持原登录可用。

### 资料接入验收

2026-10-21：组织效能 1.0.3 已发布，四项用户身份只读权限审核通过。版本地址：https://open.feishu.cn/app/cli_aa04dd8797f8dbd1/version/7699027740326267909 。本机 `.env` 已启用资料读取。Chrome 重新授权后，个人空间成功显示当前用户的真实部门名称和工作邮箱；截图 `design/feishu-profile-verified.png`。80 项自动化测试通过，真实 HTTP smoke 验证健康检查、开发登录、个人信息、角色控制、内容状态、退出及会话撤销通过。
