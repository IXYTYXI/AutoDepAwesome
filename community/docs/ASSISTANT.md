# OWS 社区助手（本地测试）

## 入口与范围

登录社区后，右下角圆形 Logo 入口可以拖动；点击打开自适应聊天窗口。
前端只访问本站 `/api/v1/assistant/*`，Aily 调用与工单写入都在后端执行。

当前助手：[OWS 社区助手](https://guanghe.feishu.cn/ai/custom_agent/agent_4mdqh9jvd7kfnnn/builder/persona)。
调用应用：OWS `cli_aa04dd8797f8dbd1`，OpenAPI 调用白名单限定此应用。
该应用已发布 v1.0.9，新增 `aily:agent_chat:read`、`aily:agent_chat:write`。

知识范围：社区使用说明、工具报销文档、首页 FAQ。
- [社区使用说明](https://guanghe.feishu.cn/docx/BYZedrUUcob9Clxy5YNc50MCnNb)
- [工具报销文档](https://guanghe.feishu.cn/docx/Qx1hd8ep7oJGiFx7vDrcxSFAnKg)
- [专用知识空间](https://guanghe.feishu.cn/ai/space/knowledge_space_4mdrf4yawv1a2/knowledge)
- 本地 `app/data/assistant-guide.txt` 与现有 FAQ 缓存提供请求上下文；没有导入整个业务 Base。

助手关闭互联网搜索、全企业知识搜索与内置 Feishu 写操作技能。知识空间仅链接上述社区资料。
模型输出只能建议答案和反馈草稿，无权直接写工单。

## 配置

`.env` 使用已有的 Feishu App ID / Secret，另有：

```dotenv
OWS_COMMUNITY_ASSISTANT_ENABLED=true
OWS_COMMUNITY_ASSISTANT_AGENT_ID=agent_4mdqh9jvd7kfnnn
OWS_COMMUNITY_ASSISTANT_SOURCE_TENANT=<当前飞书企业 tenant_key>
OWS_COMMUNITY_ASSISTANT_FEEDBACK_BASE=CzDvbFSxgaisAbsbVeXcNqWFnYd
OWS_COMMUNITY_ASSISTANT_FEEDBACK_TABLE=tblx7HMvB3ygfS7O
```

默认关闭，当前仅本地 `.env` 开启；没有修改生产服务器配置或发布网站。
开启时必须使用 Redis、指定来源租户与目标表。关闭或租户不符时前端隐藏入口。
通过 `uv run python -m app` 启动。配置变更需重启后端。

## 会话、资源与确认

- Redis 会话按租户+用户隔离，首次打开后固定 3600 秒有效；重新打开、切换页面和发送消息不延长到期时间。
- 到期重新创建本站和 Aily 会话。Redis 到期不等于删除飞书端历史记录，远端历史仍受飞书平台保留策略控制。
- 不跨用户缓存答案；公共 FAQ 仍复用原有 Redis 缓存。Aily OAuth token 按应用缓存，提前 120 秒过期。
- 同一用户请求使用 Redis 锁，最多 4 个全局在途对话。单用户发言间隔至少 3 秒，每会话最多 80 条消息。
- 前端每 2.4 秒查询进行中的回答；关闭窗口暂停查询。后端至少 2 秒才请求一次 Aily，170 秒未完成返回可重试提示；容量锁 180 秒兜底释放。
- HTTP 请求最长受 25 秒会话锁内超时约束。关闭页面会清理前端定时器和请求。
- 每条问题最长 2000 字；只传页面路径，不传查询参数、登录令牌或密钥。
- 所有接口需要本站登录；Cookie 写请求需要 Origin 和 CSRF 校验。用户身份由服务端读取。
- 工单草稿必须点击“确认提交”；取消不会写表。服务端验证草稿 ID 属于当前会话。
- 使用稳定 UUID 作为飞书 `client_token`，确认重试幂等；只有收到真实 record_id 后显示提交成功。
- 答案只以 Vue 文本、强调和 HTTP(S) 链接展示，不执行模型 HTML。

## 工单与通知

[OWS Community · 反馈工单](https://guanghe.feishu.cn/base/CzDvbFSxgaisAbsbVeXcNqWFnYd?table=tblx7HMvB3ygfS7O&view=vewblJ48v2)

字段：工单编号、标题、类型、问题描述、所在页面、提交人、提交人标识、状态、处理备注、创建时间。
OWS 应用具有此专用 Base 的编辑权限。工作流“新工单提醒王岩”已开启，支持 OpenAPI 新增记录，向王岩发送工单通知卡片。

2026-11-02 真实联调：工单 `OWS-20B03712`，记录 `reczz28Ui4kxZ7rR`，标题“联调测试 - 案例库增加收藏功能”。
已核对飞书提醒送达：14:27 消息 `om_x100b61b69b82d0b4c1fc933b3276e22`，包含标题、提交人、描述、查看工单链接。
该测试记录保留用于检查，未重复创建。

## 验证结果

- 后端 `uv run pytest -q`：175 项通过；现有 Starlette/httpx 弃用提示 1 条。
- 前端 `npm --prefix frontend test`：27 项通过；Vite 生产构建通过。
- 实际 Aily 问答、实际飞书工单写入和提醒已验证。
- 390×844 手机视口与桌面视口检查完成。拖拽实测移动 90×120 像素，无误触展开；关闭重开可续用已有问答。
- 截图：`output/playwright/assistant-desktop.png`、`assistant-mobile.png`、`assistant-bubble.png`。
