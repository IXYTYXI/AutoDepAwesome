# API 契约

适用于 Awesome Deploy 0.11.4。来源：`docs/team-integration.md` 和 `src/awesome_deploy/team_server.py`、`team_api.py`、`team_outbox.py`、`team_sessions.py`（2026-10-26）。本文件随 Skill 分发，不依赖读者拥有源码；若目标版本不同，先让平台运维确认差异，不猜接口。

## 地址、凭证和身份

A 后端 → 适配服务：典型同机地址 `http://127.0.0.1:8099`，以下每个请求都带 `Authorization: Bearer INTERNAL_INTAKE_CREDENTIAL`；JSON 请求带 `Content-Type: application/json`。这枚内部凭证与 B 专用的 source 委托凭证不同。前端不能持有任意一枚；适配服务拒绝带 Origin 或 Sec-Fetch-Site 的浏览器请求。

地址来自受控配置，只允许 loopback HTTP 或经正常证书校验的 HTTPS。容器中的 127.0.0.1 是容器自己；需运维提供受控连接，不能擅自开放公网 8099/Redis、启用 host 网络或启动本机 Docker。所有 HTTP 客户端必须设超时且禁用重定向，防止 Bearer 被转发。

同一 Feishu App ID + tenant_key 才能共享 open_id；不同 App 的 open_id 不能直接互换，不用姓名匹配。首次进入 B 的用户是普通成员，既有账号保留 B 的本地角色、禁用状态和 can_deploy。source 标记只确定来源，不能赋予用户 admin。

## 1. 会话

`POST /sessions` → HTTP 201：

```json
{"identity":{"appId":"cli_example","tenantKey":"example-tenant","openId":"ou_example","name":"示例用户"},"ttl":3600}
```

以上均为虚构示例，真实 identity 只取自 A 后端已验证的身份结果，且只允许这四个字段。openId 形如 `ou_` 加 1–128 位字母、数字、下划线或中划线；name 为 1–256 字符，不含控制字符。ttl 为 1–28800 的整数，省略为 28800；实际应取上游剩余有效秒数与 28800 的较小值。

```json
{"sessionId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","expiresIn":3600}
```

sessionId 实际为 43 位 `[A-Za-z0-9_-]` 随机标识。示例字符串不是真实会话。Redis 只保存签名身份和绝对有效期，不保存密码或 OAuth token，无滑动续期。

`POST /sessions/revoke`，body 为 `{"sessionId":"服务端保存的会话"}` → `{"revoked":true}`。退出和重新登录时撤销原会话；撤销失败不得声称成功。会话过期/退出阻止新申请，不取消已持久受理的任务。

## 2. 搜索负责人和选择应用

### 2.1 按姓名查询负责人

`POST /owners/search` → HTTP 200：

```json
{"sessionId":"当前操作者服务端保存的会话","query":"张三","limit":20,"offset":0}
```

```json
{"items":[{"ownerId":"platform-user-123","displayName":"张三"}],"total":1,"limit":20,"offset":0,"nextOffset":null}
```

负责人候选仅来自当前用户有权部署的项目：普通用户可查本人拥有、明确授权维护的项目的负责人；启用且有 can_deploy 的管理员可查全部项目的负责人。没有可部署项目的人员不出现在候选中，这不是企业通讯录接口。先限制项目权限，再按姓名或平台用户 ID 不分大小写包含匹配、去重并分页。query 省略或为空表示列出权限范围内的候选；分页约束同项目搜索。

同名人员可能返回多条，按 ownerId 区分并由调用者明确选择。不能仅凭同名自动选首项、用飞书 openId 代替 ownerId，或换用目标负责人的 sessionId。sessionId 始终是当前操作者自己的共享会话。

### 2.2 搜索项目

`POST /apps/search` → HTTP 200：

```json
{"sessionId":"当前操作者服务端保存的会话","ownerId":"platform-user-123","query":"demo","limit":20,"offset":0}
```

- sessionId 必填，放在 JSON，不放 URL；receipt 不能代替它。不得退化为无用户的全项目查询。
- query 最多 200 字符；按当前/历史 subdomain、repository 或 appId 不分大小写包含匹配。空 query 表示当前用户可部署项目。
- ownerId 可选，省略或 null 时不按负责人筛选；填写时必须为负责人查询返回的平台用户 ID，1–128 字符。空字符串无效。它只筛选项目当前所有者，不代表部署提交人、不转移归属，也不授予权限。目标不在当前权限范围内时返回空结果，不能泄露他人项目是否存在。
- limit 是 1–20 的整数，默认 20；offset 是 0–1000000 的整数，默认 0。布尔值不是整数参数。
- 权限过滤先于负责人筛选、关键词匹配、total 和分页：owner / maintainer 可部署，viewer 不够；启用且有 can_deploy 的 admin 可查全部。

```json
{"items":[{"appId":"app-example","projectName":"demo","repository":"https://github.com/example/demo.git","subdomain":"demo","lastSubdomain":"demo","domainBound":true,"ownerId":"platform-user-123","ownerName":"张三"}],"total":1,"limit":20,"offset":0,"nextOffset":null}
```

nextOffset 为数字时原样用于下一页，保持 sessionId/ownerId/query/limit；null 表示结束。返回的 ownerId/ownerName 表示项目当前负责人；历史项目未分配负责人时两者可为 null，这种项目不产生负责人候选。错误不能转换成空 items。由用户明确选择目标，或核实其已经明确给出的 appId；不按排序选首项。`domainBound=false` 时 subdomain 为 null，lastSubdomain 不代表域名仍归该项目。

