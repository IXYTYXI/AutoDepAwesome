# 社区一键部署接入

## 当前状态

Vue 部署向导已接入 `/api/v1/deployment`，复用社区 Cookie/Bearer 身份、CSRF 校验和错误返回格式。部署引擎作为 uv 本地依赖保存在 `packages/deploy-engine`，保留来源版本 0.11.8；不挂载原项目的登录、管理网页或额外 HTTP 服务。

本机 macOS：提交默认关闭，不创建假任务，不显示虚假成功。Linux 上通过引擎检查后才启用执行。2026-11-02 已接管正式服务器的原部署台账和执行环境，详情见 SERVER_LIVE.md。

## 启动和配置

`uv sync` 后运行 `uv run python -m app`，同时启动前端、后端和独立部署 Worker。仅 uvicorn 启动时，另运行 `uv run python -m app.deployment.worker`。

- DEPLOY_ENABLED：部署模块开关，默认 true。
- DEPLOY_EXECUTION_ENABLED：真正执行构建、Docker、网关，默认 false。
- DEPLOY_SYNC_ENABLED：Redis/飞书同步，默认 true。
- DEPLOY_STATE_ROOT：无引擎配置时的数据库目录，默认 var/deployment。
- DEPLOY_ENGINE_CONFIG：Linux 引擎配置文件。配置存在时其 state_dir 是唯一数据库目录，API 和 Worker 的 state_dir 必须相同；系统用户不同时可使用两份配置引用各自属主的凭据。
- DEPLOY_DATABASE_GID：可选，宿主机 API/Worker 不同 UID 共用 SQLite 时指定预配置的专用组；数据库须为该组 0660、父目录 setgid 2770，两个用户均入组。默认不放宽原有 0600 权限。
- DEPLOY_TENANT_ID：明确允许的组织。不同组织不能访问部署。
- DEPLOY_ALLOWED_USERS：可选社区 user_id 白名单；空数组允许本组织有效飞书用户，仍受引擎本地账号/应用权限约束。
- DEPLOY_APPS_TABLE_ID / DEPLOY_JOBS_TABLE_ID：部署应用/部署记录目标表。

配置前缀均为 OWS_COMMUNITY_。本机 .env 已写入新表 ID 及从既有有效社区会话确认的租户，不使用 CLI 凭据作为运行时身份。引擎中的 is_admin 不从社区角色自动提升。停用和 can_deploy 变更保存在引擎 SQLite，Worker 在执行、发布前重新验证。

## Linux 上线条件

参照 `packages/deploy-engine/ops/README.md` 完成原引擎安装。`deploy/deployment/engine.example.json` 是需要填实际值的示例，不可直接作为已就绪配置。

1. 独立非 root 部署用户、rootless Docker、支持 cgroup v2 的资源限制，独立 BuildKit builder。不要连接社区/Dify 的 Docker daemon 作为简化替代。
2. 持久化 state_dir，保护权限；配置网关和存储的 root-owned helper。采用 XFS project quota 才能真正限制持久数据，不能把 local 存储当作已有 50 MiB 硬限制。
3. 预配置泛域名解析和公网 80/443；helper 的域名/端口范围与引擎完全一致。
4. 选择资格规则并提供应用密钥文件：continuous 需要员工状态和应用可用范围权限；app_visibility 仅调用应用可用范围接口（application:application:self_manage），不请求受雇信息。后者按用户明确选择启用，仍在提交和执行/发布前校验资格；离职冻结状态不单独查询，通过飞书应用范围及本地禁用管理权限。缺少所选规则必需权限时拒绝执行。
5. API/Worker 共享同一本机持久化数据库；不可多个服务器通过网络盘共同写 SQLite。
6. `uv run awesome-deploy --config /etc/ows-deployment/engine.json check` 验证后再开 EXECUTION_ENABLED。Worker 启动还会自行 preflight，只有就绪且心跳新鲜时才受理任务。

