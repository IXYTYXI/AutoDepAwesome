# 仓库部署准备、控制台与普通 API

适用平台基线 0.11.8，修订于 2026-10-30。保留原业务，准备可部署仓库。本文件随 Skill 分发，仓库检查无需先登录飞书或取得接入服务凭证。

## 从真实项目开始

确认原启动命令、监听地址/端口、健康路径、依赖、文件写入位置和外部服务。静态 HTML/JS 项目可以使用与原内容一致的静态 HTTP 服务，不需要为了提交部署而增加游戏登录、OAuth、数据库或部署 API 后端。

根目录提交 UTF-8 `awesome.yaml`，最大 256 KiB；源码构建还需 Dockerfile、完整 COPY 输入和必要的离线依赖。使用允许的 HTTPS Git 仓库 URL 及真实分支/提交，默认 ref 为 main；部署文件须已推送。不需要上传 ZIP。平台不支持 Git submodule、符号链接或自动下载 Git LFS 内容。

新建需确定项目名称；更新需明确原 appId 和仓库，已绑定域名时须保留该域名；已下线且解绑时，保留原 appId，选择空闲且与清单一致的名称重新部署。项目名称对应 subdomain，为 1–7 位小写字母、数字或中划线，首尾为字母或数字；可用性由平台受理时确认，不能仅因搜索为空就新建应用。

## 清单版本与最小模板

**新生成清单统一为 schemaVersion: 2，无状态项目同样适用。** 平台仍兼容已有 v1；使用 v1 不代表必然解析失败。新交付按 v2 生成，迁移已有项目先核对存储与实际解析结果，不能盲改存储身份。

下面只描述一个已有 HTTP 服务：它实际监听 0.0.0.0:8080，/healthz 是有效健康路径。按原项目替换；PORT 只有程序读取时才生效。无需持久数据或秘密时，不添加 storage/secretRefs。

```yaml
schemaVersion: 2
app:
  name: 示例应用
  description: 保留现有业务实现的单个 HTTP 服务
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

只允许 schema 声明的字段；YAML 禁止重复键、锚点、别名和插值。image/build 二选一；env 值为字符串，秘密仅通过 secretRefs 引用。command/entrypoint 为参数数组，保留实际入口语义。只支持 1–16 个 kind: web 服务、1–32 条路由；dependsOn 与路径转发须匹配实际服务，不把数据库或 worker 改名为 web 绕过限制。

## 构建、运行与数据

- 构建 RUN 默认无网络，不能依靠 npm/pip/apt 在线安装；使用原有离线依赖、产物或合适基础镜像。无第三方依赖的项目不必为形式完整增加锁文件。基础镜像由平台获取并固定摘要，字面标签不因未手写 digest 就不合格。
- Dockerfile 支持 FROM/RUN/COPY/ENV/WORKDIR/USER/EXPOSE/LABEL/CMD/ENTRYPOINT/ARG/STOPSIGNAL；不支持 ADD、VOLUME、ONBUILD、自定义 frontend、续行、heredoc、动态 FROM 和 RUN flags/mount。COPY 只能引用仓库内容或前序阶段。
- 以非 root 运行，根文件系统只读，监听 0.0.0.0 与清单端口。COPY 必须包含所有真实文件；静态游戏除了 server.py 通常还有 HTML、CSS、JS 模块，不能只复制入口便声称页面完整。Python 可用 `-B` 避免写字节码缓存。
- 使用真实健康路径；无独立探针时可核对已有可用路由。不伪造业务依赖健康；确需改业务才能运行时明确冲突。
- 当前默认应用内存预算 500 MiB；每服务至少 32 MiB、CPU 至少 0.05。活动版、候选版和待清理版未释放的预留合并计入预算；内存按实际占用消耗。以目标服务器实时配置为准。
- 浏览器 localStorage 属于站点数据，无需服务器卷；清理站点数据、切换浏览器或域名可能失去原存档。不能偷偷增加服务器同步改变业务。
- 原应用确需持久文件时，v2 storage 指定独立 name、mountPath: /data、正整数 sizeMiB/maxInodes，服务器须启用并验收硬配额。当前初始每应用预算 50 MiB、共享池 64 GiB，仅作基线；更新复用原存储身份，不能删改挂载规避检查。
- 原应用需要密码或外部 Key 才声明引用。平台仅对约定的托管 MASTER_KEY 自动生成并保存；普通 API Key 由供应商提供，不进 Git。无登录、无加密的静态项目不需要这些引用。

## 选择提交途径

**控制台**：操作者登录 https://awesome.guanghexinzhi.cn，选择创建或原应用，填写仓库和项目名称后提交。平台登录是管理操作身份，不要求业务游戏增加飞书登录。准备仓库时无需先登录。

**普通脚本/CI**：在控制台 API 凭证页创建适当范围的个人凭证；受控来源由管理员配置普通非委托来源凭证。一次凭证可处理多个授权项目，范围以签发设置为准。Bearer 仅留在脚本或后端受控环境，不放进游戏 JS。

首次提交前生成并保存幂等键（1–128 位 ASCII 字母、数字或 `._:-`）及原请求体：

```http
POST https://awesome.guanghexinzhi.cn/api/v1/automation/deployments
Authorization: Bearer <AWESOME_DEPLOY_TOKEN>
Idempotency-Key: <本次申请固定ID>
Content-Type: application/json

