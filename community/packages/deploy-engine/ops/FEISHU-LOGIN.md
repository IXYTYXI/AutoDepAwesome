# 飞书登录配置与验收

本指南适用于 `auth_mode: "feishu"`。平台网页通过飞书授权码流程识别员工，平台自己保存可撤销的登录会话和应用权限。`lark-cli` 用于应用配置和运维核验；服务不会复用某台电脑的 CLI 登录态，也不要求员工提供访问凭证。

本指南覆盖平台登录及应用访问控制。B 阶段的应用秘密静态加密与旧数据迁移另见 [密钥库运维指南](../docs/vault-operations.md)；审批、业务应用自己的飞书单点登录及 Skill 配对登录仍是后续工作。自动化测试使用模拟飞书响应，不能代替真实租户、授权回调和撤权验收。

`feishu_eligibility_mode` 明确选择身份核验范围，只接受以下两个值；省略时仍为 `continuous`，未知值拒绝启动，权限不足或接口故障不会自动切换模式。

| 模式 | 登录与后续核验 |
| --- | --- |
| `login` | 基础飞书登录。通过 OAuth 换码及 `user_info` 核验租户、Open ID；保留 state、浏览器绑定、S256 PKCE、平台会话、CSRF 和本地成员权限。不申请下表三项应用身份权限，不查询员工状态或应用可用名单；登录后暂不主动跟踪飞书账号停用及可用范围变化。 |
| `continuous` | 既有持续核验模式。登录时查询员工状态与应用可用范围，浏览器资格缓存最多 5 分钟，worker 关键阶段强制远端复核；需要下表对应应用身份权限。 |

两种模式均不保存飞书用户 access/refresh token。`login` 模式下，API 与 worker 仍检查本地账号 enabled/eligible、授权版本、部署能力和应用成员权限；平台本地停用及成员撤权继续生效。平台会话仍受 30 分钟空闲和 8 小时绝对期限约束；只完成一次基础登录不代表持续确认飞书账号状态。

**当前版本需要 OAuth 登录重定向，不需要事件订阅或卡片回调。** 下文的 `/auth/feishu/callback` 是浏览器登录返回的 GET 接口，不是飞书服务器推送事件的请求地址。不要将它填入后台“事件与回调”的事件配置或回调配置。

## 1. 准备平台专用飞书应用

在组织的飞书开发者后台使用一个受管理员管理的企业自建应用，并完成发布、启用及应用可用范围配置。记录它的 App ID 和预期企业 `tenant_key`，不要按员工邮箱或姓名推断企业身份。

仅 `continuous` 模式为应用身份开通以下权限；`login` 模式无需为基础登录申请这些权限：

| 权限 | 用途 |
| --- | --- |
| `contact:contact.base:readonly` | 按 Open ID 查询成员 |
| `contact:user.employee:readonly` | 获取激活、冻结、离职、退出、未加入状态 |
| `application:application:self_manage` | 查询本应用的可用、禁用名单 |

`continuous` 模式的通讯录数据范围应覆盖允许登录的员工。应用可用范围与通讯录数据范围是不同设置；持续模式分别检查两者，不能用“查得到员工”替代“员工可以使用应用”。状态缺失、权限不足和上游失败不会被视作允许登录。`login` 模式只使用授权码和 `user_info` 返回的基础身份，接口失败、错误租户或无效 Open ID 仍拒绝登录。两种模式均不需要申请 `offline_access`、邮箱或手机号权限。

用户授权码固定通过 `https://open.feishu.cn/open-apis/authen/v2/oauth/token` 换码，以 JSON 发送客户端凭证、原回调地址及 `code_verifier`，请求类型为 `application/json; charset=utf-8`。仅 `continuous` 模式获取应用 token 时继续使用 OAuth v3 和表单请求；该应用客户端认证必须允许 `client_secret_post`，不支持时飞书会返回 `20140`。代码保留 S256 PKCE，不会在端点之间自动回退。

在应用安全设置中登记完整回调地址。下文的 `deploy.example.com` 是示例域名，必须替换为自身部署的 `public_url` 主机，示例配置不可直接用于实际接入：

