# Awesome Deploy（阶段一）

把受控 Git 仓库部署到 `https://<subdomain>.awesome.guanghexinzhi.cn` 的单机原型。管理网页提交仓库、ref 和域名前缀，独立 worker 执行部署；网页/API 接收成功仅表示任务已入队。

当前交付包含配置校验、Git 提交固定、受控容器与持久卷部署、同域名路径路由、HTTPS 网关、SQLite 状态和中断恢复。生产使用前须在目标机完成实际部署、证书、故障恢复与续期验收，不能据本地测试认定生产可用。Linux 安装前请阅读 [运维指南](ops/README.md)。

`main` 是本仓库完整代码的统一维护入口，安装部署不再选择旧功能分支。历史位置通过 `archive/2026-10-20/*` 标签和[分支整理记录](docs/history/2026-10-20-branch-consolidation.json)保留。源码合并不表示目标环境已经发布，功能启用和实机验收仍按对应运维说明执行。

后续功能分支只用于临时开发，通过审查和测试后合入 `main` 再清理。部署时固定具体提交；正式发布版本使用版本标签，归档标签仅用于历史追溯。

## 飞书登录与应用权限（0.9.0，待真实回调验收）

`auth_mode: "feishu"` 启用飞书网页登录，核验租户、员工状态及应用可用范围，使用平台独立会话和 CSRF 保护；此模式不接受旧 Basic 凭证。普通成员的新应用记录明确负责人；owner 管理成员及下线，maintainer 可部署和补充缺少的配置，viewer 仅看安全概览。所有部署类写操作还要求用户具备平台部署权限。管理员只能在可信服务器终端通过 `user-set` 明确授予，旧应用须显式指定归属。

撤销账号、部署能力或应用成员权限会使旧排队授权失效；执行器在获取代码、冻结配置、停止旧版、启动和发布前复核。已经开始的下线清理按固定范围完成，已上线版本的系统恢复不依赖原提交者。新应用不能自动继承其他应用或尚未归属的历史秘密，也不能借用平台全局 Git 凭证。`lark-cli` 可用独立应用 profile 核验配置；服务不复用某台机器的 CLI 用户登录态。配置、迁移、撤权与验收步骤见 [飞书登录操作说明](ops/FEISHU-LOGIN.md)。网页还会阻止停用最后一位本地可用管理员。业务应用 SSO、Skill 配对登录及审批流仍是后续工作。

## 应用加密密钥库（B 阶段，显式启用，待目标机验收）

新增 `secret_backend: "encrypted"`，仅在飞书模式及显式初始化/迁移后启用。密钥以稳定应用身份、类型、数据代际和版本隔离，使用 AES-256-GCM；每应用 DEK 由独立凭证目录中的 KEK 包裹。默认 `legacy` 保持旧行为；加密状态不允许静默回退明文格式。

应用管理页增加密钥元数据与版本历史、带当前版本校验的普通轮换、紧急吊销及清理进度。网页不返回已保存值。平台托管主密钥持久保护，普通网页/CLI 无法轮换；冻结部署继续使用原版本。吊销会持久冻结影响范围，阻止相关版本启动、发布、回滚和系统恢复，核验路由与容器停用后才释放运行资源。尚未确认的清理需要显式重试。

初始化、全历史迁移、KEK 重包裹、可信 CLI、精确销毁和恢复步骤见 [加密密钥库运维说明](docs/vault-operations.md)。本批未修改业务仓库；未执行生产迁移、真实 OAuth 或 Linux/Nginx/Docker 验收。C 阶段 Skill 配对和 D 阶段审批仍未实现，当前部署不代表已审批。

## 服务存储硬配额（0.8.0，待目标机验收）

新增可选 XFS 后端，为每服务 `/data` 同时设置容量与 inode 硬限；提供唯一账本、显式扩容、未决操作重试、受控恢复、独立巡检与定向删除。默认仍为 `storage_backend: local`。新后端需管理员配置和验收，新建存储使用 schemaVersion 2 的 `sizeMiB`、`maxInodes`，已有 named volume 不会自动迁移。