若沿用原服务的域名空间和线上应用，须先停止旧 Worker、迁移完整状态/密钥/存储及网关发布记录；本服务器的接管与备份路径已记录在 SERVER_LIVE.md。两个独立状态库不能同时控制相同域名。

## 资源隔离

单 Worker 串行执行构建。默认候选端口 22000–22999；禁止覆盖社区监听端口、80/443、Redis 6379。API 不执行长时间构建。

示例运行中应用总预算 2 CPU / 2048 MiB，每应用 500 MiB。引擎检查 builder 自身也有 CPU/内存边界；builder 与应用预算是两个资源集合，不能把 runtime 总额度理解为整机总额度。启动要求物理内存至少达到两倍应用总内存预算再加 2 GiB 的社区保留空间。实际还需为 Dify、Redis 等现有服务另外留足空间。

Worker systemd 示例只限制 Python 控制进程；Docker daemon、BuildKit 和应用容器必须另外受 cgroup/引擎预算限制。磁盘至少保留 4 GiB，镜像/源码预算独立配置。没有对现有容器执行 prune 或全局清理。

## 数据一致性

SQLite 存储应用/任务/归属/域名/资源占用/恢复状态，WAL、短事务；Docker 操作仍由引擎检查点与恢复机制处理，不能由 SQL 回滚。

`community_sync` 是事务 outbox：jobs/apps INSERT/UPDATE 的触发器在相同事务内增加 revision。一个实体对 Redis/飞书各自一条待处理项，合并连续变化。同步器有独占文件锁、每批最多 20 条、HTTP 并发上限 2、独立 Redis 连接池 4，不与资讯同步共用连接池。

成功只确认读取时的 revision；中途的新变更继续待处理。失败分别指数退避，最多间隔 600 秒，写日志并保留 attempts。可直接查询 pending/retry_at 判断积压。重启继续处理，无需重新部署。飞书只读展示，不反向控制状态；完整快照和密钥不写飞书。

飞书创建使用稳定应用/任务 ID 去查重并保存 record_id；未知创建结果先重新查询，发现重复时保留待同步项等待人工核对，不无限新增。部署记录同步访问地址，以及错误码、原因、字段和处理建议；不写入原始进程输出或密钥。

Redis 缓存部署详情 5 秒，查询必须重新鉴权并比较 SQLite updated_at，旧缓存不能覆盖新状态；Redis 不可用时查询回源 SQLite。业务身份会话仍依赖社区 Redis，Redis 全故障时不会绕过登录。应用列表/权限判断直接查 SQLite。

备份使用 SQLite 在线备份机制，同时备份对应运行状态、密钥与持久数据；不要仅复制活跃数据库主文件而漏 WAL。

## 接口

- GET /api/v1/deployment/options：执行开关、域名、仓库白名单、内存限制。
- POST /api/v1/deployment/jobs：repository/name/app_id（可选），必须传 Idempotency-Key；固定 main。相同请求重试复用 key。
- GET /api/v1/deployment/jobs：当前用户可见的最近任务；前端据此提供已有应用的更新入口。
- GET /api/v1/deployment/jobs/{id}：受权限控制的进度；已提交后刷新页面会找回未完成任务。

项目名称在提交 SQLite 事务中即绑定到应用，不能用不同请求编号创建同名应用。名称保留给原应用，失败后从“已有应用”重试。相同应用存在 queued/running/recovery_required 任务时拒绝再次排队；全局队列上限 100。相同用户、相同 Idempotency-Key 和相同请求返回原任务，修改参数则冲突，执行器停机也能查询已受理的原任务。

GET /api/v1/deployment/name-availability 提供输入提示，最终以提交事务为准。域名和 awesome.yaml 在 Worker 中再次检查，表单预览不表示已发布。前端查询请求会在切换任务或离开页面时取消，并忽略过时响应。

## 本次上线核对与生产启动

