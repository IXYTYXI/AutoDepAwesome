# Linux 运维部署指南

这些文件是部署模板和检查清单，尚未经过真实服务器联调。当前开发环境没有运行中的目标 Linux rootless Docker daemon、Nginx、Certbot；不要把单元测试或 `check` 成功当作 HTTPS 发布及续期已经验收。本阶段不包含审批、飞书多维表格建表和同步以及数据库迁移。

## 1. 固定账号、路径和配置

模板使用非 root 专用账号 `awesome`、示例 UID `1001`、代码目录 `/opt/awesome-deploy`。安装前确认真实 UID，并同时修改平台配置的 `docker_host`、网关配置的 `allowed_uid` 和 worker unit 的 `XDG_RUNTIME_DIR`。该账号不要加入 rootful Docker 的 `docker` 组，也不要授予任意 sudo。

| 路径 | 归属与用途 |
| --- | --- |
| `/opt/awesome-deploy` | 管理员维护的代码和 `.venv`，服务账号只需读取和执行 |
| `/etc/awesome-deploy/config.json` | root 管理，`root:awesome 0640`；来自 `config.example.json` |
| `/etc/awesome-deploy/gateway.json` | `root:root 0600`；来自 `gateway.example.json`，只接受模板中的五个字段 |
| `/var/lib/awesome-deploy` | `awesome:awesome 0700`；SQLite、凭证、秘密版本和源代码状态 |
| `/srv/awesome-docker` | 示例 DockerRootDir；独立有界文件系统的挂载根，由 rootless Docker 使用 |
| `/usr/local/libexec/awesome-gateway` | `root:root 0755`；安装的独立 Python helper，不能从可写 checkout 直接 sudo 执行 |
| `/var/lib/awesome-gateway` | `root:root 0700`；网关发布/回滚日志 |
| `/var/www/awesome-acme` | `root:root 0755`；可被 Nginx 读取的 ACME webroot，不能放到私有网关状态目录下 |
| `/etc/nginx/conf.d` | root 所有且组/其他用户不可写，必须在 Nginx 的 `http` 上下文中加载 |

安装 Python 3.11+、Git、Docker CLI/daemon 所需组件、buildx、Nginx、Certbot 和 sudo；具体包管理方式由目标发行版决定。管理员在 `/opt/awesome-deploy` 创建虚拟环境，可先用 `pip install -r requirements-dev.lock` 安装当前验证过的固定版本，再用 `pip install --no-deps .` 安装项目；锁文件含测试工具，目标机仍需验证版本与架构兼容性。不要向生产源代码目录写入仓库令牌或应用秘密。

在仓库根目录按目标主机调整模板后安装；以下命令仅是由运维执行的示例：

```sh
sudo install -d -o root -g root -m 0755 /etc/awesome-deploy /usr/local/libexec
sudo install -d -o awesome -g awesome -m 0700 /var/lib/awesome-deploy
sudo install -d -o root -g root -m 0700 /var/lib/awesome-gateway
sudo install -d -o root -g root -m 0755 /var/www/awesome-acme
sudo install -o root -g awesome -m 0640 ops/config.example.json /etc/awesome-deploy/config.json
sudo install -o root -g root -m 0600 ops/gateway.example.json /etc/awesome-deploy/gateway.json
sudo install -o root -g root -m 0755 ops/awesome-gateway /usr/local/libexec/awesome-gateway
```

`gateway.json` 的 `email` 必须替换为真实 ACME 联系邮箱。两个 JSON 的 `base_domain` 和端口范围须一致。网关固定使用 `/usr/sbin/nginx`、`/usr/bin/certbot`、`/etc/letsencrypt`、`/etc/nginx/conf.d`；helper 检查文件归属和可写权限，二进制路径也不能是符号链接。这些固定路径不由平台 `config.json` 覆盖，ACME 邮箱仅配置在 root 所有的 `gateway.json`。

## 2. 专用 rootless Docker 和磁盘边界

Docker daemon 与 worker 必须由**同一个非 root UID**运行，使用专属 Unix socket，例如 `unix:///run/user/1001/docker.sock`。socket 必须属于 worker，不能是 root 所有或任意用户可写。必须有 cgroup v2、CPU/内存/PID 配额支持和默认 seccomp；`docker info` 中相关 limit 标记必须实际启用，而不是只存在配置项。

