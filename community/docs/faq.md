# 首页 FAQ

数据表：https://guanghe.feishu.cn/wiki/Rg2Dw2MLii1fpNkma0EcFSIWn4e?table=tblJSNXiez1lKLGm&view=vew6kNRlaP

使用既有文本字段「问题」「回复」。两列均非空才展示，保留指定视图的返回顺序；空记录及未填写回复的问题不会展示。不写入示例问答。

前端首页只请求本站 `GET /api/v1/faq`。后端验证登录和来源租户，优先使用统一 Redis 快照，缓存 600 秒，过期后的首次访问回源飞书。并发回源互斥，异常时可使用最长 24 小时的上一版快照。FAQ 与每 10 分钟执行的 AIHOT 增量任务独立。

在表中填写问答后，下次缓存刷新即可在首页展开阅读。内容作为纯文本显示，保留换行，不执行表内 HTML。

本机配置已填好；服务器通过 `.env.example` 中的 `OWS_COMMUNITY_FAQ_*` 设置表、视图、缓存和来源租户。复用现有飞书应用与 Base token；运行时不使用 CLI 的个人身份。
