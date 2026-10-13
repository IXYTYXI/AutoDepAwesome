# Awesome Deploy（阶段一）

把受控 Git 仓库部署到 `https://<subdomain>.awesome.guanghexinzhi.cn` 的单机原型。管理网页提交仓库、ref 和域名前缀，独立 worker 执行部署；网页/API 接收成功仅表示任务已入队。

当前交付包含配置校验、Git 提交固定、无状态容器部署、同域名路径路由、HTTPS 网关、SQLite 状态和中断恢复。生产使用前须在目标机完成实际部署、证书、故障恢复与续期验收，不能据本地测试认定生产可用。Linux 安装前请阅读 [运维指南](ops/README.md)。

## 执行流程

1. API 校验请求与幂等键，将任务持久化到 SQLite。
2. worker 取得覆盖整个执行过程的文件锁，读取应用归属和上个成功版本的默认配置。
3. 拉取 ref，先保存完整 commit，再导出该提交；重启只使用已保存的 commit。
4. 读取仓库根目录 `awesome.yaml`，校验并冻结镜像 digest、构建上下文和秘密配置版本。
5. 事务内预留域名、每个服务的本机端口和资源。旧版本、候选版本及尚未确认清理的版本同时占用配额。
6. 构建并保存镜像 ID，按依赖关系启动容器，检查候选版本健康。
7. 准备 HTTPS，保存发布意图，经受限网关 helper 更新路由，并验证真实 HTTPS 请求到达候选端口。
8. 原子提交成功状态和当前版本指针，再回收旧版本。清理失败保留配额，持久化待办至少间隔 60 秒重试。

发布前中断可以继续已有快照；发布意图写入后中断则先回滚路由再清理候选版本。回滚或清理无法确认时标记 `recovery_required`，保留资源并阻止该应用继续部署，等待运维介入。失败清理意图也会持久化，已释放资源的候选版本不会被重新启动。阶段一不提供自动审批或代码审查，飞书多维表格建表和同步、审批、数据库迁移等工作留待后续阶段。

已上线容器由 worker 统一恢复，保持 `--restart=no`。主机重启或容器停止后，worker 从 SQLite 的当前成功版本中找到既有容器，核对归属、冻结镜像及端口，按服务依赖顺序启动；不会重新拉取分支、构建镜像或创建容器。候选、失败、已退役版本不会被这条恢复路径启动。0.1.2 的恢复还要求任务具有持久化的 RootlessKit 转发意图及容器、网络身份；缺少这些记录的旧版本不会被自动迁移，应通过受控的新发布补齐。

发布验证在全部路由共用的 30 秒时限内重新建立 TLS 连接、发起新探针，并核对可信 Nginx 日志中的 token、域名、路径、上游端口和状态码。短暂连接失败或命中旧上游会重试，截止仍不匹配才回退；回退也要确认旧路由实际生效，再允许清理候选。该行为针对 Nginx 异步重载窗口，实际主机仍需按运维指南验收。

## 本地开发

要求 Python 3.11+、Git。以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/awesome-deploy init
.venv/bin/awesome-deploy serve
```

`requirements-dev.lock` 记录当前已验证开发环境的精确依赖版本；更换 Python、操作系统或依赖版本后需重新验证。

默认管理入口为 `http://127.0.0.1:8088`，用户 `operator`，密码在 `init` 时交互设置。用户名须为 1–128 位 ASCII 字符且不能含空格或冒号，密码须为 12–1024 位可打印 ASCII 字符。`init` 不覆盖已有凭证。远程管理必须按运维指南配置 HTTPS 反向代理；服务始终只监听 `127.0.0.1`。