按照 [Docker 官方 rootless 安装指南](https://docs.docker.com/engine/security/rootless/) 准备用户命名空间、subuid/subgid 和用户级 daemon。需要开机维持用户服务时由管理员为该账号启用 linger；随后通过该账号的用户 systemd 管理 Docker。不要复用业务主机上承载特权工作负载的 daemon。

有效的 `DockerRootDir` 必须满足：

- 该路径本身是一个独立挂载点，不能仅是大分区中的普通子目录。
- 它与 `/var/lib/awesome-deploy` 的 `st_dev` 不同；不同目录但同一设备不合格。
- **整个文件系统容量**不超过 `docker_storage_limit_mib`，模板为 81920 MiB（80 GiB）。设置一个目录软配额但 `statvfs` 仍报告更大的总容量不会通过检查。
- 可用空间至少 `required_free_mib`（默认 2048 MiB），可用 inode 不少于 1024。
- rootless daemon 的有效 data-root 指向这个挂载根；源文件冻结后的构建上下文位于其中的 `awesome-build-contexts`，镜像、缓存和构建空间共同受该文件系统容量约束。

由管理员先规划独立分区、LV 或其它真实有界文件系统并安装挂载，再配置专用 daemon。不要盲目格式化已有磁盘；本仓库不提供会破坏现有数据的一键分区脚本。调整 daemon 数据根或迁移存量数据时先停服务并按当前 Docker 安装方式操作。

用服务账号检查实际信息：

```sh
/usr/bin/docker --host unix:///run/user/1001/docker.sock info
```

主机整体还要为操作系统、Nginx、Docker daemon 和构建器留资源。`max_cpu`/`max_memory_mib` 限制的是被预留的应用实例总和，builder 自身单独检查上限；不能把 6 CPU/6144 MiB 当作平台所有进程的整体上限。

## 3. 受限 buildx 构建器

构建器使用平台专用 Docker CLI 配置目录 `/var/lib/awesome-deploy/docker-config`。在普通用户默认 Docker 配置中创建同名 builder，不会自动出现在平台的配置目录里。

`check` 对 `awesome-builder` 实际检查：

1. `docker-container` driver、单节点、running，Endpoint 与 `docker_host` 字面值相同。
2. `buildx inspect` 报告 `worker.oci.process-mode: sandbox`。
3. builder 容器的 `Config.Image` 为固定的 `moby/buildkit:rootless@sha256:<64位digest>`（可带 `docker.io/` 前缀），不能只有可变 tag。
4. `Config.User` 为 `1000` 或 `1000:1000`；实际 CPU 和内存限制大于零且不超过平台配置上限。
5. 不使用 host/container 网络、主机 bind mount 或 `no-process-sandbox`。

[Docker 官方 docker-container driver 文档](https://docs.docker.com/build/builders/drivers/docker-container/)列出了 image、memory、cpu-period、cpu-quota 等 driver 参数。但不同 Docker/buildx/BuildKit 和内核组合是否能同时满足上述 rootless 与 process sandbox 条件，仍须目标机验证；这里不提供未经实测的万能创建命令。运维应选择已审查的固定 BuildKit digest，记录版本与创建参数，并让下面的检查通过。不要为了绕过 `BUILDER_UNSAFE` 而加 `--oci-worker-no-process-sandbox`、取消资源限制或修改校验代码。

```sh
# 以 awesome 身份运行；使用平台同一份 Docker CLI 配置与 endpoint。
/usr/bin/docker --config /var/lib/awesome-deploy/docker-config \
  --host unix:///run/user/1001/docker.sock buildx inspect awesome-builder

/opt/awesome-deploy/.venv/bin/awesome-deploy \
  --config /etc/awesome-deploy/config.json check
```

builder 检查使用普通文本 `buildx inspect` 和真实容器 inspect，不依赖不存在的 `buildx inspect --format`。部署构建强制 `--network=none`；基础镜像可由受控 worker 拉取，但 Dockerfile 的 RUN 不能联网安装依赖。容器共享宿主内核，这不是敌对代码的 VM 沙箱。

## 4. 安装受限网关权限

安装后确认 helper、其父目录 `/usr/local/libexec`、`/etc/awesome-deploy`、`/etc/nginx/conf.d` 均为 root 所有且不可被服务账号修改。helper 不导入项目包，读取固定 root 配置，仅从 stdin 接受受验证的 JSON 操作；worker 不能指定路径或任意命令参数。

用 `visudo -f /etc/sudoers.d/awesome-gateway` 创建最窄授权：

```sudoers
awesome ALL=(root) NOPASSWD: /usr/local/libexec/awesome-gateway ""
```

末尾 `""` 将授权限制为空参数列表。不要授予 `sudo python`、`sudo sh`、任意 Nginx/Certbot 命令、checkout 脚本或 `awesome-gateway *`。helper 还会核对 `SUDO_UID` 与 `gateway.json.allowed_uid`；worker 无权传入 `--reload`。

```sh
sudo visudo -cf /etc/sudoers.d/awesome-gateway
# 然后以 awesome 身份验证 stdin 协议：
printf '%s\n' '{"op":"check"}' | sudo -n /usr/local/libexec/awesome-gateway
```

helper 将应用配置写为 `/etc/nginx/conf.d/awesome-app-<subdomain>.conf`，探针日志定义写为 `00-awesome-probes.conf`。这些名称由平台管理，不能与既有配置争用。每次路由替换先保存回滚日志，再进行 `nginx -t` 和 reload；失败会尝试恢复先前配置。

## 5. DNS、管理 HTTPS 与证书续期

管理入口沿用已确认且已有证书的 `https://awesome.guanghexinzhi.cn`，与 `config.json.public_url` 一致；应用继续使用已配置的 `*.awesome.guanghexinzhi.cn`。核对 A/AAAA 和公网 80/443 可达。管理服务只监听 loopback:8088，不直接开放 8088 或候选端口段到公网。

**保留现有管理根域名的证书配置、renewal 配置和 ACME location。** 该根域名现有 webroot 为 `/var/www/certbot`，不要改为应用 helper 的 `/var/www/awesome-acme`，也不要重新申请根域名证书。只在现有 `awesome.guanghexinzhi.cn` 的 HTTPS 虚拟主机中加入或调整管理入口反代；以下只是 location 片段，不能整段覆盖已有 server 配置：

```nginx
location / {
    client_max_body_size 128k;
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

根域名原有 `/.well-known/acme-challenge/` location 继续指向 `/var/www/certbot`；既有 HTTP 到 HTTPS 跳转和证书路径保持不变。只有新部署的应用子域由 helper 写入自己的 ACME location，并使用 `/var/www/awesome-acme`。

API 使用自身 HTTP Basic 认证，不要改写或移除 Authorization、Origin 等请求头。不要在自定义 access log 中记录认证头。管理员可进一步在管理域名入口限制可信网络，但不要影响应用域名的 ACME 验证。

**保留现有 `certbot.timer`/续期调度，不禁用它，不另建一套竞争的续期 timer。** 把目录 deploy hook 安装到既有 Certbot 流程：

```sh
sudo install -d -o root -g root -m 0755 /etc/letsencrypt/renewal-hooks/deploy
sudo install -o root -g root -m 0755 ops/50-awesome-gateway-deploy-hook \
  /etc/letsencrypt/renewal-hooks/deploy/50-awesome-gateway
```

该 hook 由 root 的 Certbot 续期进程直接调用 `/usr/local/libexec/awesome-gateway --reload`，与发布、回滚共用网关文件锁，先做 `nginx -t` 再 reload。不要通过 worker sudo 执行它，也不要给 worker 追加 `--reload` 权限。

helper 自己已经持有网关锁，申请应用证书时明确执行 `certbot certonly --no-directory-hooks ...`，避免同步触发 deploy hook 再等同一把锁造成死锁。平台管理的证书若在各自 `/etc/letsencrypt/renewal/*.conf` 内带自定义 pre/post/renew/deploy hook，helper 会拒绝；管理员须先审查并协调这些既有 hook，保留无关证书的流程。目录 deploy hook 与证书配置内的自定义 hook 是两回事。Certbot 的续期和 hook 行为可查 [官方使用说明](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。

确保 `/var/www/awesome-acme` 及其父目录可被 Nginx worker 遍历，挑战文件可读取。上线前应使用临时挑战文件验证 HTTP 路径。按现有证书策略执行受控续期演练，并单独验证 hook 的 root 调用；不要未经检查就在生产证书上反复申请。

## 6. 初始化并启动

先以 `awesome` 身份运行，秘密值只通过不回显提示输入：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json init
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json check
```

`check` 失败时先处理明确错误：

| 错误 | 检查方向 |
| --- | --- |
| `HOST_UNSUPPORTED` | 必须是 Linux，不能在当前 macOS 环境实际部署 |
| `HOST_UNAVAILABLE` / `HOST_UNSAFE` | UID、Unix socket、rootless 标记、cgroup v2、seccomp、实际配额能力 |
| `STORAGE_UNSAFE` | DockerRootDir 是否为独立挂载根、与 state 不同设备、文件系统总容量上限 |
| `DISK_CAPACITY` | state 与 Docker 文件系统可用空间、inode；旧版本未确认清理也继续占用配额 |
| `BUILDER_UNSAFE` | 上述 builder 的 endpoint、固定镜像、UID、process sandbox 与实际资源限制 |
| 网关命令失败 / `GATEWAY_FAILED` | sudoers 空参数限制、allowed_uid、root 归属、Nginx/Certbot 路径及 `nginx -t` |
| `GIT_FAILED` | 允许的仓库 host、ref、网络和运维配置的 askpass |

先核对模板中的用户名、UID、路径，再安装 systemd unit：

```sh
sudo install -o root -g root -m 0644 ops/awesome-api.service /etc/systemd/system/awesome-api.service
sudo install -o root -g root -m 0644 ops/awesome-worker.service /etc/systemd/system/awesome-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now awesome-api.service awesome-worker.service
sudo systemctl status awesome-api.service awesome-worker.service
```

rootless Docker 仍由同 UID 的用户服务管理；系统级 worker unit 不会代替它启动 rootful Docker。worker 的 `ExecStartPre` 会再次运行 `check`，daemon 尚未就绪会失败并按间隔重试。

API unit 启用 `NoNewPrivileges` 和只读系统保护；worker 必须调用精确授权的 sudo helper，不能直接照搬 API 的 `NoNewPrivileges=true` 或把 helper 要写的 `/etc/nginx` 设为只读。停 worker 时需让进程组结束，再恢复；文件锁由子进程继承，避免父进程崩溃后出现两个并发执行者。

## 7. 私有 Git、应用秘密和日志

私有 Git 使用 root 管理、服务账号可执行的绝对路径 `git_askpass`。helper 应按提示读取仓库外的受限凭证，并只输出 Git 所需值，不输出调试信息。Git 会清除普通用户配置、credential helper 和任意继承环境；不要假设 `HOME`、shell profile 或交互登录中的 token 对 worker 可见。模板默认 `git_askpass: null`，只适合无交互可读取的公开仓库。

应用秘密通过 `secret-set --repository ... --subdomain ... --name ...` 交互输入；详见主 README。备份 state 时包含版本化秘密及凭证，按秘密备份管理；不要把 SQLite 或 state 打包上传为公开诊断附件。

API/worker 输出进入 journald。Docker 应用容器使用 local 日志驱动，每个容器约束为 `5m × 2` 文件；镜像和 builder 缓存仍需管理员在专用磁盘边界内管理。可评估安装 `awesome-gateway.logrotate` 轮转 root 所有的探针日志；先排查是否与已有 Nginx logrotate 通配规则重复，避免同一路径重复定义。它使用 `nginx -s reopen` 重新打开日志，不替代证书 deploy hook 的加锁 reload。

## 8. 目标机验收与恢复

上线前至少记录以下实机结果，而不是仅勾选测试通过：

1. 在专用 UID 下 `check` 成功，记录内核、Docker/buildx/BuildKit 版本、digest、cgroup 与磁盘边界。
2. 将两个 example 分别复制到独立 Git 仓库根，完成单服务和前端/API 部署，核对 `/`、`/abc`、HTTPS 证书与实际上游端口。
3. 修改 ref 后确认旧任务始终使用原 commit；更新部署提供原 `appId`，验证域名不会被新应用抢占。
4. 构造候选健康检查失败和发布后验证失败，确认旧版本仍响应、候选清理顺序正确、资源只有确认清理后才释放。
5. 在受控窗口中测试 worker 中断恢复，包括发布意图之后、成功切换但回收之前；确认全程互斥锁和持久化退役重试。
6. 验证 CPU、内存、PID、只读根目录、临时空间、构建网络和有限磁盘实际生效；不要仅看命令参数。
7. 分别验证根域名 `/var/www/certbot` 与应用子域 `/var/www/awesome-acme` 的 HTTP challenge、既有 Certbot 续期调度及目录 hook，确认续期与发布竞争时不会死锁或覆盖路由。

`recovery_required` 表示平台不能确认网关或资源状态。先停止新的运维操作，读取任务阶段和安全事件，核对 root 网关日志中的 current/pending 及具体应用/版本 Docker labels，再决定恢复方案。系统尚无通用恢复 CLI 或数据库修复命令；不要清空 worker 锁、直接删除数据库 reservation 或把任务改为成功来绕过确认。保留旧版本与候选版本，直到网关回滚和资源归属都明确。