代码和模拟测试完成不代表生产已启用。安装、兼容边界、故障恢复及隔离环境验收见 [存储硬配额运维说明](ops/STORAGE-QUOTA.md)。原硬配额准入要求继续保留，DEC-QUOTA-001 尚待目标机验收与确认。

## 在部署页面补齐应用配置（0.6.1）

平台拉取并固定代码后，若项目声明的 `secretRefs` 尚未配置，部署页面列出需要填写的配置和由平台管理的配置。用户设置 `BOOTSTRAP_PASSWORD` 登录密码后点击“保存并继续部署”，无须复制加密密钥或勾选保管确认。保存后按原任务仓库、ref、域名和应用选择继续部署；原失败记录保留，同一失败任务的继续请求在切换记录、刷新和重试后仍复用幂等键。

对引用名为 `MASTER_KEY` 且全部环境变量用途也为 `MASTER_KEY` 的已支持约定，平台在新应用首次保存时使用服务器的密码学安全随机数生成器创建 32 字节、64 位十六进制密钥，保存在服务器私密配置中，并通过既有环境变量注入给应用。浏览器只显示管理状态，不接收密钥值。该约定要求应用接受此格式；其他格式不会自动推断。加密模式按环境变量用途识别 `MASTER_KEY`，即使引用名不同也持久标记为受保护主密钥，不能借改名解除保护。外部模型 API Key 等仍填写供应商提供的真实值。

已有密钥持续复用，更新、重试不会重新生成。原应用的密钥缺失、不可读取或损坏时，需要恢复原配置，不能生成新值替换。平台生成不是密钥的技术要求；现有正确密钥可以由运维从仓库外安全导入。实际密码和密钥不能提交到 Git，仓库只声明引用名称。

页面只补充缺失配置，已配置值不返回浏览器、不覆盖。旧 legacy 模式使用已确认的仓库/域名前缀空间和文件权限保护；加密模式使用应用身份、密文与固定版本；不写入 Git、部署记录、执行日志、浏览器持久存储或 AI 分析内容。用户输入在保存成功或切换记录时清空。平台持久保存不等于异机备份，迁移时须保留原秘密配置。应用仓库及业务逻辑保持原样。

## Git 拉取代理（0.5.1）

管理员可通过 `git_proxy_url` 为仓库拉取指定代理，例如 `http://127.0.0.1:7890`。默认不配置；启用后仅 Git 的网络拉取使用该代理，保留 HTTPS 证书校验，代理失败时不会自动退回直连。配置不支持嵌入用户名或密码，具体设置见 [运维指南](ops/README.md)。业务代码、仓库 ref、容器网络、构建网络和 AI 接口均不受这个设置影响。代理可改善网络可达性，但不能解决不存在的分支或仓库权限问题。

## 运行时公网 HTTPS 直连（0.5.0）

管理员启用 `runtime_network: public-https-v1` 后，新部署的业务容器可直接连接公网 IPv4 的 TCP 443 端口，并使用指定公共 DNS。外部模型 API 可以使用原有 HTTPS 地址和 SDK；平台不注入代理，不修改业务源码、模型请求或认证逻辑。API Key 仍由项目通过 `secretRefs` 引用自己的秘密配置，平台诊断 AI 的密钥不会转给业务容器。

内部服务仍按 `dependsOn` 网络通信。私网、宿主机地址、云元数据、其他出口端口和 IPv6 不开放；TCP 443 规则不是 TLS 内容检查或域名白名单。Dockerfile 构建网络仍为 `none`。管理员须配置全部宿主机公网地址（包括云 NAT 地址）并完成目标机验收，详见运维指南。

策略随部署输入冻结：旧部署保留 internal 模式，升级平台本身不会把旧应用自动连到公网；重新提交部署才使用新配置。变更全局 DNS 或屏蔽地址必须先维护所有公网模式应用，不能直接修改配置后让旧快照恢复。网关新发布的应用路由禁用响应缓存，读取超时为相邻数据块间 300 秒，便于应用原有流式响应及时到达客户端；平台不会把非流式业务请求改成流式请求。

## 部署控制台与应用管理（0.7.0）