```text
https://deploy.example.com/auth/feishu/callback
```

它必须与 `public_url` 加 `/auth/feishu/callback` 的结果完全一致，包括协议、主机和端口。配置固定 HTTPS 管理入口，不使用应用子域名作为管理登录入口。已有证书、ACME 路由和续期配置按 [运维指南](README.md) 保留。

### 后台配置清单

| 后台项目 | 本阶段设置 |
| --- | --- |
| 应用类型、版本和状态 | 使用平台专用企业自建应用，确认所属企业、已发布版本和启用状态。 |
| 凭证与基础信息 | 确认 App ID；App Secret 通过下节安全方式安装到服务器。预期 `tenant_key` 由受信任的企业管理员或已核验的企业安装信息确认。 |
| 权限管理 | `login` 无需上表三项权限；`continuous` 开通对应**应用身份**权限并完成必要的审批和发布。不额外申请邮箱、手机号或 `offline_access`。 |
| 通讯录数据范围 | 仅 `continuous` 查询员工时需要覆盖允许登录的管理员和员工。 |
| 应用可用范围 | 包含允许使用平台的人，首次验收优先限定为指定管理员和专用测试成员；与通讯录数据范围分别核对。 |
| 开发配置 → 安全设置 → 重定向 URL | 登记完整的 `https://deploy.example.com/auth/feishu/callback`。这是 OAuth 配置位置。 |
| OAuth 客户端认证方式 | 确认支持代码使用的 `client_secret_post`，按后台实际呈现的认证设置核对；不要只因填写了 App Secret 就假定该方式已允许。 |
| 开发配置 → 事件与回调 | 本阶段无需选择订阅方式、添加事件或卡片回调，也无需提供事件请求地址、Verification Token 或 Encrypt Key。已有应用承载的其他事件配置不要为本平台随意删除。 |

