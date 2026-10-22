# 自动化部署 API 认证实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为少量明确指定的人员或自动化来源提供可撤销、范围受限的部署 API 凭证。

**Architecture:** 飞书登录仍负责日常网页操作。独立凭证库只存哈希和权限元数据；独立 Bearer 路由复用原部署校验与持久化，但不接受 Cookie。任务授权绑定凭证 ID，执行前复核。

**Tech Stack:** Python 3.11、FastAPI、SQLite、pytest。

---

### Task 1: 凭证生命周期

- [ ] 在 `tests/test_api_credentials.py` 写签发、摘要保存、有效期、撤销、人员权限与应用范围的失败测试，并运行确认失败。
- [ ] 新建 `src/awesome_deploy/api_credentials.py`，实现最小凭证表和签发、认证、列出、撤销；复用 `AccessStore` 的现有权限校验。
- [ ] 运行凭证测试，并添加边界用例直到全部通过。

### Task 2: 专用部署路由和持久授权

- [ ] 在 `tests/test_automation_api.py` 写提交、查询、幂等、Cookie/Basic 拒绝、范围隔离和撤权测试，运行确认失败。
- [ ] 修改 `src/awesome_deploy/api.py`、`src/awesome_deploy/store.py`、`src/awesome_deploy/access.py`，加入独立 Bearer 端点和同事务的凭证任务绑定。
- [ ] 运行 API、Store、worker 和授权测试。

### Task 3: 管理命令、文档与验收

- [ ] 在 `tests/test_cli.py` 写凭证签发、列出和撤销命令测试，运行确认失败。
- [ ] 在 `src/awesome_deploy/cli.py` 加入管理员命令，更新 `README.md` 给出调用示例与安全边界。
- [ ] 运行完整相关测试和静态检查；目标服务器可用时做只读状态及临时凭证验收，最后撤销临时凭证。