部署表单与当前进度独立排列；最近记录显示在固定高度的滚动窗口中，可按状态筛选。API 当前最多返回最近 50 条，滚动窗口仅覆盖已加载记录。应用选择支持搜索、方向键、Enter 确认和 Escape 关闭；选择已有应用会填入仓库与域名前缀。任务编号、版本和执行日志按需展开，AI 建议仍由用户主动触发。

顶部导航可在部署控制台 `/` 与应用管理 `/manage` 之间切换。应用管理读取全部应用和所有未结束部署，显示在线、排队、执行中及待处理数量；各列表固定高度并在窗口内滚动，每 3 秒自动刷新。部署队列不受最近 50 条历史记录的限制，可直接进入具体部署进度。

在线应用的“下线”按钮会确认目标版本，将下线任务持久化后交给 worker 执行。平台先关闭并验证 HTTP/HTTPS 公开入口，再停止和清理该版本运行资源，全部确认后才标记已下线并释放部署配额。下线默认保留业务数据和平台保存的密码、密钥；也可选择“删除数据”，同时清除该应用的受控持久卷和全部已保存秘密配置（含历史轮换版本）。删除需输入“删除 域名前缀”确认，无法撤销。两种方式均保留应用及部署记录，完成后释放域名和端口供后续部署使用；历史成功版本不再显示可访问链接。释放资源前还会通过受控 Nginx 探针日志和进程身份，确认所有存活 worker 已使用停用路由、旧连接已排空；超时保留待处理状态和资源占用，不能把新连接收到 410 作为端口可复用的充分条件。已有部署、恢复或资源回收未完成时，暂不允许下线；下线未完成时阻止该应用的新部署和自动恢复，页面提供同一操作的显式重试。

重新上线需在部署控制台选择原应用、填写空闲域名前缀并提交部署。选择保留数据时，平台继承最后下线版本的持久存储配置，严格复用原卷身份；明确删除完成后，后续部署创建空数据卷，用户重新设置登录密码、填写外部 API Key，平台重新生成托管 MASTER_KEY。所选代码版本的 awesome.yaml 必须匹配填写的前缀，平台不会替用户修改源码。卷缺失或身份不符会失败，不会创建空卷代替。此功能不修改应用仓库的业务代码。

支持范围读取服务器能力。当前支持 Web 服务和每服务一个固定 `/data` 持久卷；有存储的更新会短暂停机。数据库服务/迁移、后台任务和自动备份恢复仍未实现；可选硬配额后端须单独配置和实机验收。审批、代码审查和员工角色等后置功能不包含在当前交付中。

## 执行流程

1. API 校验请求与幂等键，将任务持久化到 SQLite。
2. worker 取得覆盖整个执行过程的文件锁，读取应用归属和上个成功版本的默认配置。
3. 拉取 ref，先保存完整 commit，再导出该提交；重启只使用已保存的 commit。
4. 读取仓库根目录 `awesome.yaml`，校验并冻结镜像 digest、构建上下文和秘密配置版本。
5. 事务内预留域名、每个服务的本机端口和资源。旧版本、候选版本及尚未确认清理的版本同时占用配额。
6. 构建并保存镜像 ID，受配额保护的存储在停止旧版前完成准备。使用持久存储的更新先写入交接意图并停止旧版，再按依赖关系启动候选；无存储部署维持并行候选行为。随后检查健康。
7. 准备 HTTPS，保存发布意图，经受限网关 helper 更新路由，并验证真实 HTTPS 请求到达候选端口。
8. 原子提交成功状态和当前版本指针，再回收旧版本。清理失败保留配额，持久化待办至少间隔 60 秒重试。

