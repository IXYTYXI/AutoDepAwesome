# 同组后端接入：共享登录态与可靠部署

适用 0.11.8，权限说明核对于 2026-10-26。A 是将来部署在本机的同组项目，B 是 Awesome Deploy。A 项目尚未存在；本次提供 A 端适配服务、共享会话和 B 端委托接口，并不代表 A 业务已接通。原有 `/api/v1/automation` 接口保留普通个人/非委托来源凭证调用，使用凭证负责人的身份；绑定 `delegation_source` 的来源凭证必须走共享登录委托接口，调用普通自动化接口返回 403。同组共享登录态方案使用 `/api/v1/integrations`：新建应用归实际登录申请人；更新已有应用保留原负责人，本次部署提交人记录为实际操作用户。

## 1. 服务与身份

A 后端完成飞书 OAuth 后，以受保护的内部凭证调用本机适配服务签发共享会话。会话存 Redis，默认 8 小时绝对过期，可配置更短时间，无滑动续期。B 验证来源凭证及共享身份，按申请人的本地权限部署。客户端不得决定 openId、角色或负责人。只接受与 B 相同飞书 App ID、tenant_key 的 open_id；不同 App 的 open_id 不能直接互换，不通过姓名匹配。

现行本机地址：Redis `127.0.0.1:6391`；A 端适配服务 `127.0.0.1:8099`；B `127.0.0.1:8088`，内部 HTTP 设置 Host 为 `awesome.guanghexinzhi.cn`。浏览器只访问 A，不能访问 8099 或持有内部凭证。容器中的 127.0.0.1 是容器自己；若 A 以后部署在容器，必须先配置受控的宿主机连接，不要直接开放公网端口或使用 host 网络来绕过平台策略。

A/B 共用登录身份并不等于共用密码或飞书授权。Redis 只保存签名身份和有效期，不保存密码、飞书 access_token 或 refresh_token。若 B 将来需要以用户身份读取/创建飞书文件，那是另一项 OAuth 资源授权；当前部署链路不需要。部署结果登记使用平台已有的飞书应用身份。

### 1.1 共享登录态、搜索范围与部署归属

指定后端继承的是上游服务已经验证的实际用户登录态。上游就是 A 时，由 A 校验 OAuth 回调；上游是另一个登录服务时，A 必须先通过可信后端链路验证其登录会话，再把对应用户关联到共享 sessionId。当前适配组件不会自动读取任意服务的 Cookie，也没有提供任意登录服务的自动接入；不得直接相信浏览器传来的 openId、姓名或管理员标志。

| 调用身份 | 项目搜索范围 | 部署归属 |
| --- | --- | --- |
| 普通用户 | 自己拥有的项目，以及明确授予 maintainer 部署权限的项目；没有授权的项目不返回，仅 viewer 权限也不进入此可部署项目搜索 | 新建项目归本人；更新时仍检查该项目部署权限，保留原负责人 |
| 平台管理员 | 具有有效部署权限时可以搜索全部项目 | 新建项目归实际操作的管理员；更新别人的项目不转移负责人，本次提交人是管理员 |
| 指定同组后端服务 | 使用上游实际用户的有效共享会话，范围与该用户一致；用户是普通成员就按成员范围，是平台管理员就按管理员范围 | 按实际登录用户受理和记录，不统一归到服务凭证持有人 |

三类入口都受本地账号启用状态和 can_deploy 限制；被禁用或暂停部署权限时返回权限错误。这里的管理员指 B 保存的本地管理员角色，A 不能通过请求字段指定。服务与 B 位于同一台服务器不会自动获得权限。内部接入凭证和 B 的专用来源凭证分别验证受信后端及来源，不替代用户身份，也不把凭证负责人的管理员权限借给用户。

例如甲拥有项目 P，乙没有 P 的授权：甲经 A 搜索可以得到 P，乙经同一个 A、同一枚来源凭证搜索仍看不到 P；乙猜到 appId 后直接提交更新也会被拒绝。乙被明确授予 P 的 maintainer 权限后可以搜索和更新 P，P 的负责人仍是甲，部署记录的提交人是乙。管理员可以搜索 P 并更新，项目负责人仍不自动改变。

普通用户不能用个人 Bearer 直接调用同组搜索接口，不等于普通用户不能搜索：正常路径是“用户登录上游服务 → A 取得该用户共享 sessionId → A 使用专用来源链路代其搜索”。搜索必须带有效 sessionId，不能省略会话退化为全项目服务搜索，也不能传 receipt 代替搜索会话。

