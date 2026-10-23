---
name: awesome-deploy-integration
description: "为可信后端接入 Awesome Deploy 的共享登录态、按用户权限搜索项目、幂等部署申请和进度查询。用于接入开发、联调与排障；不负责自动发布服务或修改业务逻辑。"
---

# Awesome Deploy 接入

帮助其他项目和 AI 在实际登录用户的身份下接入 Awesome Deploy。这里 A 是调用方后端，适配服务保存可靠请求，B 是部署平台。契约核对版本：0.11.3，2026-10-23。

## 先确定边界

- 接入开发、生成代码、离线测试不代表获准真实部署。仅在用户指令覆盖相应外部变更时签发/撤销真实会话或提交部署；已有授权可直接执行，不重复索要确认。不自行修改业务逻辑、启动 Docker、改运维配置或发布接入服务。
- 浏览器只访问 A。A 必须验证自己的 OAuth 回调或可信上游登录会话；不得相信前端传入的 openId、姓名、角色或负责人。`sessionId` 是不透明的共享身份句柄，不是飞书 OAuth access_token / refresh_token，也不授予飞书文件权限。
- 普通用户仅搜索本人和明确授予 maintainer 的项目；viewer 不够。启用且有 can_deploy 权限的平台管理员可搜索全部。内部凭证/source 凭证不提升用户权限，也不能通过请求字段授予管理员。
- 配置 delegation_source 的同组 source 凭证仅用于 integrations 接口，不能调用普通 `/api/v1/automation/deployments` 的提交或查询接口。
- 新建应用归实际操作用户；更新保留原负责人，记录实际提交人。不能以来源凭证负责人替代用户。

## 实施顺序

接入时读取 [API 契约](references/api-contract.md)；写后端适配代码时再读取 [后端实施与验收](references/backend-integration.md)。不需要本仓库、飞书文档或生产密钥即可使用本包的说明和离线测试。

1. 确认适配服务地址、同一飞书 App ID / tenant_key 的约定和凭证已由运维配置。同机助手默认连接 `http://127.0.0.1:8099`，读取 `/etc/awesome-team/intake-token`；支持环境变量覆盖，无需每次手填。HTTP 仅可连接 loopback；其他地址必须有效 HTTPS，不关闭证书验证或跟随重定向。密钥不放在代码、参数、浏览器、Git、聊天、日志或本包中。
2. A 验证实际登录身份后 `POST /sessions`，将 sessionId 关联到 A 的服务端登录会话。TTL 不超过上游会话剩余时间和 8 小时；重新登录先撤销旧会话。
3. 更新应用前 `POST /apps/search`，携带服务端 sessionId。按 nextOffset 分页，将可见结果供用户选择。沿用用户明确指定且核实的 appId；否则取得明确选择，不能自动挑第一条或把空结果当作新建授权。新建应用须有明确新建指令。
4. 选定更新目标后使用其 appId、repository 和当前绑定的 subdomain。`domainBound=false` 的 lastSubdomain 只是曾用名称，须确定新的可用名称并匹配 awesome.yaml。
5. A 持久化“业务申请 → 用户、固定请求 ID、完整请求内容”，再 `POST /requests`，把固定 ID 放在 Idempotency-Key。同次超时保留 ID、申请人和内容；新部署才用新 ID。202 仅代表持久受理。
6. `GET /requests/{requestId}` 查询进度，按 pollAfterSeconds 有界轮询。外层 pending/submitted 不是上线；读取 deployment.status / phaseLabel / error。等待超时保存 ID 后结束本次等待，不能重建任务。A 对自己的查询入口另做用户/项目权限检查。
7. 用户退出时 `POST /sessions/revoke`；已受理任务仍会继续。不要伪造 receipt 或将其暴露给前端。

## 附带助手与验证

`scripts/awesome_deploy_client.py` 是无第三方依赖的 Python 3.9+ 后端客户端：从环境变量或受限文件读取凭证，固定端点，关闭代理继承及重定向，启用 TLS 验证，设置网络超时；不实现登录、项目自动选择或自动提交重试。仅只读查询提供命令行入口。完整用法见实施参考。

在本 Skill 目录执行：

```sh
python3 -B scripts/test_awesome_deploy_client.py
```

这些测试用内存 HTTP 替身，不连接生产、Redis、Docker 或飞书。交付时区分“离线契约/助手验证”和“真实环境验收”；没有运行真实链路时不得声称已上线或权限隔离已实测。
