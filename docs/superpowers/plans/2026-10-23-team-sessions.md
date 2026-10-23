# 同组共享会话 Implementation Plan

**Goal:** 交付显式授权的共享登录态和可靠请求投递，不破坏现有登录或普通 API。
**Architecture:** Redis 签名会话 + A SQLite outbox + B 来源凭证与申请人授权 + 来源级幂等映射。
**Tech Stack:** Python, FastAPI, redis-py, SQLite, HMAC-SHA256。

- [ ] tests/test_team_integration.py：先验证缺失模块失败；覆盖身份隔离、签名篡改、过期、退出后已受理重试、响应丢失、来源撤销、权限、凭证轮换。
- [ ] team_sessions.py：实现配置校验、Redis 会话签发/读取/注销、请求签名，远端 TLS。
- [ ] team_outbox.py：请求与凭据事务持久化，幂等冲突、恢复投递、退避、状态查询。
- [ ] api_credentials/access/store：显式 delegation_source；分别保留来源负责人和实际申请人。
- [ ] team_api.py：独立 integrations 路由，校验会话或已受理签名，不改变普通 routes。
- [ ] 运维配置与示例：A 同机 loopback HTTP、远端 HTTPS、Redis 独立 ACL。
- [ ] 定向测试与真实 Redis 隔离验收；清理测试服务和临时数据。
- [ ] 飞书文档原地更新并同步手册，发布 B；如缺 A 或 Redis 信息准确记录未接通部分。
