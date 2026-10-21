# 飞书登录配置与验收

本指南适用于 `auth_mode: "feishu"`。平台网页通过飞书授权码流程识别员工，平台自己保存可撤销的登录会话和应用权限。`lark-cli` 用于应用配置和运维核验；服务不会复用某台电脑的 CLI 登录态，也不要求员工提供访问凭证。

这一阶段包含平台登录及应用访问控制。审批、业务应用自己的飞书单点登录、应用秘密的静态加密、Skill 配对登录仍是后续工作。自动化测试使用模拟飞书响应，不能代替真实租户、授权回调和撤权验收。

## 1. 准备平台专用飞书应用

在组织的飞书开发者后台使用一个受管理员管理的企业自建应用，并完成发布、启用及应用可用范围配置。记录它的 App ID 和预期企业 `tenant_key`，不要按员工邮箱或姓名推断企业身份。

为应用身份开通以下最小权限：

| 权限 | 用途 |
| --- | --- |
| `contact:contact.base:readonly` | 按 Open ID 查询成员 |
| `contact:user.employee:readonly` | 获取激活、冻结、离职、退出、未加入状态 |
| `application:application:self_manage` | 查询本应用的可用、禁用名单 |

通讯录数据范围应覆盖允许登录的员工。应用可用范围与通讯录数据范围是不同设置；平台分别检查两者，不能用“查得到员工”替代“员工可以使用应用”。状态缺失、权限不足和上游失败不会被视作允许登录。只做登录不需要申请 `offline_access`、邮箱或手机号权限。

在应用安全设置中登记完整回调地址：

```text
https://awesome.guanghexinzhi.cn/auth/feishu/callback
```

它必须与 `public_url` 加 `/auth/feishu/callback` 的结果完全一致，包括协议、主机和端口。配置固定 HTTPS 管理入口，不使用应用子域名作为管理登录入口。已有证书、ACME 路由和续期配置按 [运维指南](README.md) 保留。

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

将以下字段合并到既有 `/etc/awesome-deploy/config.json`，保留其余部署配置。示例中的 App ID 和企业标识必须替换为实际值：

```json
{
  "auth_mode": "feishu",
  "public_url": "https://awesome.guanghexinzhi.cn",
  "feishu_app_id": "cli_REPLACE",
  "feishu_tenant_key": "REPLACE_TENANT_KEY",
  "feishu_app_secret_file": "/var/lib/awesome-deploy/feishu-app-secret"
}
```

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

登录 Cookie 使用 `__Host-` 前缀、`Secure`、`HttpOnly`、`SameSite=Lax` 和 `Path=/`，不设置 `Domain`。不要在代理层改写为 `Domain=.awesome.guanghexinzhi.cn`，否则各业务子域会与管理登录共享 Cookie 边界。修改接口还会检查来源与 CSRF；不要删除 `Origin` 等请求头。

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

用户权限变更会撤销其现有平台会话。飞书成员状态及应用可用范围也会复核；平台本地停用可用于立即撤权。撤权不等于停止已经上线的业务应用，业务停服需要单独执行受控操作。

浏览器的飞书资格缓存最长 5 分钟，过期核验失败会拒绝请求；worker 在停止旧版、启动和发布等关键阶段强制重新核验。应用成员变更采用保守的整应用授权版本：包括添加成员在内的变更都会使该应用尚未提交成功的旧部署授权失效，需要重新提交。已经写入下线破坏意图的操作仍由系统按原固定范围完成安全清理。

普通成员新建应用只尝试公开仓库读取。私有仓库先由运维配置 `git_askpass`，再由管理员在该应用的成员管理页明确批准其固定仓库使用平台读取凭证；成员看不到凭证值。变更仓库绑定同样使旧排队授权失效。没有历史任务的预置秘密空间也必须由管理员明确绑定，普通成员不能先占用相同仓库和域名前缀来取得它。

数据库恢复后、发生平台会话泄露或需要全量重新登录时，在恢复正常流量前执行：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json auth-revoke-all
```

此操作使现有会话及既有登录尝试失效，不撤销员工在飞书侧的授权，也不替代用户停用或应用下线。

切换到飞书模式前停止接单并处理完旧排队任务，核对尚未结束的下线/恢复记录；没有明确用户授权的旧队列不能自动成为员工任务。数据库从备份恢复后，先核对当前用户禁用、成员角色及仓库授权，确认没有恢复已经撤销的权限，再重新启动 worker。登录成功与权限变更保留审计事件；有效授权尝试的失败按 5 分钟桶聚合保存最近 24 小时，无效的匿名回调不逐条写入业务数据库。

## 5. 用 lark-cli 核验平台应用

这部分是可选运维工具，不是 API/worker 的运行依赖。使用能安全读取 App Secret 的受控运维账号和独立 profile，保留现有默认 profile：

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
- 冻结、离职、移出应用可用范围和平台本地停用都能导致访问被拒；飞书接口不可用时不绕过核验。
- 重复回调、过期 state、其他浏览器的回调及伪造跨站修改请求被拒绝；退出后原会话失效，全量注销使已有会话和登录尝试失效。
- 实际检查会话 Cookie 不含 `Domain`，授权回调及故障重试没有把 code、Cookie 或 App Secret 留在各层日志。
- 分别验证新应用归属、旧应用显式分配 owner、成员权限撤销，以及有排队部署时的权限变更处理。

当前实现选用 OAuth v3 的 `code_verifier` 与 S256。官方授权页文档仍有“PKCE 暂搭配 v2”的旧说明，而 v3 token 文档已明确列出 PKCE 参数及错误码；必须完成真实回调验收，不应为绕过失败而移除 PKCE 或降级校验。

接口依据：[授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)、[OAuth v3](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3)、[应用 token](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)、[员工信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get)、[应用可用范围](https://open.feishu.cn/document/server-docs/application-v6/admin/check_white_black_list)。