本地网页/API 和配置校验不要求 Docker，相关测试已在 macOS 运行通过；实际部署后端及运维模板要求 Linux，原生 Windows 尚未验证。运行实际部署 worker 前必须在目标 Linux 主机执行：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json check
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json worker
```

`worker --once` 检查到期的已上线版本恢复，并处理排队/恢复任务或到期回收任务，输出 `processed`。`check` 检查主机、Docker、存储、builder 与网关前提，不等于一次完整部署验收。

## 示例仓库

`examples/simple-web` 是单服务网页/API；`examples/frontend-api` 是前端和 API 两个容器，分别匹配 `/` 与 `/abc`，后者使用 `stripPrefix: true`。服务基于 Python 标准库，无须在构建期间联网安装依赖。

**把某个示例目录的内容复制到一个新 Git 仓库的根目录**，再提交并推送。不要把 `examples/simple-web` 作为仓库 URL 或以本项目根目录代替示例仓库；执行器只查找仓库根目录的 `awesome.yaml`。例如已在新建的空目录中时：

```sh
cp -R /path/to/AutoDepAwesoem/examples/simple-web/. .
git init
# 修改 awesome.yaml 中的 app.subdomain，并设置自己的远程仓库后提交推送。
```

提交时填写的 subdomain 必须与 manifest 完全相同。默认允许 GitHub/GitLab HTTPS 仓库；URL 不能包含用户名、密码、端口、查询参数或 fragment。ref 支持字面分支、标签、完整 commit，不支持任意 Git revision 表达式。仓库符号链接、submodule 和 Git LFS 下载均不受支持。

## awesome.yaml

```yaml
schemaVersion: 1
app:
  name: 示例应用
  description: 无状态网页服务
  subdomain: demo
services:
  web:
    kind: web
    build:
      context: .
      dockerfile: Dockerfile
    port: 8080
    env:
      PORT: "8080"
    resources:
      cpu: 0.5
      memoryMiB: 128
    healthcheck:
      type: http
      path: /healthz
routes:
  - path: /
    match: prefix
    service: web
    stripPrefix: false
```

支持项和边界以 [Schema](src/awesome_deploy/schema/awesome-v1.json) 与执行器共同为准：

| 项目 | 阶段一行为 |
| --- | --- |
| 服务 | 1–16 个 `kind: web`；每项须有容器端口、健康检查，并在 `image` 与 `build` 中二选一 |
| 配置 | 字符串 `env`、`secretRefs`、数组 `command`/`entrypoint`、绝对 `workingDir`；不做变量插值；运行时环境值须为单行 |
| 构建 | `context` 相对仓库；`dockerfile` 相对 context；可指定 `target`、非秘密 `args`；禁止大小写不敏感的 `BUILDKIT_*` / `BUILDX_*` 保留参数；最终使用 `--network=none` |
| Dockerfile | 支持显式 FROM、RUN、COPY、ENV、WORKDIR、USER、EXPOSE、LABEL、CMD、ENTRYPOINT、ARG、STOPSIGNAL；基础镜像先固定 digest |
| Dockerfile 限制 | 不支持续行、heredoc、自定义 frontend/parser、动态 FROM、RUN flags/mounts、ADD、VOLUME、ONBUILD；COPY flags 仅允许引用之前命名阶段的 `--from=` |
| 依赖 | `dependsOn: {api: healthy}` 或 `started`；拒绝缺失依赖和循环依赖 |
| 健康检查 | HTTP 路径或容器内 exec 数组；支持 intervalSeconds、timeoutSeconds、retries、startPeriodSeconds |
| 路由 | `exact`、`prefix` 与 `stripPrefix`；网关实际最多 32 条；支持规范字面路径，含点号（如 `/.well-known/security.txt`）；仅保留 `/.well-known/acme-challenge` 及其子路径；Schema 校验通过后仍须通过网关校验 |
| 域名 | 前缀 1–7 位小写字母、数字、中划线，首尾不能为中划线；不能使用保留名称或包含禁词子串 |
| 配额 | 默认总计 6 CPU、6144 MiB；更新时省略的资源字段继承同名服务旧配置，否则默认 0.5 CPU、512 MiB |
| 网络与文件系统 | 服务运行在 internal bridge 网络；RootlessKit builtin 转发仅绑定 `127.0.0.1`；非 root、只读根目录，只有受限临时空间 |
| 暂不支持 | 持久卷、主机挂载、数据库迁移、生命周期 hook、非 web worker、定制出站规则、构建秘密或 SSH agent |

禁词采用保守的子串匹配，API 提交和 manifest 校验使用相同规则。例如 `mishit` 包含 `shit`，即使长度及字符合法也会被拒绝；目前没有正常词例外名单。

`--network=none` 意味着 Dockerfile 的 RUN 中不能访问 npm/pip/apt 等外网；请使用已经包含依赖的基础镜像或仓库内已有构建产物。镜像声明持久卷或 ONBUILD 也会被拒绝。internal 依赖网络是双向网络，不是单向应用防火墙；容器共享主机内核，不能当作运行任意敌对代码的虚拟机隔离边界。

导出当前 Schema：

```sh
.venv/bin/awesome-deploy schema --output awesome-v1.json
```

## 私有仓库与秘密配置

私有 Git 仓库由运维在 `config.json` 设置绝对路径 `git_askpass`。helper 必须是运维管理的可执行文件，按 Git 提示返回用户名或仓库只读令牌；秘密值来自仓库外的受限文件或秘密管理服务。Git 子进程使用最小环境，普通全局 credential helper、用户 Git 配置和任意环境变量不会继承。不要把令牌放入仓库 URL、manifest、提交 JSON、命令参数或日志。当前没有网页端 Git 凭证管理流程。

应用秘密由运维账号交互输入：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json secret-set \
  --repository https://github.com/your-org/your-repo \
  --subdomain demo --name API_TOKEN
```

