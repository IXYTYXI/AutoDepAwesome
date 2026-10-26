# 负责人搜索实施计划

**目标：** 在现有共享登录链路中查找可见项目负责人，并以平台用户 ID 筛选项目；不改变应用归属或调用者权限。

**已确认规则：** 普通用户仅本人及明确授权为 maintainer 的项目；管理员可查全部；指定后端继承真实用户范围。ownerId 为平台 auth_users.id，不是飞书 open_id。查询必须使用操作者本人的 sessionId。所有过滤先于统计与分页。

- B：新增 POST /api/v1/integrations/owners/search，复用共享身份校验；返回可部署项目涉及的唯一负责人 ownerId/displayName。按姓名和 ID 稳定排序、分页，不提供全员目录。应用搜索新增可选 ownerId，结果补充 ownerId/ownerName；无负责人为 null。
- A：新增 POST /owners/search；透传 ownerId 和身份，保持已有错误语义。Skill 客户端增加负责人查询及应用 owner_id 参数，不自动选择重名或第一项。
- 测试先行：验证管理员、本人、maintainer、viewer、禁用用户、过期会话、未知负责人、重名、无负责人及分页。运行 `.venv/bin/pytest -q tests/test_team_search.py tests/test_team_search_adapter.py tests/test_team_client.py tests/test_team_integration.py`，新增测试文件纳入同次检查。
- 更新同组接入说明、飞书 API/同组实施手册；从更新后飞书原文生成网页手册快照。字段表、请求响应、身份/负责人区别及错误说明保持一致。
- 独立代码审查、相关回归与完整 Python 回归；打包发布和 Skill，远端健康检查及只读接口验收。提交并推送既有 GitLab，新功能不重置历史。

**验收示例：** 甲拥有项目 P，乙无授权时搜索 P 的 ownerId 返回空；授权乙 maintainer 后返回 P；管理员用自己的 sessionId 可查甲的 P。修改负责人筛选不会更换 session、转移负责人或替代最终部署权限检查。