## 2. A 后端接入顺序（任何语言均可）

以下接口都必须由 A 后端调用，带 `Authorization: Bearer <内部接入凭证>`。凭证由运行环境安全注入，不能出现在浏览器、Git、聊天或日志。

1. OAuth 回调校验成功后，取已验证的飞书身份，调用 `POST http://127.0.0.1:8099/sessions`。把返回 sessionId 关联到 A 自己的服务端登录会话，不交给用户填写。
2. 用户提交部署时，A 检查自己的申请权限，从服务端登录态取 sessionId；给此次业务申请生成并保存固定请求 ID，调用 `POST /requests`。
3. 返回 202 表示请求已事务落入 A 适配服务数据库。前端显示“已受理”，后台通过 `GET /requests/{requestId}` 查询。GET 不创建任务。
4. 用户退出 A 时，调用 `POST /sessions/revoke`。已持久受理的任务继续处理；后续新申请不能使用旧会话。
5. A 自己的请求查询接口必须校验请求属于当前用户，或该用户具有项目部署详情读取权限（admin、owner、maintainer；仅 viewer 不足）；内部接口的凭证属于整个 A 服务，不替代 A 网页的数据权限。

会话签发请求（identity 必须取自服务器验证的 OAuth 结果）：

```json
{"identity":{"appId":"cli_aa04dd8797f8dbd1","tenantKey":"平台约定的企业标识","openId":"ou_实际登录用户","name":"实际显示名"},"ttl":28800}
```

返回 `{"sessionId":"随机不透明标识","expiresIn":28800}`。若 A 现有登录剩余有效时间不足 8 小时，ttl 取剩余秒数。退出请求：`{"sessionId":"原会话标识"}`。A 重新登录时先撤销旧共享会话，再签发新会话。会话签发、退出不能用前端未经核实的用户信息调用。

提交部署请求：

```http
POST /requests HTTP/1.1
Host: 127.0.0.1:8099
Authorization: Bearer <内部接入凭证>
Content-Type: application/json
Idempotency-Key: project-a-request-001

{"deployment":{"repository":"https://github.com/your-org/your-repo.git","ref":"main","subdomain":"demo"},"sessionId":"服务端保存的共享会话标识"}
```

```json
{"requestId":"project-a-request-001","status":"pending","jobId":null,"appId":null,"error":null,"deployment":null}
```

B 接收后 jobId、appId 非空，外层状态 submitted。外层 pending/submitted 表示投递阶段，不代表已上线；部署进度读取 `deployment` 的状态和阶段。最终为 succeeded、failed、recovery_required 或 cancelled；平台明确拒绝时为 rejected。

更新已有应用：在 deployment 中加入 appId，仓库与当前绑定域名须匹配，申请人必须有该应用的部署权限。项目名称通过 subdomain 提交：1–7 位小写字母、数字或中划线，首尾为字母或数字，不含中文、空格、大写字母，并与 awesome.yaml 一致。ref 可省略，默认 main。每次新部署新请求 ID；同次超时重试始终保留 ID 和请求内容。请求 ID 为 1–128 位 `[A-Za-z0-9._:-]`。一次来源凭证可持续创建和更新多个获授权应用，没有累计申请数量上限；现有 worker 仍串行执行，受资源硬配额限制。

### 2.1 搜索已有项目


先按姓名查找负责人时，A 后端调用 `POST /owners/search`（B 对应 `POST /api/v1/integrations/owners/search`），继续携带实际操作者的共享会话与服务凭证：

```json
{"sessionId":"当前操作者的共享会话","query":"示例用户","limit":20,"offset":0}
```

返回 `items: [{"ownerId":"平台用户ID","displayName":"示例用户"}]` 和 `total/limit/offset/nextOffset`。候选人仅来自当前用户有权部署的项目：普通用户是本人及明确授权为 maintainer 的项目所涉及负责人，管理员为全部项目负责人；不提供全员通讯录。姓名可能重复，必须明确选择 ownerId。ownerId 是平台用户 ID，不是飞书 open_id；始终使用操作者自己的 sessionId，不能换用目标负责人的会话。

将选中的 ownerId 带入项目搜索：

```json
{"sessionId":"当前操作者的共享会话","ownerId":"选中的平台用户ID","query":"","limit":20,"offset":0}
```