随后在对应服务配置引用名：

```yaml
secretRefs:
  API_TOKEN: API_TOKEN
```

引用名实际支持 1–64 位字母数字、下划线或中划线，首位须为字母。同一环境变量不能同时出现在 `env` 与 `secretRefs`。秘密按规范化仓库 URL 和 subdomain 隔离，保存在状态目录的权限受限文件中；部署冻结版本，后续轮换不改变已经冻结的任务。文件权限保护不等于静态加密，管理员仍需保护状态目录和备份。构建参数不提供秘密传递能力。

## API

所有网页、静态资源和 API 都需要 HTTP Basic 运维认证。远程请求必须通过 HTTPS。写请求使用 `application/json`；浏览器 Origin 必须与 `public_url` 完全一致，跨站写入会被拒绝。单次 HTTP 请求最大 128 KiB；配置预检只校验 manifest，不拉仓库、不构建、不预留容量。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/deployments` | 提交任务，HTTP 202 |
| `GET /api/v1/deployments` | 最近部署任务 |
| `GET /api/v1/deployments/{id}` | 状态、阶段、commit、结果 URL 和安全错误 |
| `GET /api/v1/deployments/{id}/events` | 有界事件信息，不返回原始命令输出 |
| `GET /api/v1/apps` | 应用和当前版本，应用主键字段为 `id` |
| `POST /api/v1/preflight` | 请求体 `{"manifest":"YAML 字符串"}` |
| `GET /api/v1/schema` | 当前 JSON Schema |
| `GET /api/v1/capabilities` | 当前能力开关 |
| `GET /api/v1/openapi.json` | 认证后的 OpenAPI 定义 |

`Idempotency-Key` 必填，1–128 位字母数字或 `._:-`。同键同请求返回同一个任务，适用于响应丢失后的重试；同键不同请求冲突。真实新部署必须换键。已有域名重新部署必须提供该应用的 `appId`，且 repository、subdomain 与原应用相同；不能靠新建任务抢占已使用域名。

先保存请求文件，避免在命令中包含秘密：

```json
{
  "repository": "https://github.com/your-org/your-repo",
  "ref": "main",
  "subdomain": "demo"
}
```

```sh
# --user 仅给用户名，curl 会交互询问密码；替换入口域名和请求文件路径。
curl --user operator --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-initial-001' \
  --data-binary @deployment.json \
  https://awesome.guanghexinzhi.cn/api/v1/deployments

curl --user operator --fail-with-body \
  https://awesome.guanghexinzhi.cn/api/v1/deployments/JOB_ID
```

状态为 `queued`、`running`、`succeeded`、`failed` 或 `recovery_required`，`phase` 单独表示执行阶段。仅 `succeeded` 表示发布并验证完成。该服务没有多租户权限系统、审批流、删除应用 API 或恢复操作 API；`recovery_required` 需要运维诊断，不能直接修改数据库标志来跳过回滚和资源确认。

## 验证

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

测试使用临时目录、模拟 Docker/网关和本机进程验证协议、错误处理、资源归属与崩溃恢复；端口冲突测试需要允许本机 loopback bind。测试不会通过 SSH 部署，也不代表真实 Docker/Nginx/Certbot 链路已验收。目标机的单服务、多服务、故障回滚、续期与资源上限验证步骤见 [运维指南](ops/README.md)。
