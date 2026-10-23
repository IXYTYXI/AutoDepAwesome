# 独立请求登记与自动投递

此服务运行在**同组网页后端所在服务器或另一台独立主机**，不能与 Awesome Deploy 部署平台共用同一个故障点。网页后端向本机 `127.0.0.1:8099` 提交一次请求；接入服务先写本地 SQLite，再同步尝试写专用飞书多维表格「部署请求」表，随后后台向部署平台投递。平台不可达时，本地记录及已写入的飞书记录保留；后台按固定退避自动投递，同一请求始终使用同一 `Idempotency-Key`。飞书写入失败不阻止已可用的平台部署，之后继续补同步。

## 请求与响应

网页后端完成自己的登录、权限和输入校验后，调用本机服务。浏览器不能直接持有任何接入凭证。第一次请求生成并持久保存唯一请求 ID；后端到接入服务的响应丢失时，用同一 ID 和同一 JSON 重发可取回原记录。同一 ID 对应不同 JSON 返回 409。

```http
POST http://127.0.0.1:8099/requests
Authorization: Bearer <接入服务凭证>
Idempotency-Key: <唯一请求 ID>
Content-Type: application/json

{"repository":"https://github.com/org/repo.git","ref":"main","subdomain":"demo"}
```

`202` 返回 `requestId`、`status`、`jobId`、`appId`、`error`。初始 `status=pending` 只表示独立接入服务已持久接收；平台返回 202 后才有 `jobId` 和 `appId`，并显示 `submitted`；最终状态由平台 GET 同步为 `succeeded`、`failed` 或 `recovery_required`。若平台明确返回 HTTP 400、409 或 422，接入服务记录 `rejected` 和状态码，不再反复投递同一无效请求；其他无法确认结果的错误继续按同一幂等键重试。网页后端用同一接入凭证调用 `GET /requests/{requestId}` 查询，不需要自行对部署平台做不确定重试。若请求连接入服务都未到达，调用方仍必须保存自己的申请意图或明确返回未受理，不能向用户声称已经接收。

## 飞书多维表格

现有 Awesome Deploy 应用登记 Base 已新增独立「部署请求」表（表 ID：`tblgVwWTlwxvQ4Mv`），不能复用「应用」或「部署记录」结果表。字段依次为：`请求 ID`（主文本）、`请求内容`（文本，规范化 JSON）、`完整性签名`（文本）、`状态`（文本）、`部署 ID`（文本）、`应用 ID`（文本）、`提交时间`（日期时间）、`更新时间`（日期时间）。请求 JSON 只允许 `repository`、`ref`、`subdomain`、可选 `appId`，不含 Token、密码、业务密钥或日志。接入服务的飞书应用身份必须具有该表编辑权；若飞书按整个 Base 授予权限，不能将其宣称为仅有单表权限。恢复时只接受与本地完整性密钥匹配的记录，手工改表不能触发部署。完整性密钥与平台 Token 必须独立备份。

原有两张结果表仍由平台 `base-sync` 异步同步，职责不变。新表负责平台尚未接收时的请求留痕和恢复。接入服务启动时从新表恢复本地缺失且签名有效的记录；表中已记录最终状态的请求不会重投，其他记录仍使用原请求 ID 和平台幂等键。飞书不可用时，本地 SQLite 继续保管并投递；本地数据库与飞书同时永久丢失则无法恢复。若表格停留在旧的待投递状态、接入端本地库丢失，而且平台数据库也丢失，不能仅靠表格保证“恰好一次”，应先恢复平台数据库备份再开放投递。

## 独立主机配置

安装本仓库包后，复制 [`ops/intake-relay.example.json`](../ops/intake-relay.example.json) 至 `/etc/awesome-intake/config.json`，替换为真实 Base 和表 ID。所有路径为绝对路径，配置中只放文件路径、不放秘密值。以下文件由 `awesome-intake` 账号持有，权限 `0400` 或 `0600`：`platform-token`（平台签发的具名来源凭证）、`intake-token`（网页后端调用本地服务的独立凭证）、`integrity-key`（至少 16 字节随机值）、`feishu-app-secret`。运行账号独占 `/var/lib/awesome-intake`，数据库及备份按该账号保护。服务只绑定 `127.0.0.1`，不要将 8099 端口直接暴露到公网；若网页后端在另一台主机，应在其主机上运行接入服务或使用经身份校验的专用内部通道。

`awesome-deploy-intake --config /etc/awesome-intake/config.json` 启动服务；[`ops/awesome-deploy-intake.service`](../ops/awesome-deploy-intake.service) 是 systemd 示例。首次上线时用测试仓库验证：平台可用时返回部署 ID 和应用 ID；停止平台 API 后仍取得 `requestId` 并在飞书新表看到记录；恢复平台 API 后自动取得同一个任务；暂停飞书访问时平台部署仍可提交，恢复后表格补齐。不要用生产业务应用做故障注入。