无存储部署在发布前中断可以继续已有快照；发布意图写入后中断则先回滚路由再清理候选版本。有存储部署从交接意图写入起，中断均走保守恢复：确认候选停止并清理后，恢复旧容器，再按需回滚路由。回滚或清理无法确认时标记 `recovery_required`，保留资源并阻止该应用继续部署，等待运维介入。失败清理意图也会持久化，已释放资源的候选版本不会被重新启动。阶段一不提供自动审批或代码审查，飞书多维表格建表和同步、审批、数据库迁移等工作留待后续阶段。

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
| 构建 | `context` 相对仓库；`dockerfile` 相对 context；可指定 `target`、非秘密 `args`；禁止大小写不敏感的 `BUILDKIT_*` / `BUILDX_*` 保留参数；最终使用 `--network=none`，保留正常构建缓存 |
| Dockerfile | 支持显式 FROM、RUN、COPY、ENV、WORKDIR、USER、EXPOSE、LABEL、CMD、ENTRYPOINT、ARG、STOPSIGNAL；基础镜像先固定 digest |
| Dockerfile 限制 | 不支持续行、heredoc、自定义 frontend/parser、动态 FROM、RUN flags/mounts、ADD、VOLUME、ONBUILD；COPY flags 仅允许引用之前命名阶段的 `--from=` |
| 依赖 | `dependsOn: {api: healthy}` 或 `started`；拒绝缺失依赖和循环依赖 |
| 健康检查 | HTTP 路径或容器内 exec 数组；支持 intervalSeconds、timeoutSeconds、retries、startPeriodSeconds |
| 路由 | `exact`、`prefix` 与 `stripPrefix`；网关实际最多 32 条；支持规范字面路径，含点号（如 `/.well-known/security.txt`）；仅保留 `/.well-known/acme-challenge` 及其子路径；Schema 校验通过后仍须通过网关校验 |
| 域名 | 前缀 1–7 位小写字母、数字、中划线，首尾不能为中划线；不能使用保留名称或包含禁词子串 |
| 配额 | 默认总计 6 CPU、6144 MiB，每应用全部服务合计最多 500 MiB；省略的资源字段继承同名服务旧配置，新服务默认 0.5 CPU、500 MiB。继承后超限须在新提交中显式调整，历史快照不变 |
| 网络与文件系统 | 默认 internal bridge；可由管理员启用受限公网 TCP 443 直连；RootlessKit builtin 转发仅绑定 `127.0.0.1`；非 root、只读根目录；受限临时空间及显式声明的 `/data` 持久卷 |
| 持久存储 | 每服务最多一个平台派生名称的本地卷，挂载点固定 `/data`；更新复用，失败与退役清理均不删除卷 |
| 暂不支持 | 主机挂载、共享卷、任意挂载点、卷 driver/options、数据库迁移、生命周期 hook、非 web worker、定制出站规则、构建秘密或 SSH agent |

禁词采用保守的子串匹配，API 提交和 manifest 校验使用相同规则。例如 `mishit` 包含 `shit`，即使长度及字符合法也会被拒绝；目前没有正常词例外名单。

`--network=none` 意味着 Dockerfile 的 RUN 中不能访问 npm/pip/apt 等外网；请使用已经包含依赖的基础镜像或仓库内已有构建产物。镜像的 `VOLUME` 声明或 ONBUILD 仍会被拒绝；持久卷必须通过 `service.storage` 显式申请。internal 依赖网络是双向网络，不是单向应用防火墙；容器共享主机内核，不能当作运行任意敌对代码的虚拟机隔离边界。

源码构建使用正常缓存。平台将完成基础镜像固定后的构建 context 保存为内容寻址目录：最终目录名包含协议版本，以及文件内容、相对路径、类型、权限、所有者和扩展属性的完整 SHA-256 摘要，不包含修改时间、任务 ID 或绝对路径。相同输入跨任务复用本地源同步状态；字节或元数据变化使用新的同步状态，构建前重新校验目录完整性。Dockerfile、`.dockerignore` 和多阶段构建语义保持不变；摘要包含忽略文件，因此只改忽略文件也可能重新同步 context，但 Docker 仍按实际构建输入判断步骤缓存。

此目录名方案隔离的是 BuildKit 的本地源同步状态，不能消除旧的错误构建结果。首次从 0.6.3 恢复缓存时，管理员必须切换到具有全新 state volume 的独立受控 builder，禁止复用或导入旧 builder 缓存，并验证冷构建与重复缓存构建的文件权限和应用健康状态；旧 builder 停用保留供调查。不能用一次 `--no-cache` 或仅重命名 builder 代替此迁移。已有任务保存的镜像 ID 继续用于恢复，不重新构建；尚未构建的旧冻结 context 仅复制到同任务目录下的新名称，保留原文件、权限和路径。平台不新增 `--pull`、不修改客户 Dockerfile，也不清理业务镜像或卷。