若希望从飞书工作台点击进入，可另外开启网页应用能力并将主页指向平台固定 HTTPS 入口；这是可选入口展示配置。当前浏览器 OAuth 流程未使用 H5 JSAPI、`jsapi_ticket` 或飞书客户端免登 SDK，不应把这些配置当作当前登录链路的必填项。相关范围见官方[浏览器应用接入指南](https://open.feishu.cn/document/common-capabilities/sso/web-application-end-user-consent/guide)与[网页应用概述](https://open.feishu.cn/document/client-docs/h5/introduction)。

### OAuth、事件订阅与卡片回调的区别

| 类型 | 数据如何到达平台 | 当前实现 |
| --- | --- | --- |
| OAuth 登录重定向 | 用户的浏览器向已登记的重定向 URL 发出 GET，携带一次性 `code` / `state` 或拒绝授权的 `error`。 | 已有 `GET /auth/feishu/callback`；校验浏览器绑定、state、PKCE 和企业身份后创建平台会话。 |
| 事件订阅 | 飞书服务器经 Webhook POST 或 SDK 长连接推送异步事件。Webhook 地址需完成 challenge 验证并校验请求来源。 | 未实现事件接收路由、challenge 应答、事件签名/解密与去重处理，也未运行长连接或 CLI 事件消费者。 |
| 卡片等交互回调 | 用户操作卡片后，飞书经回调订阅通道把交互交给服务端并等待同步响应。 | 未实现卡片操作、审批交互或链接预览回调。没有可供后台填写的卡片回调接口。 |

`continuous` 模式通过服务端主动查询员工状态和应用可用范围来核验资格，浏览器资格缓存最长 5 分钟，worker 关键阶段强制复核。`login` 模式的后续检查仅核验本地权限，不进行远端轮询。两种模式均无员工变更事件驱动的即时失效、用户撤回 OAuth 同意的事件处理、审批卡片或消息事件集成。新增这些能力时，须先实现对应接收与安全处理，再配置订阅；不要提前编造 `/events`、`/webhook` 等地址。平台已有的部署记录 `events` API 只读取内部部署事件，也不是飞书接收端。

上述区分依据官方[获取授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)、[事件发送至开发者服务器](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server)、[长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)与[回调概述](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/callback-overview)。事件 Encrypt Key 与 B 阶段密钥库的独立 KEK 用途不同；不接收飞书事件不代表可以省略密钥库的 KEK。

### 可先完成、不依赖真实 App 的准备

- 准备固定 HTTPS 入口、有效证书、精确回调反向代理和各层日志脱敏，使用不含真实授权码的合成标记检查日志。
- 以服务运行用户确认 `accounts.feishu.cn` 与 `open.feishu.cn` 的 DNS/TLS 可达；无凭证请求只能证明网络连通，不能作为 OAuth 或 API 鉴权成功证据。
- 在独立安装与独立状态副本上验证候选版本、配置校验、会话和权限逻辑；准备平台状态及业务数据的分离备份、恢复步骤和旧应用归属清单。
- 准备带占位符的配置片段、服务用户私有目录和安全输入步骤。App 未配置完成前不把生产 `auth_mode` 切为 `feishu`，不创建假身份、假会话或空秘密文件来代替真实接入。

后台设置就绪后，服务端只需补齐 App ID、预期 `tenant_key` 和安全交付的 App Secret；不需要额外的事件凭证。指定管理员与专用测试成员仍须配合真实登录、拒绝授权及撤权验收。前三项资料到位并不等于这些验收已完成。

## 2. 保存 App Secret 并配置服务

App Secret 属于平台服务端。通过服务器终端的不回显输入或受控密钥分发保存，不放入 Git、聊天、命令参数、网页配置或普通日志。示例文件为 `/var/lib/awesome-deploy/feishu-app-secret`，归属 `awesome:awesome`，权限 `0600`；不得使用符号链接、硬链接或多人可读文件。

已有 `/var/lib/awesome-deploy` 私有目录后，可在服务器终端执行以下初始化示例。它不覆盖已有文件；轮换时先按受控运维流程准备新文件。

```sh
sudo -u awesome /opt/awesome-deploy/.venv/bin/python - <<'PY'
import getpass
import os

secret = getpass.getpass("Feishu App Secret: ")
if not secret or len(secret) > 8191 or not all(33 <= ord(c) <= 126 for c in secret):
    raise SystemExit("App Secret 必须是非空、无空白的单行 ASCII 内容")
fd = os.open("/var/lib/awesome-deploy/feishu-app-secret",
             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="ascii") as target:
    target.write(secret + "\n")
PY
```

将以下字段合并到既有 `/etc/awesome-deploy/config.json`，保留其余部署配置。示例中的 `public_url`、App ID 和企业标识必须替换为自身部署的实际值：

```json
{
  "auth_mode": "feishu",
  "feishu_eligibility_mode": "login",
  "public_url": "https://deploy.example.com",
  "feishu_app_id": "cli_REPLACE",
  "feishu_tenant_key": "REPLACE_TENANT_KEY",
  "feishu_app_secret_file": "/var/lib/awesome-deploy/feishu-app-secret"
}
```

上面显式选择基础登录模式；需要持续员工及应用可用范围核验时，先完成对应权限与验收，再显式设置为 `continuous`。`ops/config.example.json` 保留 `continuous` 默认值，不能仅因省略此字段而认为已选择基础模式。

API 和 worker 使用同一份配置及状态数据库。变更生效需要按既有流程重启两个平台服务。管理 API 继续仅监听 loopback，由 HTTPS 入口反向代理。飞书模式不接受 HTTP Basic 作为备用网页入口；应急管理员操作走受控服务器 CLI。不要为了处理飞书暂时故障将公网入口切回共用 Basic 密码。

## 3. 回调日志及 Cookie

OAuth 回调查询参数包含一次性授权码，不得进入访问日志、请求追踪或错误采集。平台 `serve` 关闭 Uvicorn access log；如果另行启动 ASGI 服务，同样需要关闭或脱敏请求 URL。检查 Nginx、上游负载均衡/WAF 和外部采集器，不能只检查 Python 日志。

在管理域名已有 HTTPS `server` 内增加精确匹配的回调 location，保留其他路由：

```nginx
location = /auth/feishu/callback {
    access_log off;
    # Upstream errors can otherwise include the complete callback query string.
    error_log /dev/null crit;
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

如必须保留路径级访问统计，在 `http` 上下文定义只记录 `$uri` 的专用格式，再给管理入口使用。不要记录 `$request`、`$request_uri`、`$args`、Referer、Cookie、Authorization 或请求正文。回调 location 单独禁记错误日志，避免上游失败时完整请求进入日志；平台仍记录不含参数的登录成功/失败事件。检查 CDN、WAF 和其他代理层同样不保存回调参数，且不要启用 HTTP 客户端 debug 日志。

登录 Cookie 使用 `__Host-` 前缀、`Secure`、`HttpOnly`、`SameSite=Lax` 和 `Path=/`，不设置 `Domain`。不要在代理层改写为 `Domain=.deploy.example.com`，否则各业务子域会与管理登录共享 Cookie 边界。修改接口还会检查来源与 CSRF；不要删除 `Origin` 等请求头。

## 4. 首位管理员及已有应用归属

首次通过飞书登录的员工默认是普通用户，可为自己提交部署；不会自动成为管理员。上线前先限制管理入口到受控网络，完成首位管理员初始化及已有应用归属处理，再开放给全体目标员工。

让指定管理员先完成一次飞书登录以创建本地用户，然后在服务器以 `awesome` 身份执行：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json user-list

/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json user-set \
  --user-id LOCAL_USER_ID --enabled true --can-deploy true --admin true
```

`LOCAL_USER_ID` 是 `user-list` 返回的平台用户 ID；核对同一平台应用、企业和员工 Open ID，不按姓名选取。授权后重新登录。管理员权限只通过受控 CLI 授予，不在网页上提升用户为管理员。

升级前已有应用默认只有管理员可见、可管理，不按仓库链接、同名域名或“首个访问者”自动认领。管理员在 `/members` 为其明确指定 owner。新部署的应用归提交者所有，其他员工仅能访问自己所属应用；owner 可按页面提供的 owner、maintainer、viewer 角色管理协作成员。

停用用户或撤销部署能力示例：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json user-set \
  --user-id LOCAL_USER_ID --enabled false

/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json user-set \
  --user-id LOCAL_USER_ID --can-deploy false
```

用户权限变更会撤销其现有平台会话。`continuous` 模式还会复核飞书成员状态及应用可用范围；`login` 模式暂不做此项远端复核。两种模式都可用平台本地停用立即撤权。撤权不等于停止已经上线的业务应用，业务停服需要单独执行受控操作。

`continuous` 模式的飞书资格缓存最长 5 分钟，过期核验失败会拒绝请求；worker 在停止旧版、启动和发布等关键阶段强制重新核验。`login` 模式在相同路径保留本地权限检查。两种模式的应用成员变更都采用保守的整应用授权版本：包括添加成员在内的变更都会使该应用尚未提交成功的旧部署授权失效，需要重新提交。已经写入下线破坏意图的操作仍由系统按原固定范围完成安全清理。

员工单独撤回飞书 OAuth 同意，不一定改变员工状态或应用可用范围。当前服务不保存用户 token，也未接入用户撤授权事件，不能承诺该动作会立即撤销平台会话。立即撤权使用平台本地停用；飞书资格变动仅在 `continuous` 模式按上述周期复核。

普通成员新建应用只尝试公开仓库读取。私有仓库先由运维配置 `git_askpass`，再由管理员在该应用的成员管理页明确批准其固定仓库使用平台读取凭证；成员看不到凭证值。变更仓库绑定同样使旧排队授权失效。没有历史任务的预置秘密空间也必须由管理员明确绑定，普通成员不能先占用相同仓库和域名前缀来取得它。

数据库恢复后、发生平台会话泄露或需要全量重新登录时，在恢复正常流量前执行：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json auth-revoke-all
```

此操作使现有会话及既有登录尝试失效，不撤销员工在飞书侧的授权，也不替代用户停用或应用下线。

切换到飞书模式前停止接单并处理完旧排队任务，核对尚未结束的下线/恢复记录；没有明确用户授权的旧队列不能自动成为员工任务。数据库从备份恢复后，先核对当前用户禁用、成员角色及仓库授权，确认没有恢复已经撤销的权限，再重新启动 worker。登录成功与权限变更保留审计事件；有效授权尝试的失败按 5 分钟桶聚合保存最近 24 小时，无效的匿名回调不逐条写入业务数据库。

## 5. 用 lark-cli 核验平台应用

这部分是 `continuous` 模式的可选运维工具，不是 API/worker 的运行依赖；基础 `login` 模式不需要执行员工或名单查询，也不应为此申请权限。使用能安全读取 App Secret 的受控运维账号和独立 profile，保留现有默认 profile：

```sh
lark-cli config init --name awesome-deploy --app-id cli_REPLACE \
  --app-secret-stdin --brand feishu < /var/lib/awesome-deploy/feishu-app-secret

lark-cli --profile awesome-deploy whoami --as bot

lark-cli --profile awesome-deploy contact +get-user \
  --user-id ou_REPLACE --user-id-type open_id --as bot

lark-cli --profile awesome-deploy api POST \
  /open-apis/application/v6/applications/cli_REPLACE/visibility/check_white_black_list \
  --as bot --params '{"user_id_type":"open_id"}' \
  --data '{"user_ids":["ou_REPLACE"]}'
```

最后一个 POST 是可用范围查询。员工 Open ID 必须来自这个平台应用，不能拿其他 CLI 应用的 Open ID 代入。bot 查询无需执行 `auth login`；缺少应用权限时在飞书后台开通并发布，不靠用户登录补权限。CLI 本地 profile 也保存敏感配置，应按私有运维凭证管理。

服务直接使用官方 HTTPS 接口进行 OAuth 和资格核验，不调用 `lark-cli auth login` 代表网页访问者，也不保存飞书用户 refresh token。运行主机需能访问 `accounts.feishu.cn` 和 `open.feishu.cn`；本接口不会继承 shell 的 HTTP 代理环境变量。

## 6. 真实环境验收

启用前在目标主机和真实飞书应用完成并记录以下结果：

- 允许的员工能完成完整 HTTPS 回调；不同企业、无应用可用权限的员工被拒绝。
- 首次登录不是管理员；管理员 CLI 授权后生效；普通员工不能查看或操作其他人的应用、部署记录及秘密配置。
- 两种模式都验证平台本地停用、成员撤权继续拒绝访问；`continuous` 另验证冻结、离职及移出应用可用范围后的持续拒绝访问，远端失败不绕过核验。`login` 验证登录后不产生员工或名单查询，并明确不承诺这类飞书状态变化自动撤销平台会话。
- 重复回调、过期 state、其他浏览器的回调及伪造跨站修改请求被拒绝；退出后原会话失效，全量注销使已有会话和登录尝试失效。
- 单独生成新的授权码，换码时传入错误的 `code_verifier`，确认返回 `20049` 且不创建平台会话；成功换码与该负向验证必须分别使用新的授权码。
- 实际检查会话 Cookie 不含 `Domain`，授权回调及故障重试没有把 code、Cookie 或 App Secret 留在各层日志。
- 分别验证新应用归属、旧应用显式分配 owner、成员权限撤销，以及有排队部署时的权限变更处理。

用户换码使用 v2 是针对实际环境中 v3 返回 `20049` 的兼容处理；真实对照已确认 v2 JSON 请求可以完成换码。官方已将 v2 标记为历史接口，这不代表官方推荐从 v3 回退。官方文档还说明 v2 会容忍“授权阶段未传 challenge、换码时传 verifier”的请求，因此必须确认授权链接含 S256 challenge，并完成上述错误 verifier 的真实负向验收。应用 token 仍使用 v3；任何换码失败都会拒绝登录，不会尝试其他端点或移除 PKCE。

接口依据：[授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)、[用户 OAuth v2](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)、[用户 OAuth v3](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3)、[应用 token](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)、[员工信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get)、[应用可用范围](https://open.feishu.cn/document/server-docs/application-v6/admin/check_white_black_list)。
