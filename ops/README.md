# Linux 运维部署指南

这些文件是部署模板和检查清单，每次安装都须保留目标环境的实测记录。不要把单元测试或 `check` 成功当作 HTTPS 发布及续期已经验收。本阶段不包含审批、飞书多维表格建表和同步以及数据库迁移。

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

平台源码统一维护于 `main`。安装或升级时，从 `main` 选择已审查的完整提交并固定 commit，或使用正式发布标签；平台包、网关及存储 helper 必须来自同一提交。记录部署提交以便核对和回退，不再依赖已归档功能分支作为安装入口。当前配置和实际验收决定哪些功能可启用。

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

建议将配置的端口段专供平台使用。数据库预留及 worker 全局锁只协调平台任务；bind 探测后会关闭 socket，不能阻止外部进程在 RootlessKit 实际绑定前抢占端口。绑定失败会进入候选版本清理，只有确认清理后才释放资源预留。

## 2. 专用 rootless Docker 和磁盘边界

Docker daemon 与 worker 必须由**同一个非 root UID**运行，使用专属 Unix socket，例如 `unix:///run/user/1001/docker.sock`。socket 必须属于 worker，不能是 root 所有或任意用户可写。必须有 cgroup v2、CPU/内存/PID 配额支持和默认 seccomp；`docker info` 中相关 limit 标记必须实际启用，而不是只存在配置项。

从 0.1.2 起，隔离容器的本机入口由同一 UID 的 RootlessKit builtin 端口驱动提供。普通 Docker `--publish` 在仅连接 `--internal` 网络时不会建立实际主机端口，不能作为健康探测或 Nginx 上游。平台保留 internal 网络，直接将 `127.0.0.1:<预留端口>` 转发到受管容器 IPv4 和冻结服务端口。0.5.0 的可选公网模式另外添加受限出口网，入站转发目标仍使用原服务网地址。

### 运行时公网直连

默认配置仍为 `runtime_network: internal`。启用新模式前，安装 `nft`、`nsenter`，使用支持 `network connect --gw-priority` 的 Docker，并确认 RootlessKit 使用 slirp4netns、tap0 与 builtin 端口驱动，开启 `--disable-host-loopback`。Docker 必须关闭 live-restore，业务容器保持 `restart=no`，由 worker 在恢复隔离规则后统一恢复。不得另设自动启动业务容器的进程。

配置 `runtime_network: public-https-v1`、`egress_docker_pid_file: /run/user/<实际UID>/docker.pid`、`runtime_dns: ["223.5.5.5", "1.1.1.1"]`，并在 `runtime_blocked_ips` 中列出**全部**宿主机公网/私网 IPv4 地址，尤其云平台 NAT 公网地址。示例文件不填写虚构的公网地址，也不会默认启用。DNS 和地址策略会冻结到任务快照，修改全局策略前须停止所有公网模式应用，制定旧快照与配置的迁移/回滚方案；不能在在线应用间切换不一致的全局策略。

worker 以自身 UID 进入经过验证的 Docker user/net namespace，只管理 `inet aw_egress_v1` 表；不改 Docker 表，也不需要额外 root sudo 权限。guard 在任何公网业务网络连接/启动之前安装完整原子规则，既有表不符则拒绝继续。`awx-*` 为专属出口桥，`awi-*` 为新模式内部桥；内部跨桥隔离仍依赖 Docker 的 internal 网络规则。不要手工修改这些接口、Docker 防火墙或该表。规则检查失败时，当前被恢复的应用会停止并等待处理；周期恢复不是对管理员恶意删防火墙的零空窗防护。

公网模式只允许公网 IPv4 TCP 443 及指定 DNS 的 TCP/UDP 53，拒绝非公网地址、配置的宿主地址、IPv6 和其他端口。公网端口过滤不检查 TLS 内容，也不限制域名；HTTP 80、UDP 443/QUIC 和自定义模型 API 端口均不支持。构建网络仍为 `none`。原有没有策略字段的快照保持旧行为，升级后需新部署才获得出口。