## 3. 持久受理

`POST /requests`，带唯一 `Idempotency-Key: example-request-001` → HTTP 202：

```json
{"deployment":{"appId":"app-example","repository":"https://github.com/example/demo.git","ref":"main","subdomain":"demo"},"sessionId":"服务端保存的会话"}
```

仅新建时省略 appId；更新必须明确 appId，repository 和当前绑定域名须匹配。subdomain 为 1–7 位小写字母、数字或中划线，首尾为字母或数字，且与仓库 awesome.yaml 一致。ref 默认 main。deployment 只接受 repository、ref、subdomain、appId，各值为非空字符串（不超过 4096 字符）；必填 repository、subdomain。服务端仍是仓库、配额、权限和域名可用性校验的最终依据。

请求 ID 是 1–128 位 `[A-Za-z0-9._:-]`。A 应在首次 POST 前持久化 ID、申请人、payload 与业务请求关系；同一来源、ID 对同一人同一内容去重，换人或换内容返回 409。响应丢失不证明失败：先 GET 原 ID；若确认未找到且仍需要提交，用原 ID/申请人/内容重试，不能生成新 ID。会话过期时重新验证同一用户，不能伪造身份或 receipt。助手不会自动 POST 重试。

```json
{"requestId":"example-request-001","status":"pending","jobId":null,"appId":null,"error":null,"createdAt":1800000000.0,"updatedAt":1800000000.0,"pollAfterSeconds":5,"deployment":null}
```

202 表示适配服务 SQLite 事务已提交。B 不可达时后台仍会保存并退避重试，最长 300 秒；POST 的 400/403/409/422 是明确拒绝，401 保留请求等待凭证维护。用户会话不能替代服务凭证。

## 4. 查询进度与错误

`GET /requests/{requestId}` → HTTP 200，字段同上。GET 不创建任务，没找到返回 404。A 自己的网页查询接口必须验证申请属于当前用户，或用户具有相应项目部署详情权限（admin、owner、maintainer），不能仅靠“知道 requestId”或全服务内部凭证。

外层 status 为 pending / submitted 时尚未结束，succeeded / failed / recovery_required / cancelled / rejected 为终态。外层 error 表示投递错误；已取得任务后 deployment 为 B 的快照，含：

| 字段 | 含义 |
| --- | --- |
| id、appId | B 任务 ID、应用 ID |
| status、statusLabel | queued/running 或 succeeded/failed/recovery_required/cancelled，以及中文说明 |
| phase、phaseLabel | 当前阶段及中文说明；没有百分比字段 |
| terminal | 是否结束，true 不等于成功 |
| error | 业务部署的安全错误对象或 null |
| createdAt、updatedAt | Unix 秒数 |
| pollAfterSeconds | 进行中建议 5 秒，终态为 0 |
| active、url | 该版本在最后同步时是否在线及地址 |

有界轮询；等待超时只结束调用方等待，保留 requestId/jobId 后可继续查询。不要把 pending、submitted、queued 或 terminal=true 报为上线。A 终态后不再持续刷新快照；确认当前在线状态要让 A 后端使用本来源有效委托凭证直接 GET B 任务。

| HTTP | 处理方式 |
| --- | --- |
| 400 / 413 / 415 / 422 | 检查字段、体积、JSON 和 Content-Type；纠正后再提交 |
| 401 | 核对用户登录/共享会话；来源凭证问题交运维，不降级身份 |
| 403 | 当前用户、来源或应用权限拒绝，不换身份重试 |
| 404 | 查询 ID 不存在；区分未受理与未查询正确来源 |
| 409 | 冲突或同 ID 换人/换内容；保留记录并排查，不能换 ID 绕过 |
| 503 / 网络超时 | 暂不可用；查询原 ID、有限重试，不能假报空结果或失败 |
| 3xx | 停止；检查受控配置，不跟随重定向发送 Bearer |

## 5. B 端协议（仅维护直接集成时读取）

推荐通过适配服务接入。B 的接口为 POST `/api/v1/integrations/owners/search`、POST `/api/v1/integrations/apps/search`、POST `/api/v1/integrations/deployments`、GET `/api/v1/integrations/deployments/{jobId}`。B Bearer 必须是运维签发且配置 delegation_source 的专用 source 凭证；普通 personal/source 不自动获得委托权。

委托 source 凭证不能调用普通 `/api/v1/automation/deployments` 提交或查询接口；需要普通自动化调用时，使用独立非委托凭证，不能绕过 integrations 的实际申请人校验。

搜索 JSON 与适配服务相同；部署 envelope 含 deployment，sessionId 或 receipt 必须且只能一种。适配服务在受理时生成绑定身份、请求 ID、部署摘要和来源的 HMAC receipt 并持久化；后台重放无需用户仍在线。不要自行构造 receipt，不写入前端、普通日志或飞书。直接提交 B 缺少 A 的可靠 outbox，不是等价替代。

B 的 GET 按来源授权，可在申请人退出后查询；A 仍需做最终用户查询权限。管理员更新他人应用保留原 owner。原凭证撤销会阻止其绑定的待执行任务；轮换先让未完成请求用新来源凭证及原 ID 重投，再由运维撤销旧凭证。签名密钥在待处理 receipt 完成前不能直接替换。