{"repository":"https://github.com/your-org/your-project.git","subdomain":"demo"}
```

ref 省略为 main；更新增加明确的 appId。202 仅表示入队，返回例如（节选）：

```json
{"id":"任务ID","appId":"应用ID","status":"queued","phase":"queued","terminal":false,"pollAfterSeconds":5,"active":false,"url":null,"error":null}
```

用提交时同一凭证 GET `/api/v1/automation/deployments/{id}`，返回同一任务的最新快照，按 pollAfterSeconds 有界查询；terminal=true 时判断 status 是否 succeeded，并核对 active/url。没有百分比字段。

首次 POST 响应丢失、尚不知道任务 id 时，可用**同一凭证、同一幂等键和相同请求体**再次 POST，平台返回原任务，不新建任务。已知 id 时先 GET。幂等范围包含凭证，不能更换 Token 后假定仍会去重；凭证已失效且结果未知时，由平台管理员核对原记录。新部署才用新键，同键不同内容返回冲突。

401 检查凭证是否失效；403 检查用户及凭证授权范围；409 排查幂等或应用身份冲突；400/422 校正字段。部署期失败以任务的 error 和 phase 为准，不能仅凭提交 HTTP 成功判断上线。网络超时保留原申请，不循环换 ID 重试。

普通自动化不要求 A 端适配服务、共享 sessionId 或 Redis；Bearer 也不能访问控制台 schema/preflight。拥有部署权限不代表拥有服务器运维权限。

**同组后端委托**：仅当另一个服务需要继承实际登录用户、搜索并代其申请时，读取 [API 契约](api-contract.md)与[后端实施](backend-integration.md)。这条路径的 Redis、飞书命名空间和内部凭证由平台管理员提供，不应强加给普通仓库或 CI；本 Skill 不要求业务开发者自行安装整套平台基础设施。

## 验证与结论

区分源码审查、清单解析、项目测试、实际 HTTP 检查、远端构建、受限容器运行和 HTTPS 发布。只做了前几项，就明确构建/上线未验证；202、queued、pending 都不是成功上线。

有完整平台代码时，可用 `awesome_deploy.manifest.load_manifest` 配合目标 Settings 验证；本机默认 Settings 不代表线上资源可用。只有本 Skill 时不假称运行了完整解析器，可使用网页预检：POST `/api/v1/preflight`，需控制台会话、同源 Origin、X-CSRF-Token，body 为 `{"manifest":"YAML 字符串"}`。它不拉代码、不构建、不预留资源，普通 Bearer 不能调用。

不为检查启动本机 Docker Desktop。按已有远端环境执行用户授权的构建/发布验收，并定向清理新增测试资源。

完整技术协议：https://guanghe.feishu.cn/docx/DSUwdxpXhot6fQx2YticNCtWnHe 。如果协议、示例和实际执行器冲突，列明证据与阻塞点；新模板仍用 v2，不能用旧兼容示例覆盖现行约定，也不能忽略执行器真实拒绝的限制。