以 awesome 运行 `ops/verify-direct-egress.py --config <已配置文件>` 可用已有 `python:3.12-slim` 镜像创建并清理独立的验收容器，验证 TLS、DNS、依赖连接、入站健康检查、拒绝目标及容器恢复。该命令不会提交用户项目或发布应用路由；需要先在隔离环境覆盖 daemon/worker 重启和失败回滚，再启用正式发布。只有 TCP 握手/HTTP 响应成功不等于模型账户鉴权、配额或推理已经验证。

默认 RootlessKit API 位于 Docker socket 同级目录的 `dockerd-rootless/api.sock`；非标准安装可设置配置项 `rootlesskit_socket`，相对路径按配置文件目录解析。API socket 须属于 worker，不能为 root 所有或 world-writable，端口驱动必须是 builtin。检查会失败关闭，不能通过更改业务网络来绕过。协议参见 [RootlessKit v3.2.0 API](https://raw.githubusercontent.com/rootless-containers/rootlesskit/v3.2.0/pkg/api/openapi.yaml)。

端口创建意图与容器、网络身份先以 `confirmed=false` 写入任务 execution，确认转发存在后再保存 `confirmed=true`；重建已确认但缺失的转发也遵循这个顺序。未确认意图只能认领当前精确匹配的映射，不能重新发送创建请求；若映射暂未出现，清理须保留容器和预留资源，以免尚未完成的旧请求随后建立孤儿转发。进程恰在意图写入后、请求发送前退出，或在删除未确认映射后退出，也可能需要人工核验。

RootlessKit 数字 ID 会重用，清理必须按当前完整目标匹配，先删除转发再删除容器/网络，最后释放预留资源。转发删除失败时保留资源等待恢复。诊断入口要检查 RootlessKit 映射、主机 localhost listener 和实际 HTTP 响应，不能只检查 Docker PortBindings。

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

`verify` 成功后会保留 `pending` 回滚日志，因为网关验证与 worker 的 SQLite 成功提交不是同一事务；如果在两者之间中断，恢复流程仍需该日志回退。数据库提交成功后，日志可以一直保留到下次 `publish` 覆盖。不要仅凭 `pending` 存在判断发布未完成，也不要手动清除；应结合数据库任务状态、当前版本指针及网关 `current.verified` 判断。

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

API 按 `auth_mode` 使用兼容的 HTTP Basic 或飞书独立会话；多人使用按 [飞书登录操作说明](FEISHU-LOGIN.md) 配置后者。不要改写或移除 Authorization、Cookie、Origin、X-CSRF-Token 等认证请求头，也不要记录其值。飞书回调还须禁止日志记录含 code 的查询参数，具体反代片段见该说明。管理员可在管理域名入口限制可信网络，但不要影响应用域名的 ACME 验证。

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

rootless Docker 仍由同 UID 的用户服务管理；系统级 worker unit 不会代替它启动 rootful Docker。worker 启动不再使用全量 `check` 作为 `ExecStartPre`，避免 builder 或证书暂不可用时连既有容器都无法恢复。安装和新部署仍需相应检查。升级时重新安装本仓库的 worker unit 并运行 `daemon-reload`；仅更新 Python 包、保留旧 unit 会留下原启动门槛。

worker 检测到新的 Linux boot ID 后，优先逐一尝试恢复当前成功版本，再继续耗时构建；恢复失败也会记录本次尝试，避免阻塞其他应用。常态检查间隔为 60 秒，失败按 60、120、240、480、900 秒退避，上限 900 秒且跨 worker 重启保留；单次容器恢复受 `health_timeout` 限制。正在执行的部署也可能延迟周期检查，因此这些是调度间隔，不是对外可用时间保证。普通 `recovery_required` 应用只会尝试恢复数据库记录的旧当前版本。带未完成 `storage_handoff_intent` 的应用禁止这条自动恢复入口，即使旧当前容器已停止；必须先确认候选停止/清理，避免共享卷双写。候选资源和人工恢复标记继续保留。

API unit 启用 `NoNewPrivileges` 和只读系统保护；worker 必须调用精确授权的 sudo helper，不能直接照搬 API 的 `NoNewPrivileges=true` 或把 helper 要写的 `/etc/nginx` 设为只读。停 worker 时需让进程组结束，再恢复；文件锁由子进程继承，避免父进程崩溃后出现两个并发执行者。

## 7. 私有 Git、应用秘密和日志

### Git 拉取代理（0.5.1）

仓库拉取运行在宿主机 worker 内。若服务器已有受管理员管理的代理，在 `/etc/awesome-deploy/config.json` 设置：

```json
"git_proxy_url": "http://127.0.0.1:7890"
```

将上面的字段加入现有 JSON 对象；不要用这一行覆盖整个配置文件。默认值为 `null`，保持直连。仅接受带明确端口的 `http`、`https` 或 `socks5h` 地址；禁止用户名、密码、查询参数、fragment、非根路径和空白字符。代理本身应在平台之外安装和维护，确保服务账号可连接。

平台使用 Git 的 `--config-env` 将地址传给拉取进程，要求 Git 2.31 或更新版本。代理地址不写入仓库配置，也不放进 Git 命令参数；不会继承 shell 的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 或 `NO_PROXY`。HTTPS 仓库证书和 HTTPS 代理证书校验始终开启；配置了代理后，代理不可用就按原有错误分类失败，不会静默尝试直连。认证仍由单独的 `git_askpass` 管理，代理不能代替私有仓库凭证。

在平台空闲时修改配置并重启 API 与 worker，使用一个公开测试仓库核对实际分支和拉取结果。代理只影响 Git 的网络操作，本地提交解析、归档、业务容器、镜像构建、模型 API 均保持原行为；不修改用户仓库文件或用户提交的 ref。拉取仍受 `git_timeout` 限制；代理不能修复不存在的分支、标签或提交。

### 私有仓库与秘密配置

0.6.0 增加部署页面的缺失配置填写入口。只有已经固定代码并报告 `SECRET_NOT_FOUND` 的失败任务可使用；GET 只显示所需引用和状态，POST 使用管理账号及同源写入保护。网页不接受客户端指定仓库、域名或应用身份，均从任务记录推导。保存时按 worker 锁、SQLite 写事务顺序重新核对归属和进行中的任务；已有指针原子保护，不覆盖现有配置。历史归属冲突仍需使用原应用或独立前缀。

0.6.1 将已支持的 `MASTER_KEY` 约定改为平台管理：仅引用名精确为 `MASTER_KEY` 且全部用途的环境变量名也为 `MASTER_KEY` 时，metadata 标记 `managed: true`。新 scope 缺失该项时，POST 在同一锁与归属检查下于服务器生成 `secrets.token_hex(32)`，用户项先保存，托管项后保存。API 不返回生成值，不接受客户端传入托管项，也不覆盖已有版本。仅托管项缺失时可提交 `{"values":{}}`；全部配置已存在时空请求可幂等返回状态。现有应用缺失原密钥时拒绝自动生成，要求恢复原配置。恢复或迁移继续使用仓库外的安全管理通道，绝不把实际值写进 Git。页面不再提供随机值生成按钮或保管确认框。

网页请求为 `POST /api/v1/deployments/<job_id>/secrets`，JSON 形如 `{"values":{"REFERENCE":"用户输入的值"}}`；值通过 HTTPS 请求正文传输，不应写入命令参数或访问日志。该接口只保存配置，不创建部署；页面在保存成功后通过既有部署接口提交新任务。磁盘写入中断时，已保存项保留，刷新状态后只补剩余缺项。不可用的旧配置不能通过此入口随机替换，需由运维恢复或轮换。

私有 Git 使用 root 管理、服务账号可执行的绝对路径 `git_askpass`。helper 应按提示读取仓库外的受限凭证，并只输出 Git 所需值，不输出调试信息。Git 会清除普通用户配置、credential helper 和任意继承环境；不要假设 `HOME`、shell profile 或交互登录中的 token 对 worker 可见。模板默认 `git_askpass: null`，只适合无交互可读取的公开仓库。

应用秘密通过 `secret-set --repository ... --subdomain ... --name ...` 交互输入；详见主 README。备份 state 时包含版本化秘密及凭证，按秘密备份管理；不要把 SQLite 或 state 打包上传为公开诊断附件。

API/worker 输出进入 journald。Docker 应用容器使用 local 日志驱动，每个容器约束为 `5m × 2` 文件；镜像和 builder 缓存仍需管理员在专用磁盘边界内管理。可评估安装 `awesome-gateway.logrotate` 轮转 root 所有的探针日志；先排查是否与已有 Nginx logrotate 通配规则重复，避免同一路径重复定义。它使用 `nginx -s reopen` 重新打开日志，不替代证书 deploy hook 的加锁 reload。

## 8. 目标机验收与恢复

上线前至少记录以下实机结果，而不是仅勾选测试通过：

1. 在专用 UID 下 `check` 成功，记录内核、Docker/buildx/BuildKit 版本、digest、cgroup 与磁盘边界。
2. 将两个 example 分别复制到独立 Git 仓库根，完成单服务和前端/API 部署，核对 `/`、`/abc`、HTTPS 证书与实际上游端口。
3. 修改 ref 后确认旧任务始终使用原 commit；更新部署提供原 `appId`，验证域名不会被新应用抢占。
4. 构造候选健康检查失败和发布后验证失败，确认旧版本仍响应、候选清理顺序正确、资源只有确认清理后才释放。
5. 在受控窗口中测试 worker 中断恢复，包括发布意图之后、成功切换但回收之前；确认全程互斥锁和持久化退役重试。还须按下面步骤执行主机重启验收。
6. 验证 CPU、内存、PID、只读根目录、临时空间、构建网络和有限磁盘实际生效；不要仅看命令参数。
7. 分别验证根域名 `/var/www/certbot` 与应用子域 `/var/www/awesome-acme` 的 HTTP challenge、既有 Certbot 续期调度及目录 hook，确认续期与发布竞争时不会死锁或覆盖路由。

`recovery_required` 表示平台不能确认网关或资源状态。先停止新的运维操作，读取任务阶段和安全事件，核对 root 网关日志中的 current/pending 及具体应用/版本 Docker labels，再决定恢复方案。系统尚无通用恢复 CLI 或数据库修复命令；不要清空 worker 锁、直接删除数据库 reservation 或把任务改为成功来绕过确认。保留旧版本与候选版本，直到网关回滚和资源归属都明确。

### 重启与重载验收（由部署人员在目标机执行）

本地回归使用真实 SQLite/Git 和受控 Docker、TLS 边界替身，尚未执行以下实机操作。验收期间使用测试应用及维护窗口，保存每项实际结果。

1. 升级 Python 包、root 所有的 `awesome-gateway` 和 worker unit。确认 rootless Docker 的用户服务与 `awesome-worker.service` 都已启用开机启动；运行完整 `check`。应用恢复不需要重建镜像，不依赖 BuildKit 正常工作。
2. 成功部署一个双服务测试应用，记录当前部署 ID、完整 commit、各容器 ID/镜像 ID、本机端口与 HTTPS 返回内容；另保留一个失败部署记录用于对照。确认 `/` 与 `/abc` 正常。
3. 在维护窗口重启目标主机，等待 rootless Docker、Nginx、worker 启动；无需提交新部署。核对当前成功版本恢复、两个路径重新可用，容器 ID、commit、镜像 ID、端口保持不变，失败/退役版本没有被重新启动。
4. 单独重启 rootless Docker daemon，验证运行中的 worker 也会发现停止的当前版本并恢复。若 Docker 暂不可用，确认恢复事件说明失败、重试有退避，成功部署记录和端口预留未被删除。
5. 在另一测试应用更新发布期间模拟 worker 中断并重启。核对先恢复数据库记录的旧当前版本，随后确认旧 HTTPS 路由，再清理候选；恢复或回退无法确认时，应保留候选资源并标记需人工恢复。
6. 在多次连续更新中核对发布探针确实建立新连接，最终指向新端口；短暂旧进程响应不应立即导致回滚。故意维持错误上游时，探测在总截止时间后失败，回退确认之前不得删除候选。日志关联必须同时匹配 token、域名、路径、端口和状态。
7. 确认现有容器丢失或身份不一致时只报告恢复问题，不会拉取最新分支、重新构建或改写网关。需要停机维护某个应用时，应先停止 worker；只执行 `docker stop` 会被平台按“当前版本应运行”恢复。

Docker 的自动重启策略与主机进程管理器不宜同时管理同一容器；本平台使用 worker 这一处恢复入口，保留 `--restart=no`，详见 [Docker 重启策略说明](https://docs.docker.com/engine/containers/start-containers-automatically/)。Nginx 的配置重载会启动新 worker 并让旧 worker 完成旧连接，因此验证必须允许切换窗口，详见 [Nginx 控制说明](https://nginx.org/en/docs/control.html)。


## 9. 传统 local 持久卷更新、备份与容量（0.2.0）

每个声明 `storage: {name: images, mountPath: /data}` 的服务使用唯一受控 local 卷。派生卷名、随机实例标识和创建时间保存在任务 execution 的 `storage_volumes`；归属由 `awesome.managed`、`awesome.app`、`awesome.service`、`awesome.storage` 与 `awesome.storage-instance` labels 共同确定。卷不使用 job label，跨版本复用；容器、网络和镜像退役不删除卷。不得对专用 daemon 执行 `docker volume prune`，也不得把旧容器删除与数据卷删除混为一谈。

升级平台包及 API/worker 后，先确认 `/api/v1/capabilities` 的 `persistentStorage=true`。镜像内 `/data` 应归平台配置的 `runtime_user`（默认 `65532:65532`）所有，首次卷 copy-up 才有正确权限。已有卷不会自动重新初始化或递归改属主。原有无存储应用可直接沿用，无需改 manifest。

有存储更新会在镜像构建完成后短暂停止整个旧版本，再启动候选。运维须安排可接受的停机窗口。候选失败时平台先验证其退出并清理，再拉起旧版本；任何未确认步骤都会保留资源并阻断维护扫描，须人工核查。不要绕开全局 worker 锁手工启动旧版或候选。主动停机维护前应停止 worker 并等待其整个进程组退出。

备份建议：记录当前成功版本 ID、commit、卷名和 labels；停止 worker，再停止使用该卷的应用并确认退出，使用受审查的备份工具只读导出卷内容，连同所需版本元数据和受保护的配置/秘密做离机备份。恢复演练使用独立测试环境和新卷，逐项验证图片字节及元数据。生产卷若缺失或身份不符，平台会拒绝自动启动，恢复不能只改 execution 的创建时间来绕过校验；需要运维审查完整的容器、卷与状态迁移方案。没有通用一键灾备命令。

自动应用回滚不会撤销卷写入或回滚存储格式/schema，故破坏性数据迁移须先备份并另行安排维护。卷与镜像、构建缓存共同使用专用有界 Docker 文件系统；local 后端不提供逐卷硬配额；0.8.0 可选独立 XFS 后端的安装与验收见 [存储硬配额说明](STORAGE-QUOTA.md)。除应用自身上传/总量限制外，持续监控可用字节、inode 和备份新鲜度，低容量时暂停新部署及上传，避免把持久化理解为无限容量或备份替代品。

至少验收：上传后停止/恢复容器图片仍在；更新版本后字节一致；健康失败先停候选再恢复旧版；在交接意图写入后、旧版停止后、候选启动后、候选清理后分别中断 worker；未确认交接转人工恢复时维护扫描不启动旧实例；卷丢失/名称冲突/driver/options/labels/随机实例标识/CreatedAt 变化均不得创建空卷或继续运行；不同应用不能访问彼此的卷。测试应用结束时先确认数据可删除，再由管理员精确处理目标卷，勿使用批量 prune。

## 10. 可选 XFS 存储硬配额（0.8.0）

新增字段、root helper 安装、双维预算、巡检 timer、未决操作恢复、迁移限制及验收矩阵统一维护在 [STORAGE-QUOTA.md](STORAGE-QUOTA.md)。默认未启用；不能仅更改后端设置就把既有 named volume 当作受配额保护的数据。真实 Linux/XFS 验收与 DEC-QUOTA-001 确认尚未完成。