### 传统持久存储（schemaVersion 1、local 后端）

在需要保存文件的服务中声明：

```yaml
storage:
  name: images
  mountPath: /data
```

`storage` 属于单个 `services.<name>`。平台以应用 ID、服务名和 storage.name 派生 Docker 卷名称，核对卷的归属 labels、local driver、空 options、随机实例标识和创建时间；不同应用隔离，同一应用不同服务不得共享 storage.name。成功版本中的存储不能在后续 manifest 中删除、改名、移动或通过服务改名绕过。下线默认保留数据；明确选择删除数据后才执行受控删除。失败的首次部署也可能保留一个卷，由运维确认归属后处理。

镜像须预建 `/data` 并赋予平台 `runtime_user`（默认 `65532:65532`）写权限，Docker 首次挂载空卷时初始化该目录。不要声明 `VOLUME`；平台不以 root 运行应用，也不对已有数据递归 chown。镜像更新只能使用原卷；已有卷丢失、实例标识、创建时间或所有权变化时失败关闭，不能静默创建空卷代替。既有容器恢复也必须具有持久化的卷及容器身份记录。

有存储的更新使用 Recreate，有短暂停机：镜像构建成功后先持久化交接意图，确认旧容器停止，才启动候选。失败或中断先确认候选停止和清理，再恢复旧版及路由；无法确认时转 `recovery_required`，旧版维护扫描也会被阻断，避免双写。首次部署没有旧版需要暂停。没有 storage 的既有应用保持原部署策略。

自动回退恢复的是应用版本和路由，**不会撤销候选对卷的写入，也不自动回滚存储格式或数据库 schema**。更新必须保持数据向后兼容；破坏性迁移需要单独维护方案。卷持久化不能防范误删、磁盘故障或主机丢失，运维仍须做离机备份与恢复演练，并监控磁盘剩余空间、inode 与图床自身容量上限。

导出当前 Schema：

```sh
.venv/bin/awesome-deploy schema --output awesome-v1.json
```

## 私有仓库与秘密配置

私有 Git 仓库由运维在 `config.json` 设置绝对路径 `git_askpass`。helper 必须是运维管理的可执行文件，按 Git 提示返回用户名或仓库只读令牌；秘密值来自仓库外的受限文件或秘密管理服务。Git 子进程使用最小环境，普通全局 credential helper、用户 Git 配置和任意环境变量不会继承。不要把令牌放入仓库 URL、manifest、提交 JSON、命令参数或日志。当前没有网页端 Git 凭证管理流程。

缺失的应用秘密可在部署页面直接填写。加密模式的普通运行密钥可在应用管理页轮换和紧急吊销；可信 CLI 使用 `secret-set --app-id`，轮换另需 `--expected-current-version` 和原因，详见 [加密密钥库运维说明](docs/vault-operations.md)。已有主密钥不能普通轮换，配置恢复使用受控运维流程。

以下 CLI 和文件存储说明仅适用于默认 `legacy` 模式，其已有值轮换仍使用运维入口。运维账号可交互输入：

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

引用名实际支持 1–64 位字母数字、下划线或中划线，首位须为字母。同一环境变量不能同时出现在 `env` 与 `secretRefs`。秘密按规范化仓库 URL 和 subdomain 定位，保存在状态目录的权限受限文件中；部署冻结版本，后续轮换不改变已经冻结的任务。文件权限保护不等于静态加密，管理员仍需保护状态目录和备份。构建参数不提供秘密传递能力。域名被另一应用复用时，若新部署引用秘密配置，平台会拒绝沿用其他应用已使用过的仓库与域名配置空间，包含尚未被部署使用的轮换版本。需要沿用秘密配置时选择原应用；创建新应用时需使用该仓库未被其他应用使用过的空闲前缀，并通过部署页面或 secret-set 单独配置。没有引用秘密配置的新应用仍可复用已释放域名。存在多应用历史时，原应用可重用它在同仓库、同前缀下成功部署并实际引用过的相同秘密名称和版本；失败、冻结记录或其他应用的记录不能作为授权依据。尚未成功使用过的新版本仍按配置空间隔离规则处理。默认保留数据的下线不会删除历史秘密文件；明确选择删除数据时会清除该应用独占的历史配置空间。若历史配置空间已被其他未清理应用使用，平台会拒绝自动清除，避免误删其他应用配置。清理期间阻止新部署与秘密写入；中断只允许同一请求继续清理，不会扩大清理范围或自动恢复可能已部分清空的旧版本。已清除部署记录仅作历史审计，旧补配表单不能恢复其配置。

