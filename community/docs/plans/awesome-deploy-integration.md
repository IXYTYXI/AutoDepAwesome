# OWS Community 部署能力内置方案（修订）

2026-10-27。按用户最新意图：将 Awesome Deploy 融合进社区后端，不采用社区 HTTP 调用另一套部署 API 的方案。
分析基于用户提供的 0.11.8 压缩包；尚未修改功能代码、连接生产服务器或执行部署。

## 目标架构

同一个仓库、uv 依赖环境与配置入口；一个面向前端的 FastAPI 服务，一套社区飞书登录；部署执行采用同项目的独立 Worker 进程。

Vue → OWS /api/v1/deployment → 部署应用服务 → 内置部署引擎 / SQLite 持久队列 → 独立 Worker → Docker / HTTPS 网关。

Redis 继续负责会话和短缓存。SQLite 负责应用、归属、任务、幂等、执行检查点、域名/资源占用、密钥元数据。此前社区无本地数据库的设计需要在部署模块增加这一例外；不能将部署状态改为会过期的缓存。

## 模块组织（拟定）

- app/api/deployment.py：社区鉴权后的部署 API。
- app/schemas/deployment.py：输入、进度及结果 schema。
- app/services/deployment.py：参数校验、身份映射、权限、入队与查询。
- app/services/deployment_identity.py：将已验证的社区飞书身份映射为引擎用户。
- app/workers/deployment.py：同项目独立执行器入口。
- packages/deploy-engine/：本地 Python 包，保留 awesome_deploy 模块边界与原引擎测试，以 uv 本地依赖管理。
- var/deployment/：开发状态目录；生产配置独立持久目录，不能落在随版本替换的临时目录。

## 复用与适配

复用原 Store、AccessStore、Worker、GitSource、DeploymentBackend、清单/Dockerfile 校验、诊断、资源配额、持久存储、回滚恢复、密钥库及其必要依赖。

不对外挂载原完整 FastAPI 子应用：原 api.py 自带登录、Cookie、CSRF、网页、异常与 app.state，直接挂载不会自动继承社区身份，容易产生两套登录和重复路由。

通过新服务层直接调用 Store.enqueue(..., actor=...)。该方法已支持没有自动化凭证的用户提交，包含幂等、应用归属及权限检查。提取并复用原 API 的输入校验与安全结果格式，转换成社区统一响应；不得直接返回原始数据库记录或命令日志。

这条内置链路不需要 team intake HTTP、共享 sessionId、来源 Token 或受理 receipt 中转。原包相关模块可先保留为未启用能力，避免初期删除依赖破坏引擎；后续再决定裁剪。

## 身份与权限

社区 OAuth 是唯一浏览器登录。由后端验证过的 appId/tenantKey/openId 创建或关联引擎用户，保留引擎本地 enabled、can_deploy、角色及应用 owner/maintainer/viewer。

普通新用户默认成员；不自动把社区 admin 映射为部署 admin。已有用户不可因重新登录被恢复已撤销权限。部署详情读取和更新都检查归属/授权。

Worker 执行前继续复核持久化操作授权。原 Worker 会通过 IdentityStore 做身份可用性检查，融合时必须适配同一身份提供器/飞书配置，保留本地撤权及执行前校验，不能因去掉第二套登录就跳过这些检查。Worker 不依赖浏览器 Cookie。

## API 与执行进程

API 完成鉴权、CSRF、输入校验、事务入队后返回 202 + jobId。数据库事务成功才表示已受理。

Worker 负责 Git、构建、容器、HTTPS、健康检查和恢复，保持现有全流程文件锁和串行执行语义；不使用 FastAPI BackgroundTasks 执行真实部署，不在每个 Uvicorn worker 的 lifespan 启动执行器。

开发统一启动入口可拉起前端、API 和模拟 Worker；生产由 systemd 或等价进程管理器分别托管 API 与单 Worker，代码/配置仍归同一个项目。实际 Docker/网关/配额执行在满足条件的 Linux 服务器上验收。

## 首期 API / 页面

- 配置与资源规则、创建部署、查询本人任务、本人任务列表、下载部署配置 Skill。
- 保留四步向导；提交后显示排队、获取代码、校验、构建、启动、健康检查、发布验证节点。
- 固定 main，保留 1–7 位项目名称规则，补齐仓库白名单/保留名称等后端校验。
- 同次提交和不确定结果重试用同一幂等键；刷新按 jobId 恢复，失败后重新申请生成新键。
- 状态约 5 秒查询；鉴权后可做 2–5 秒短缓存。终态停止；只有 succeeded 表示成功。
- 第一版不开放下线删除、密钥管理、审批与更新别人项目；需要密钥的应用先用已有受控配置流程。

## 首期仍需处理的运行条件

- 原引擎构建网络为 none；配置 Skill 要提供预构建镜像/离线依赖方案。
- 统一配置里增加 deploy.enabled、state_dir、worker 模式及 Docker/网关/配额设置。
- 持久目录与 SQLite 在 API/Worker 间受控共享；Worker 拥有部署所需权限，避免把完整 Docker/宿主机管理权限赋予网页进程。
- 不把生产 Worker 放进自己管理的普通应用容器中自我部署。
- 若接管现有线上平台，保留应用 ID、数据库、冻结快照、卷、配置和密钥；备份并暂停旧执行器后切换，不能让两套配置不同的执行器同时管理相同资源。

## 实施顺序

1. 将引擎作为本地 Python 包纳入仓库和 uv.lock，保留原测试。
2. 适配社区登录身份、应用权限、统一错误和返回格式。
3. 增加 SQLite 入队、本人查询 API 与独立 Worker 入口。
4. Vue 接真实任务、阶段动画、恢复和错误提示，补配置 Skill。
5. Linux 测试环境验证重复提交、两用户隔离、进程重启、构建失败、健康检查失败、发布回滚、旧应用及数据保留。

代码落点核对：原 api.py 的 create_app 有独立认证；store.py 的 enqueue 已接受 actor；worker.py 的 run_once 有跨进程文件锁、授权复核和持久恢复；access.py 的 authorize_operation 会调用 IdentityStore。