新增 `Dockerfile` 构建 Vue 产物和 Python/uv 依赖，`compose.server.yaml` 启动社区 API 与专用 Redis。API 使用非 root 用户、只读容器根目录、内存/CPU/PID 限制、日志轮转及 Redis 健康依赖。原 `compose.yaml` 仍是本地开发 Redis，不替换现有容器。

生产包仍需填写真实配置，不能把示例中的 example.com 或 REPLACE 字段当作上线值。社区容器不挂载 Docker socket；真实部署 Worker 在 Linux 宿主机独立用户下运行，复用同一份源码和 SQLite，通过原引擎限制的 helper 管理网关及存储。

1. 从 `deploy/server.env.example` 复制为项目根目录 `.env.server`，填写飞书应用、Base/表 ID、租户、正式网站域名、允许来源。飞书后台同步登记正式回调地址。已有可用配置可以复用对应字段；不把本机 OAuth 地址带到服务器。
2. 创建 `/var/lib/ows-community`、`/var/lib/ows-deployment`、`/etc/ows-deployment`。前两个目录归运行用户所有（默认 UID/GID 1001，也可设置 OWS_UID/OWS_GID）。这些是持久数据目录，删除容器不会删除它们。Redis 使用独立 named volume。
3. 完成上文 Linux 引擎安装，把真实配置放入 `/etc/ows-deployment/engine.json`；API 和 Worker 可分别使用凭据属主匹配的引擎配置。两端 app_id、tenant、state_dir 必须相同，选择 continuous（员工状态＋应用范围）或 app_visibility（本企业身份＋应用范围）；社区不接受仅 login 模式。未完成安装时保留 EXECUTION_ENABLED=false。
4. `docker compose -f compose.server.yaml config --quiet` 检查格式；`docker compose -f compose.server.yaml up -d --build` 构建并启动社区。默认仅绑定本机 8000 与 Redis 6381，不暴露到公网。TLS 网关代理到 8000，参照 `deploy/nginx.conf.example`，不要将项目目录作为公开静态目录。
5. 宿主机 `/opt/ows-community` 安装源码并执行 `uv sync --frozen --no-dev`。复制正式配置到 `/etc/ows-community/worker.env`；Redis 地址使用 `redis://127.0.0.1:6381/0`（API 容器内由 Compose 自动覆盖为 redis:6379）。用 `OWS_COMMUNITY_SETTINGS_FILE` 指定此配置。安装 `deploy/deployment/ows-deployment.service.example`，它在启动前执行 executor preflight。它应由专用 `ows-deploy` 用户运行，UID 与共享目录一致。
6. 验收：`docker compose -f compose.server.yaml exec api python -m scripts.check_server`；Worker 的 `scripts.check_server --executor` 必须通过；再用测试仓库实际验证构建、HTTPS 访问、失败回滚、重启恢复及飞书台账同步。没有完成真实 Linux 测试前，不标记为“自动部署已验收”。

Worker 在启动、退出和后台协程失败时清除就绪心跳；同步器意外退出会让进程失败，由 systemd 重启。正常停止等待当前执行动作结束，不接受新任务；强制退出仍依赖引擎检查点恢复。SQLite 事务不包裹构建过程，Redis/飞书同步失败不回滚部署主记录。

图片写入后清理同目录中超过 30 天的可再生成 WebP 缓存和超过 1 天的遗留临时文件，严格匹配程序生成的文件名，不跟随软链接。此策略解决长期累积，不是磁盘硬配额；运行目录仍需容量监控。部署镜像/容器清理保留引擎的归属校验，清理无法确认时保留资源占用并标记 recovery_required，避免误释放或误删其他服务。

本地验证通过不等于目标服务器验收。当前 Docker 构建曾因 Docker Hub 认证端点连接超时中断，尚无成功镜像构建记录；Linux rootless Docker、XFS 配额、TLS/泛域名和实际仓库构建仍需在目标服务器验证。
