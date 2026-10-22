# XFS 业务存储硬配额

代码版本 0.8.0，基于平台 0.7.0。依据：[审查后的存储硬配额设计](https://guanghe.feishu.cn/docx/Cy1Tdi4qwo4lHoxqXqic6Wndnrl)。

本次交付包含代码、模拟故障测试和运维模板。**尚未部署，也未完成真实 Linux/XFS/Rootless Docker 验收。** `DEC-QUOTA-001` 仍待确认，原硬配额准入要求未取消。默认 `storage_backend: local` 保持原行为，不代表已有卷具备硬配额。

## 支持边界

每服务最多一个固定 `/data`，XFS project quota 同时限制实际分配块和 inode。inode 包含目录、符号链接等，并非业务“上传文件个数”；硬链接共享 inode。逻辑文件大小、稀疏文件大小、剩余可写字节不能直接等同配额。`/tmp`、日志、镜像、构建缓存、备份和 I/O 速率仍需各自预算。

首次目录为空，不复制镜像 `/data` 内容。应用需要自行处理空目录初始化和写入失败。无种子复制、自动迁移、自动缩容、自动备份或配额审批产品。旧 named volume 不会被偷偷转换；启用新后端前必须安排旧服务迁移或继续保留旧部署环境。旧卷的只读核验及明确删除仍可执行。

平台把宿主 root、worker、受限 helper 和该 worker 的 Docker daemon 视为可信控制面。业务进程不能获得宿主路径选择、project ID、Docker socket 或 helper 权限。不能把此边界解释为能抵抗控制面被攻破。

## 主机准备与安装

仅支持 Linux 5.14+、x86_64/aarch64，固定解释器 `/usr/bin/python3` 须为 Python 3.11+，需 XFS quota 内核能力及 `/usr/sbin/blkid`。实现通过 `quotactl_fd`、项目属性 ioctl 回读实际限制；系统调用版本依据 [Linux man-pages](https://man7.org/linux/man-pages/man2/quotactl.2.html)。Rootless 的实际映射需从 daemon 的 `/proc/<pid>/uid_map` 和 `gid_map` 获取，参见 [Docker Rootless 文档](https://docs.docker.com/engine/security/rootless/)。

由管理员先盘点分区、既有数据和备份，在获准目标上准备专用 XFS 文件系统并启用项目配额的 accounting **和 enforcement**。此交付不包含自动格式化、分区或挂载脚本。不要对在用盘直接套用示例参数。

- 数据池挂载点、设备和 UUID 必须精确匹配；`pool_device` 使用实际块设备路径，不能使用符号链接。
- `pool_root` 是挂载点下的 root 所有目录，须为 project ID 0 且无 project inheritance 或 realtime 标志；各级父目录不可由组或其他用户写入，并给 rootless daemon 必要穿越权限。helper 管理的 generation 父目录为只可穿越，payload 的 owner 由实际映射计算。
- `/var/lib/awesome-storage` 是 root 专属控制状态，**必须在另一个文件系统**；不能仅放在数据池内的另一个目录。
- rootless daemon、worker、patrol 使用同一非 root UID。`runtime_uid/runtime_gid` 与平台 `runtime_user` 一致，映射不可漂移。更改映射须停写并受控修复，不能自动递归 chown。
- 检查目标机 SELinux/AppArmor、rootless bind 权限、cgroup 与已有 Docker 独立文件系统限制；业务 `/data` 数据池不替代 Docker 数据目录限制。

`storage.example.json` 的容量策略为总池 65536 MiB（64 GiB）、单应用合计 50 MiB、单服务最多 50 MiB；多服务应用不能各自获得独立的 50 MiB 应用预算。null 和空映射仍是刻意留空的必填项，直接使用会拒绝启用。实际设备、inode 预算、余量及示例 UID 须由管理员按现场确认。池的元数据和空闲余量会减少实际可承诺容量，不能把完整 64 GiB 都承诺给应用。全局 inode 预算依据小文件压力验收确定，`df -i` 仅辅助观察，不能作为可超卖的库存。

管理员审核五个 Python 源文件及入口后，在仓库根目录执行以下安装步骤。正式升级时先停止相关 worker/patrol，避免混用新旧 helper 文件；保持已运行容器的内核配额。

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec/awesome-storage-lib/storage_quota
sudo install -o root -g root -m 0644 ops/storage_quota/__init__.py ops/storage_quota/errors.py ops/storage_quota/service.py ops/storage_quota/protocol.py ops/storage_quota/xfs.py /usr/local/libexec/awesome-storage-lib/storage_quota/
sudo install -o root -g root -m 0755 ops/awesome-storage /usr/local/libexec/awesome-storage
sudo install -d -o root -g root -m 0700 /var/lib/awesome-storage
sudo visudo -cf ops/awesome-storage.sudoers
sudo install -o root -g root -m 0440 ops/awesome-storage.sudoers /etc/sudoers.d/awesome-storage
```

仅首次安装且配置文件不存在时，运行 `sudo install -o root -g root -m 0600 ops/storage.example.json /etc/awesome-deploy/storage.json`。升级必须保留真实 `storage.json` 和整个 `/var/lib/awesome-storage`，不能用模板覆盖。

安装目录只允许这五个源文件，不得复制 `__pycache__`、`.pyc`、扩展模块或整个可写工作区。入口使用隔离模式并禁写 bytecode，核验 root 归属、权限及精确文件集合。不得授权任意 Python、shell、`xfs_quota` 或可写源码路径的 sudo。

编辑 root 所有的 `storage.json`：填写真实设备、UUID、映射，六项服务/应用/全局双维预算及两项空闲余量。预算检查包括已承诺、未完成操作和保留数据，不按当前低使用量重复分配。`pool_root` 初始应为空；发现数据但账本缺失会拒绝重新分配。

将平台 `config.json` 中 `storage_backend` 设为 `xfs-project`，`storage_command` 保持固定入口。这只表示配置，先执行 `storage-check` 和下方完整验收后才启用正式业务。所有命令由 awesome 服务账号执行：

```sh
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json storage-check
/opt/awesome-deploy/.venv/bin/awesome-deploy --config /etc/awesome-deploy/config.json storage-status
```

调整 patrol service 的 UID 路径并安装 service/timer：

```sh
sudo install -o root -g root -m 0644 ops/awesome-storage-patrol.service ops/awesome-storage-patrol.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now awesome-storage-patrol.timer
sudo systemctl enable awesome-worker.service
```

在测试目标完成验收后，正式启用时执行 `sudo systemctl start awesome-worker.service`；`enable` 本身不启动已停止的进程。保留 worker 开机恢复；业务容器保持 `restart=no`，禁止 Docker live-restore 及外部进程自行启动业务容器。巡检不持 worker 构建锁。60 秒为初始间隔，实际周期含运行耗时，不是失效零空窗保证。应监控巡检服务失败、超时和最后成功时间；如需告警，接入组织现有 systemd/journal 监控，代码不擅自发送飞书消息。

## 仓库清单与能力

新建受配额保护的存储使用 `schemaVersion: 2`，在服务中声明：

```yaml
storage:
  name: appdata
  mountPath: /data
  sizeMiB: 50
  maxInodes: 50000
```

上面容量使用部署模板的单应用上限，inode 数仅演示字段，须与主机策略核对。两个额度必须为正整数；实际可申请范围还受 root 策略约束。修改模板不会为既有 named volume 增加配额，也不会缩减已有 XFS 配额。用户不能提供宿主路径、project ID 或挂载参数。`awesome-deploy schema --version 2` 或 `/api/v1/schema?version=2` 获取完整 schema；省略版本仍返回 v1。

v1 无存储应用兼容。已有 XFS 稳定引用的服务可使用 v1 清单省略额度并沿用账本；首次申请不能省略双硬限。普通发布请求小于现值时保留现值，并返回 `kept_current_limits`；大于现值时要求先显式扩容。业务回滚不回滚额度、数据或数据格式。稳定引用仅含 backend、storage_id、generation，不把可变额度冻结为身份。

`/api/v1/capabilities.storageHardQuota` 区分 supported、configured、enabled、status、checkedAt。API 不提权，读取 worker/check/patrol 写入的 120 秒有效快照；观测在 RPC 前排序，旧响应不能覆盖新状态，unknown 保留 lastSuccessfulCheckAt；缺失或过期为 unknown。快照用于展示，每次实际放行仍由 helper 重新核验。旧 `persistentStorage` 不能证明硬配额有效。

## 查询、扩容、阻断与恢复

`storage-status --app-id <ID>` 返回 initial_limits、账本已提交的 size_mib/max_inodes、实际回读 observed_limits、allocated_bytes、used_inodes、revision、state、pending、checked_at 和 writer 登记。请求结果另含 requested_limits；pending 部分扩容时已提交额度与 observed_limits 可能不同；未完成对账或存在 query_error 时，不能声称两维有效额度已核验。字段为空或查询 unknown 不等于用量为零。容量达到硬限不自动停机，不会自动删除数据或扩容。

管理员从当前查询复制完整 storage_id、generation 和 revision，选取持久保存的唯一操作 ID：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json storage-expand \
  --storage-id <64位标识> --generation <32位代次> \
  --size-mib <新硬限> --max-inodes <新硬限> \
  --expected-revision <当前版本> --operation-id <唯一操作ID>
```

两维均不得减少。不变的一维仍填当前值。操作先持久化预留，再改内核并回读；超时、崩溃和部分生效仍占预算。不能通过换操作 ID 绕过 pending，也不能手改 SQLite 清预留。

查询暂时失败标为 unknown，拒绝新的存储写入启动，不因一次失败停止所有在线服务。确认池/目录/映射/硬限失效会持久阻断。`storage-check`/`storage-status` 本身不停止 Docker 容器；执行或等待 `storage-patrol` 才会停止登记的精确容器。`stop_failures` 表示尚未完成隔离，需要管理员核对和停止；不要把返回错误当作已停妥。

修复原因为真实可验状态后，先 `storage-unblock --pool` 显式解除全池阻断，再对每个 blocked 条目执行 `storage-unblock --storage-id ... --generation ...`。单条解锁不能清除全池阻断；普通检查通过也不会自动解除。之后重新巡检、查看容器状态并验证业务。解锁不修改任意不一致硬限或自动承认替换目录。

`storage-retry --storage-id ... --generation ... --operation-id ...` 只重放账本保存的原始 ensure/expand/delete 请求，不接受新的额度/路径。先核对 pending 和操作 ID；blocked 时先完成上述修复。删除重试仍然是原先明确授权的删除，可能继续清除数据，不能用于仅查看状态。

账本损坏、截断、丢失或表结构异常时禁止自动重新初始化。保存现场，停止相关写入者，在独立目标从一致备份恢复并比对文件系统 UUID、generation、project ID、实际限额与用量后，由管理员执行恢复。不要单独删除 `initialized` 或重新编号。root SQLite 审计记录保存操作人 UID、对象、原始结构化请求、前后状态及结果；定期备份控制状态并监控其文件系统容量。

## 删除与人工迁移

应用下线保留数据时继续占额度；版本回收不会删除数据。现有“下线并删除数据”流程停止并核验全部写入者，再调用 helper。helper 保留删除意图，只有目录身份确认、定向清理完成且项目块/inode 用量为零才释放预算。删除后仍打开的文件继续占用，结果未知保持 pending；不使用全局 prune。旧 generation 的完成记录保留，不能清理后来重建的同名存储。

**本版不提供原应用就地迁移命令。** 不得只改 `storage_volumes`、数据库或 bind 路径冒充迁移。现有 named volume 环境不能直接切换后端并期望自动恢复。受控人工迁移需要单独制定和审核关联身份转换步骤，或使用新应用完整部署与受控数据导入方式；未完成前继续使用旧环境。

迁移规程：确定维护窗口及两份空间预算 → 停止并确认全部写入者 → 固定源卷完整身份并做可恢复备份 → 分配真实双配额目标 → 以受限映射身份复制到固定 payload（不覆盖根目录，不继承来源 project 属性/任意 owner）→ 核对内容/权限/符号链接和用量 → 通过完整部署流程重建容器、网络、转发身份 → 业务检查后切流。此处是规程，不是已实现的自动复制功能。复制过程也须受启动门禁及登记约束，未提供安全迁移工具前不得以宿主 root 随意解包代替。

目标尚无新写入时可按维护方案返回保留源；产生新写入后，必须停写并反向迁移或从备份恢复，不能直接切旧卷造成数据分叉。迁移、源卷和备份保留仍占物理预算。禁止降级到不认识 XFS 引用的旧平台版本直接运行，升级/回退须先停止所有控制进程并验证兼容性。

## 隔离环境验收记录

以下全部需要获准 Linux/XFS 测试目标；本地 pytest 的 fake/ioctl 模拟不算实机通过。记录版本/提交、内核、文件系统 UUID、挂载参数、Docker 与真实 UID/GID map、预算、操作者、时间及原始输出。只使用专门测试服务/数据池；禁止故障注入到在用业务池。

| 场景 | 通过依据 |
| --- | --- |
| 容量及 inode 各自触顶 | 分别写大文件、建小文件/目录/符号链接；分配被内核拒绝，实际双限与用量一致；不要求恰好填满硬限 |
| 稀疏/预分配、硬链接、延迟写入 | 正确理解 allocated bytes；业务检查 write/flush/fsync/close，失败不虚报保存成功 |
| 两服务与累计预算 | A 满额时 B 和平台仍工作；并发申请、pending、保留数据与扩容不超卖 |
| 权限与绕过 | 实际 rootless 可写 `/data`；业务不能清除继承/改 project ID、改同级目录或越界 bind；额外挂载被拒绝 |
| 失效与 unknown | 隔离池上测试 enforcement 关闭、UUID/目录/映射/硬限改变；持久 fence 且精确停写；查询失败只标 unknown；新申请不能绕过全池 block |
| 重启与中断 | 主机、daemon、worker 重启后受控恢复；候选/失败版本不恢复；启动超时登记保留，不能双写 |
| 未决操作 | ensure/expand/delete 各步骤中断；原操作重试完成或仍 pending，预算不提前释放，不自动降额 |
| 删除与并发巡检 | 超深/大量目录、打开未关闭文件；长删除不挡其他服务巡检；仅确认实际用量归零才释放；旧代次不能删除新代次 |
| 更新失败与回滚 | 旧/新 writer 交接无双写；业务回滚保留当前限额及数据；额度不足在停旧版前失败 |
| 池紧张及控制状态故障 | 数据池耗尽不占控制盘；账本损坏/丢失不得重建空真相；未知状态不放行新 writer |
| 备份、恢复及人工迁移 | 在独立目标恢复并验证业务数据；验证镜像目录无自动 copy-up、源目标双份预算及回退条件 |

每项记录“步骤、预期、实际、证据链接、是否通过、清理结果”。验收完成后仅定向清理本次测试容器、数据及镜像，核对额度和物理用量；未确认删除不释放预算。将最终参数和证据同步到运维手册、接入协议，再处理 DEC-QUOTA-001。