ownerId 可选，省略或 null 保持原范围；提供时为 1–128 字符、非空且非纯空白字符串。普通用户的筛选只能缩小已有权限范围；不可见或未知负责人返回空结果，不能泄露其项目和数量。项目结果新增 ownerId/ownerName。未分配负责人的项目仅管理员可见，这两个字段为 null，且不进入负责人候选。管理员可以管理已停用负责人名下的项目，不恢复该负责人权限。负责人候选按姓名（忽略大小写）与 ID 排序、去重后分页；负责人搜索 query 匹配姓名或 ID，项目 query 匹配项目名称、仓库、appId。

A 后端调用 `POST http://127.0.0.1:8099/apps/search`，使用内部接入 Bearer。适配服务使用专用委托来源凭证调用 B 的 `POST /api/v1/integrations/apps/search`；JSON 相同：

```json
{"sessionId":"服务端保存的共享会话标识","query":"demo","limit":20,"offset":0}
```

sessionId 必填，取自已验证的上游登录态，放在 JSON，不放 URL。query 最多 200 字符，按当前/历史项目名称（subdomain）、repository 或 appId 不区分大小写包含匹配；空字符串列出当前用户所有有权部署的项目，不是所有人的项目。limit 为 1–20 的整数，默认 20；offset 为 0–1000000 的整数，默认 0。权限过滤先于负责人/关键词匹配、total 统计和分页。下一页保持 ownerId、query、limit、sessionId，只更新 offset。来源凭证的管理员身份不会扩大实际登录用户的权限；普通 personal 或未开启委托的 source 凭证不能直接调用此接口，普通用户经 A 的共享登录态链路可以搜索。

```json
{
  "items": [{
    "appId": "已有应用编号", "projectName": "demo",
    "repository": "https://github.com/your-org/your-repo.git",
    "subdomain": "demo", "lastSubdomain": "demo", "domainBound": true,
    "ownerId": "平台用户ID", "ownerName": "示例用户"
  }],
  "total": 1, "limit": 20, "offset": 0, "nextOffset": null
}
```

nextOffset 为数字时，把它作为下一次 offset，保持会话、query、limit 不变；null 表示结束。total 仅统计有权限且匹配的项目，无匹配返回空 items。出现多个匹配时由用户明确选择，不能自动部署第一项。

选定结果后，将 appId、repository 和已绑定的 subdomain 放入 deployment，调用 `POST /requests`，再按 requestId 查询。`domainBound=false` 时 subdomain 为 null，lastSubdomain 只是曾用名称；须先选空闲且符合清单的项目名称，不得把历史名称当成保留域名直接复用。提交时会重新核对权限、仓库与域名。

搜索不创建请求或部署，不需要幂等键。会话失效或来源凭证需维护可返回 401；核对登录态后由运维处理来源凭证轮换。权限拒绝 403，参数错误 422，Redis/B 不可用 503。搜索错误不应在界面伪装成“没有项目”。

### 2.2 查询进度

A 后端用内部接入凭证调用 `GET /requests/{requestId}`。A 必须先校验记录属于当前用户，或该用户具有对应项目的部署详情读取权限（admin、owner、maintainer；仅 viewer 不足），因为内部接口按整个来源授权。返回 requestId、status、jobId、appId、error、deployment，另含 Unix 秒数 createdAt/updatedAt 以及 pollAfterSeconds（未结束 5、已结束 0）。外层 error 是投递错误；业务部署错误读取 deployment.error。deployment 取得前为 null。

B 最新进度接口为 `GET /api/v1/integrations/deployments/{jobId}`，用本来源有效委托凭证；普通自动化任务仍使用 `GET /api/v1/automation/deployments/{jobId}`，用提交时同一凭证。均返回：

| 字段 | 含义 |
| --- | --- |
| id / appId | 部署任务 ID / 应用 ID |
| status / statusLabel | 机器状态码 / 中文状态说明 |
| phase / phaseLabel | 阶段码 / 中文阶段说明，如 build / 构建镜像 |
| terminal | 是否结束；true 不等于成功 |
| createdAt / updatedAt | Unix 秒数（number） |
| pollAfterSeconds | 进行中建议 5 秒，终态为 0 |
| error | 中文安全错误对象，无错误为 null |
| active / url | 该成功版本是否仍在线、当前访问地址 |