## API

兼容模式 `auth_mode: "basic"` 使用 HTTP Basic 运维认证。多人使用应配置 `auth_mode: "feishu"`，登录页及静态资源公开，业务页面/API 需要独立会话和应用权限；所有写请求必须同时具备精确匹配的 Origin 与 `X-CSRF-Token`。远程请求必须通过 HTTPS，写请求使用 `application/json`。单次 HTTP 请求最大 128 KiB；配置预检只校验 manifest，不拉仓库、不构建、不预留容量。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/deployments` | 提交任务，HTTP 202 |
| `GET /api/v1/deployments` | 最近部署任务 |
| `GET /api/v1/deployments/{id}` | 状态、阶段、commit、结果 URL 和安全错误 |
| `GET /api/v1/deployments/{id}/events` | 有界事件信息，不返回原始命令输出 |
| `GET /api/v1/deployments/{id}/secrets` | 查询固定任务声明的配置名称和状态，不返回值 |
| `POST /api/v1/deployments/{id}/secrets` | 仅补充声明中的缺失值；请求体 `{"values":{"REFERENCE":"value"}}`，不覆盖、不启动部署 |
| `POST /api/v1/deployments/{id}/analyze` | 请求体 `{}`，按需生成 AI 修改建议；仅失败或需恢复的任务 |
| `GET /api/v1/apps` | 应用和当前版本，应用主键字段为 `id` |
| `GET /api/v1/apps/{app_id}/secrets` | 加密模式：密钥名称、类型、版本和吊销清理状态，不返回值 |
| `POST /api/v1/apps/{app_id}/secrets/{name}/rotate` | 加密模式：普通密钥轮换；请求体含 `value`、`expected_current_version`，要求近期认证 |
| `POST /api/v1/apps/{app_id}/secrets/{name}/revoke` | 加密模式：紧急吊销及持久清理；请求体含 `version`、`expected_current_version`、`reason`，要求近期认证 |
| `GET /api/v1/management` | 全部应用、所有未结束部署和待处理下线操作的安全投影 |
| `POST /api/v1/apps/{id}/offline` | 请求体 `{"expectedReleaseId":"当前部署编号","dataPolicy":"retain"}`；删除使用 `"dataPolicy":"delete","deleteConfirmation":"删除 demo"`，需要幂等键，HTTP 202 |
| `GET /api/v1/offline-operations/{id}` | 下线状态、阶段和中文错误；不返回运行快照或秘密 |
| `POST /api/v1/preflight` | 请求体 `{"manifest":"YAML 字符串"}` |
| `GET /api/v1/schema` | 当前 JSON Schema |
| `GET /api/v1/capabilities` | 当前能力开关 |
| `GET /api/v1/openapi.json` | 认证后的 OpenAPI 定义 |

`Idempotency-Key` 必填，1–128 位字母数字或 `._:-`。同键同请求返回同一个任务，适用于响应丢失后的重试；同键不同请求冲突。真实新部署必须换键。当前已绑定域名的应用更新必须提供原 `appId`，且 repository、subdomain 与原应用相同；不能靠新建任务抢占已使用域名。下线完成后，应用的 subdomain 为 null、domainBound 为 false，lastSubdomain 仅表示历史前缀。原应用可以使用原 appId 和原仓库重新认领空闲前缀，数据仍属于原应用；新建应用使用独立身份和数据卷。

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

部署状态为 `queued`、`running`、`succeeded`、`failed` 或 `recovery_required`，`phase` 单独表示执行阶段。仅 `succeeded` 表示发布并验证完成；是否仍在线以应用当前版本和管理状态为准。清理仅覆盖平台管理的该应用持久卷与秘密配置；不会删除外部数据库、第三方 API 账号、手工备份、部署历史、仓库代码或构建缓存。该服务没有多租户权限系统、审批流或删除应用记录 API；部署的 `recovery_required` 需要运维诊断，不能直接修改数据库标志来跳过回滚和资源确认。下线操作的 `recovery_required` 可使用原数据策略、原请求和同一幂等键显式重试，继续核验原目标；版本变化返回冲突，不会下线更新后的版本。

## 中文诊断与可选 AI 建议

失败页面展示中文原因、问题位置和逐步修改建议；历史英文错误和事件也在读取时转换，数据库中的原始记录保持不变。`error.code` 保持机器可读，新增 `title`、`steps`；字段位置中的动态服务名和变量名会归一为 `*`。新任务的 `INVALID_MANIFEST` 使用可选 `reason` 区分 `MANIFEST_MISSING`、`MANIFEST_TOO_LARGE` 和 `MANIFEST_ENCODING`。无法细分的旧记录仍明确显示“缺失或超限”。

AI 默认关闭。运维可在配置 JSON 中同时设置以下三项（不要将实际密钥写入 JSON、仓库或命令参数）：

```json
{
  "ai_base_url": "https://provider.example/v1",
  "ai_model": "provider-model-id",
  "ai_api_key_file": "/etc/awesome-deploy/ai-api-key"
}
```

`ai_base_url` 是 Chat Completions API 的父路径，平台向其末尾追加 `/chat/completions`；若服务提供 `/v1/chat/completions`，应配置含 `/v1` 的 Base URL。`ai_model` 必须使用服务实际接受的模型 ID，不要把界面展示名称或推理强度直接拼进去。可选 `ai_reasoning_effort` 默认 `null`；仅当供应商支持时设置为其支持的强度（如 `ultra`）。启用强度时发送 `reasoning_effort` 和 `max_completion_tokens: 8192`，否则发送 `max_tokens: 1200`。

密钥文件须为与服务进程同 UID 的普通文件，权限为 `0400` 或 `0600`，存放在可信且他人不可写的目录中；文件自身不能为符号链接。它只包含一行密钥，最大 8192 字节。通过运维安全输入流程创建，应用配置不会读取环境中的 API Key；重启 API 服务后生效。文件权限不等于加密，root 和同账号进程仍可读取。

用户点击“AI 分析修改建议”才调用。发送内容仅为标准化中文诊断、失败阶段、归一字段位置和静态平台规则；不会发送仓库地址、源码、完整 manifest、环境变量、秘密或原始日志。AI 无执行工具、不会修改仓库或触发部署，建议必须结合实际项目核对。页面以纯文本显示输出。

接口使用 HTTPS，不跟随重定向，不继承代理环境变量；连接时限 10 秒、总调用时限 240 秒。请求启用标准流式响应，服务端汇总完成后返回建议，以适配长推理请求；也兼容忽略流式参数而直接返回 JSON 的接口。JSON 响应最多 32 KiB；SSE 单行和单事件最多 32 KiB、总数据最多 2 MiB；建议文本最多 12000 UTF-8 字节。相同摘要的结果在单进程内缓存 5 分钟（最多 32 项），并发相同请求合并，其他调用至少间隔 20 秒。供应商失败结果缓存 20 秒，避免轮询触发重复调用。单次 HTTP 等待最多 8 秒，未完成返回 `202 {"status":"pending"}` 和 `Retry-After: 1`；页面自动用 `{"poll":true}` 轮询（只查询已有任务与缓存，结果过期后必须手动重试，不会自动触发新的供应商请求），期间保持内置建议可见。接口未配置、密钥不可读、超时、限流或返回异常时，内置建议仍可使用；不会改变部署状态。

## 验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
# 可选的前端交互回归，使用 Node 内置测试器，无需安装 npm 依赖。
node --test tests/ui_*.test.cjs
```

测试使用临时目录、模拟 Docker/网关和本机进程验证协议、错误处理、资源归属与崩溃恢复；端口冲突测试需要允许本机 loopback bind。测试不会通过 SSH 部署，也不代表真实 Docker/Nginx/Certbot 链路已验收。目标机的单服务、多服务、故障回滚、续期与资源上限验证步骤见 [运维指南](ops/README.md)。