queued/running 继续查询；succeeded/failed/recovery_required/cancelled 为终态。不存在百分比字段，各阶段耗时不固定。A 外层还可能 rejected，同样停止轮询并展示 error。调用方等待超时不等于平台任务失败，保存 requestId/jobId 后可继续查询，不创建新任务。

A 在任务结束后不持续更新缓存的 deployment，因此其 active/url 只表示最后同步时的状态。确认当前是否仍在线时，A 后端用来源凭证直接查询 B 任务；不要以 A 历史快照证明当前在线。

完整搜索→选择 appId→更新→轮询的 Python 后端示例见[同组后端实施手册](https://guanghe.feishu.cn/docx/DWPCdke22oEN8Bxwy70cFX9anXe)，网页手册同步同一内容。

## 3. B 端接口与已受理授权

`POST /api/v1/integrations/deployments`：Bearer 是专用来源凭证，必须显式签发 `--kind source --all-apps --delegation-source project-a`。请求 envelope 包含 deployment，并且 sessionId 或 receipt 二选一。直接请求带 sessionId 时 B 实时读 Redis；普通 personal/source Token 不自动获得代理用户的权限。

推荐由已提供的 A 适配服务生成 receipt。它在受理时校验 Redis，事务保存完整部署内容、申请人、请求 ID 和 HMAC 签名受理凭据。后台向 B 发送 `{"deployment":{...},"receipt":{...}}`。B 校验来源、签名、请求内容、身份命名空间和本地权限，不要求用户此时仍保持登录。receipt 不是飞书 Token，但属于授权材料，必须保存在受限数据库，不能上传飞书表格、前端或普通日志。不要手工构造或修改 receipt，复用组件。

B 返回 HTTP 202 和 `{id,appId,status,phase,...}`；GET `/api/v1/integrations/deployments/{id}` 查询任务。相同来源可用轮换后的有效委托凭证查询和重投同一请求，仍返回原任务。不同来源不能查询；同一来源同请求 ID 不允许换申请人或请求内容。原凭证撤销会阻止其绑定的待执行任务；轮换应先把待投递/未完成请求切到新凭证并用原请求 ID 重放，再撤销旧凭证。签名密钥在待处理请求完成前不能直接换掉，否则旧 receipt 无法核验。

申请人首次从 A 接入时 B 创建普通成员，不授予管理员角色。既有账号保留本地禁用状态、权限和角色；A 不能传角色覆盖。会话退出不取消已受理任务，明确禁用账号、撤销来源或撤销应用权限仍会阻止未获执行授权的操作。

## 4. 故障、存储和飞书记录

A 适配服务在 SQLite 事务提交后才返回 202。B 不可达、响应丢失或 5xx 时，后台按原 ID 退避重试（最长 300 秒）；POST 400/403/409/422 作为明确拒绝，不无限重试。401 可能是来源凭证过期或需维护，保留请求并重试，不换 ID。Redis 不可用时不受理新的共享身份申请，返回 503；已经保存 receipt 的请求仍可继续投递。退出登录、会话过期和 Redis 清空都不会清除已提交数据库记录。

本机 SQLite 为 A 的待投递底账；B 数据库为部署状态权威。当前共享会话方案不会把 receipt 或会话写入多维表格；B 收件后，原有“应用”“部署记录”两表异步同步。“应用”表负责人取当前应用所有者，“部署记录”表提交人取该次实际操作用户；更新部署不会把负责人改成提交人或来源凭证负责人。旧独立接入模块的“部署请求”表备份恢复是另一套接口，未自动接入本方案，不能声称新链路在 B 收件前已同步飞书或可以从该表恢复。若需要跨机器故障恢复，应备份 A 数据库、B 数据库及签名密钥，或单独扩展外部请求留痕。

A/B/Redis 暂时同机，因此 B 进程故障可由 A 保留请求，整机停机仍会影响两者。Redis AOF everysec 可能丢失故障前约一秒的会话写入，用户可重新登录；部署请求以 SQLite 记录为准。当前不承诺吞吐量或并发执行扩容。

## 5. 运维交接和迁移

配置示例：`ops/team-sessions.example.json`、`ops/team-intake.example.json`；服务：`awesome-team-redis.service`、`awesome-team-intake.service`。A 侧路径 `/etc/awesome-team`、`/var/lib/awesome-team/requests.sqlite3`；B 侧 `/etc/awesome-deploy/team/sessions.json`，平台配置 shared_sessions_file 指向它。只有受控服务账号可读密钥。

配置中的 source 必须与 registry 和委托来源凭证一致，为 1–40 位小写字母、数字或中划线，首位为字母或数字；registry 最多 32 个来源且不可重名。shared_sessions_file、database_path、platform_token_file、intake_token_file，以及 registry 的两个密钥文件，均使用非空绝对路径，不含控制字符。密钥文件按运行账号所有、0400/0600 权限保管；签名密钥至少 32 字节。

feishu_app_id 使用 cli_ 后接 1–80 位 ASCII 字母或数字，示例 cli_REPLACE 必须换成真实 App ID；tenant_key 使用 1–128 位 ASCII 字母、数字、下划线或中划线。两者必须与 B 相同。redis_username 为不含空白的非空可打印 ASCII 名称，A/B 可以使用各自的 ACL 用户；redis_url 不内嵌账号或密码，不能带查询参数或片段，数据库路径为 /0 至 /99，实际可用库数由 Redis 配置决定。显式端口为 1–65535。

同组 platform_origin 只允许 HTTPS 根地址，或 127.0.0.1、localhost、::1 的本机 HTTP 根地址；独立旧 intake 模块仍要求 HTTPS。地址不能包含 URL 用户名/密码、查询参数、片段、子路径、空白或反斜杠。platform_host 配置项可填 null；设置时只填不带协议、端口和路径的 ASCII 主机名。来源、路径或 URL 配错时会在启动加载配置阶段拒绝，不能把启动失败当成部署请求已受理。

Redis 本机专用端口 6391，64 MiB 内存上限，noeviction；A ACL 仅可操作 project-a 会话键的 GET/SET/DEL，B ACL 只可 GET。默认账号禁用，不开放公网。未来迁到异机时，把两侧 registry 的 redis_url 改为 `rediss://目标主机:端口/0`，配有效 TLS 证书和分别的 ACL 密码文件；代码不允许远端明文 redis://。迁移前暂停会话写入/新申请，迁移数据或让用户重新登录，验证 A/B 指向同一 Redis 后恢复；已经落库的请求不依赖会话迁移。异机 Redis 不是 A/B 数据库备份。

平台来源凭证有效期 1–90 天，运维需到期前轮换。签发命令仅在可信终端执行，输出直接写入接入服务账号的 0600 文件，不复制到文档：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json api-token-create \
  --user-id PLATFORM_RESPONSIBLE_USER_ID --name project-a --kind source \
  --all-apps --delegation-source project-a --days 90
```

## 6. 接入验收

- A OAuth 回调创建共享会话；退出后新请求被拒绝。
- 两位用户分别提交，应用负责人正确，不能更新对方应用；普通凭证不能委托。
- 同一来源凭证配合甲、乙不同 sessionId 搜索时，结果及 total 分别按各自权限过滤；管理员可查全部，普通用户不能查未授权项目，viewer 不能通过可部署搜索取得项目。
- 明确授权 maintainer 后可搜索并更新；更新保留原应用负责人，任务提交人是操作用户。撤销权限后不能继续搜索或新提交更新。
- 搜索缺失、失效或伪造会话时不得降级为全局搜索；来源凭证持有人是管理员也不能提升普通用户权限。
- B 不可达时 A 返回持久请求 ID；恢复后取得 jobId/appId。
- B 已接收但响应丢失时，同 ID 重试只有一个任务；重启 A 适配服务继续投递。
- 已受理后退出登录仍能部署；本地停用、来源撤销和权限撤销按平台授权规则生效。
- 请求最终状态、错误摘要、在线链接可显示，凭证和 receipt 不出现在网页及飞书表格。
- Redis 外部端口不可达，A/B ACL 不越权；迁移与数据库备份分开验收。

## 7. 可分发 Skill 与便捷配置

配套 Skill 为 `skills/awesome-deploy-integration`，包含标准库 Python 客户端、接入顺序和 API 契约。客户端默认连接 `http://127.0.0.1:8099`，默认从 `/etc/awesome-team/intake-token` 读取 Key；也支持 `AWESOME_INTAKE_TOKEN` 和 `AWESOME_INTAKE_TOKEN_FILE`。其他系统账号运行的业务服务由管理员一次性提供凭证文件或环境变量。实际 Key 已写入[同机服务 Key 与快速接入](https://guanghe.feishu.cn/docx/OjSxd1b3goodWrxgl4ec1DLMnPf)，按项目文档权限访问。
