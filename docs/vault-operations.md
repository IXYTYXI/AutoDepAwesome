# 加密密钥库运维与恢复

本文件描述 B 阶段代码的受控操作方法。它不表示目标服务器已经迁移，也不替代真实 OAuth、备份恢复、卷身份及字节/inode 硬配额验收。迁移不会修改用户仓库或业务代码，不会停止健康业务容器；迁移窗口内需要停止平台 API 和 worker，业务容器继续运行。

## 边界与准备

- 以平台服务的系统用户执行下列命令。CLI 审计 actor 为实际 Unix UID，例如 `cli-uid-1000`；恢复记录另记负责人，须关联组织运维操作记录。这不是飞书个人身份的替代。
- 停止平台 API、worker 和旧管理写入；确保没有排队/运行/恢复中的部署、下线或吊销任务。命令还会获取 `worker.lock`，不能通过删除锁文件绕过。
- 制作一致状态备份和业务数据备份。登记仍保留的卷实例/数据代际、原成功版本、全部密钥历史版本、销毁记录与密钥吊销记录。不要把主密钥重建当成恢复。
- 目标企业 `feishu_tenant_key`、`feishu_app_id` 必须确定且一致。旧应用及旧配置空间须先由管理员核对绑定，不能以仓库相同、域名相同或首位登录者身份自动认领。
- 使用平台状态目录之外的独立凭证目录，例如 `/etc/awesome-deploy-vault/`，由平台服务用户持有、权限 `0700`。KEK JSON 文件权限 `0600`、单一硬链接，路径所有组件不能是符号链接。不要与状态备份、仓库或普通配置 JSON 一起保存密钥字节。
- 以下路径为运维示例。普通配置只存 `secret_backend` 和 `secret_kek_file` 路径，不存 KEK 内容。CLI 不接受命令参数中的业务密钥值。

## 恢复记录

`recovery.json` 只保存以下非秘密元信息。`verified_at`、`retention_until` 是带时区的 ISO 时间，保留截止必须晚于核验时间。参考编号应指向受限备份记录，不要放密码、密钥、可逆片段或秘密内容摘要。

```json
{
  "backup_reference": "受限状态及业务数据备份编号",
  "kek_recovery_reference": "独立KEK副本保管记录及key_id",
  "responsible": "负责恢复与到期清理的人员",
  "verified_at": "2026-10-21T10:00:00+08:00",
  "retention_until": "2027-04-21T10:00:00+08:00"
}
```

程序验证元信息格式并保存审计，不能证明管理员已经制作了离机备份。必须实际核验恢复副本；未完成时不要执行初始化或迁移。

首次生成 KEK 时尚不可能核验它的独立副本。仅 `vault-keyring-init --manifest` 接受清单中显式的 `"backup": null`，表示恢复记录尚未完成，不表示已备份。清单其余字段与应用、配置空间、全部历史及类型的检查仍须通过；缺失 `backup` 字段或提供残缺记录都会拒绝。生成命令只记录 KEK 操作，不建立迁移记录、应用 DEK 或激活加密格式。已有完整恢复记录仍可用于此命令。

## 空平台初始化

只有没有应用、任务历史、旧配置空间及加密记录的空平台才能使用 `vault-init`。已有状态即使目前没有在线容器也必须走迁移。

1. 配置正确的飞书身份。可先保持 `secret_backend=legacy`，通过 `--kek-file` 明确指定目标路径。
2. 显式生成 KEK。该命令只生成根密钥，不加密或迁移任何应用数据；再次运行不会覆盖已有文件。

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-keyring-init \
  --kek-file /etc/awesome-deploy-vault/keyring.json
```

3. 制作并核验独立 KEK 副本，记录命令返回的 `key_id`，填写 `recovery.json`。
4. 初始化：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-init \
  --kek-file /etc/awesome-deploy-vault/keyring.json \
  --recovery-record /受限运维目录/recovery.json
```

5. 将正式配置设置为：

```json
{
  "secret_backend": "encrypted",
  "secret_kek_file": "/etc/awesome-deploy-vault/keyring.json"
}
```

其余原配置保持。使用兼容新格式版本执行 `vault-check`，确认通过后再启动平台 API/worker。

## 旧平台迁移清单

`migration.json` 要覆盖全部应用、全部已绑定配置空间、空间中所有秘密名称和所有未清除的冻结历史任务。每个名称的类型须人工核对，禁止根据当前清单猜测或降级历史主密钥。一个应用的多个旧空间如存在同名秘密，程序拒绝自动合并；应先完成专项归属/版本映射评审，不改文件绕过检查。

```json
{
  "format": 1,
  "migration_id": "0123456789abcdef0123456789abcdef",
  "tenant_id": "目标tenant_key",
  "feishu_app_id": "cli_目标飞书应用ID",
  "backup": {
    "backup_reference": "一致状态及业务数据备份编号",
    "kek_recovery_reference": "独立KEK副本记录及key_id",
    "responsible": "负责人",
    "verified_at": "2026-10-21T10:00:00+08:00",
    "retention_until": "2027-04-21T10:00:00+08:00"
  },
  "applications": [
    {
      "app_id": "实际稳定应用ID",
      "data_generation": "人工核对的数据代际ID",
      "job_ids": ["未清除的原成功或冻结任务ID"],
      "scopes": [
        {
          "repository": "https://github.com/组织/仓库.git",
          "subdomain": "demo",
          "secrets": [
            {"name": "MODEL_API_KEY", "kind": "runtime_secret"},
            {"name": "MASTER_KEY", "kind": "platform_managed_master"}
          ]
        }
      ]
    }
  ]
}
```

无秘密的应用仍登记应用和需要保留的历史任务；无秘密的已绑定空间使用空 `secrets` 数组。秘密值不进入清单。迁移器自行验证私有目录、current 指针和全部版本，保留原 UUID version ID；不是只导入 current。

冻结历史的原 `app_id`/`input.appId` 若存在，必须与清单一致；缺失归属仅通过管理员明确列出的 `job_ids` 与已确认配置空间映射。它不会改写旧任务快照。任何找不到原版本、损坏路径、孤立目录、未知文件、未完成任务或不明 kind 都会拒绝迁移。

失败任务中只要已保存 `snapshot.secret_versions` 或 `execution.secret_versions` 字段，即使值是空映射，也必须列入历史绑定；迁移和 `vault-check` 使用同一覆盖规则。格式损坏或声明了秘密却没有固定版本仍然拒绝，不把它们当成“没有秘密”。

同一个旧配置空间若被两个不同应用的未清除历史引用，现有迁移器会拒绝自动合并；旧任务指向的应用记录缺失也不能忽略。先核对原应用、独立数据卷和数据代际，并制定明确的保留或清理方案。仅恢复应用行、把旧任务归给当前应用、伪造已删除标记，或删去清单中的历史任务，都不是合法修复。

## 迁移步骤与重试

1. 完成维护窗口、状态与业务数据备份、应用归属与清单核对。
2. 首次生成 KEK 之前，用上述完整清单的全部身份和映射字段预检，但将 `backup` 字段设为 `null`，明确独立 KEK 恢复核验尚未完成；不要填写虚构的副本编号或核验时间：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-keyring-init \
  --kek-file /etc/awesome-deploy-vault/keyring.json \
  --manifest /受限运维目录/migration.json
```

3. 实际备份并核验该 KEK，将同一清单中的 `backup: null` 替换为完整且真实的恢复记录，包含状态/业务备份、独立 KEK 副本、责任人与带时区的核验及保留时间。此时尚未创建迁移记录，可以完善恢复元信息。`vault-migrate`、`vault-init` 和已激活迁移的重试都不接受空恢复记录；生成 KEK 成功不解除这一门禁。
4. 执行迁移，命令不再提供即时生成 KEK 的选项：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-migrate \
  --kek-file /etc/awesome-deploy-vault/keyring.json \
  --manifest /受限运维目录/migration.json
```

迁移阶段为 `maintenance → imported → sealing → active`。进入 maintenance 后清单和迁移 ID 固定。导入与解密比对失败不激活；中断后用同一命令、同一清单和原 KEK 重试，不能更换 migration_id 或新生成根密钥。完成后更新正式配置为 encrypted，再用兼容版本执行 `vault-check`。旧 legacy 配置会被新二进制拒绝。

旧 `state/secrets` 目录被原子改名为 `state/secrets-quarantine-<migration_id>`；原路径变为私有常规文件 sentinel，阻止旧文件存储代码继续创建/读取目录。sentinel 必须保留。封存目录仍然包含明文，是受限恢复材料；迁移并未删除备份，也不承诺 SSD 擦除或已流出副本不可恢复。封存名称、文件身份与恢复记录保存在迁移日志中。

sentinel 只能阻断旧文件密钥读写，不能使旧二进制理解新授权和吊销状态。必须同时保持管理服务停止、切换正式配置并使用新版本，禁止以旧二进制恢复旧容器。切换后已有新写入时，不能直接覆盖旧 DB 或回退旧程序。

## 日常检查、补录与轮换

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-check
```

检查认证全部密文，核验仍保留的冻结引用及应用/代际绑定，不返回值。合法的已吊销历史版本不会被当成密文损坏；未结束的吊销清理或仍活动的吊销版本会单列，返回非零，必须处理。

补录真正缺失的普通运行密钥：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json secret-set \
  --app-id 实际应用ID --name MODEL_API_KEY
```

值仅在交互式隐藏输入时提供。已有配置不会被覆盖。轮换必须给出当前逻辑版本及非敏感原因：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json secret-set \
  --app-id 实际应用ID --name MODEL_API_KEY \
  --expected-current-version 当前32位版本ID --reason '供应商凭证轮换'
```

成功仅影响后续新任务；旧运行实例、冻结任务及回滚历史仍绑定原版本。并发版本变化会拒绝覆盖。普通 CLI 不能轮换持久 kind 为 `platform_managed_master` 的值，也不能借名称/映射改变其类型。`MASTER_KEY` 的首次生成通过平台受控配置流程完成。

紧急吊销使用平台管理页/API 的持久清理流程；不能用修改数据库或仅删除文件代替。供应商侧的 Key 撤销需负责人另行完成并记录。

## 下线与删除应用数据

保留数据的下线不会删除密钥或更换数据代际。选择删除数据时，加密模式使用 v2 清理记录：固定下线操作 ID、授权申请人、所有历史任务/数据卷实例，以及该应用当前代际的全部秘密版本和状态。平台先核对整个范围，确认容器清理完成，再删除原卷；之后在同一数据库事务内清除密钥密文与 DEK 包裹，写入销毁墓碑和审计。普通主密钥轮换保护不阻止这一经明确确认的整代际销毁操作。

清理中断须重试原下线操作，不要手工改清理记录。已删除的原卷和已落库的销毁墓碑允许幂等重试；新增密钥、吊销状态变化、替换卷或新数据代际都会拒绝旧范围清理。完整清理确认之前，新部署和密钥变更仍被门禁阻止。

v1 文件清理记录只适用于 legacy，v2 只适用于 encrypted。加密清理不会读取或删除 `state/secrets` sentinel，也不会顺带删除迁移封存目录或备份副本；这些历史副本必须按恢复记录另行到期清理。“已删除应用数据”表示当前记录范围完成清理，不代表所有外部备份或介质残留已不可恢复。

## KEK 重包裹与恢复

新 KEK 必须由受控凭证工具生成并添加到同一私有 keyring，设置 `active`，保留仍用于现有包裹/备份的旧 key。keyring 格式为 `{format:1, active:密钥ID, keys:{密钥ID:32字节的Base64}}`，不要把真实内容贴到聊天或写进 shell 参数。先备份并核验完整新 keyring，再执行：

```sh
awesome-deploy --config /etc/awesome-deploy/config.json vault-rewrap \
  --recovery-record /受限运维目录/new-kek-recovery.json \
  --reason '计划内平台KEK轮换'
```

重包裹不改变业务原值、应用 MASTER_KEY 或逻辑 secret version。全部写入和审计在同一事务中。旧 KEK 仍须保留到依赖旧备份退出恢复窗口，不能因为当前库已重包裹就删除旧根密钥。

恢复时先关闭新发布，恢复一致状态和独立 KEK；执行 `auth-revoke-all` 撤销备份中的网页会话及在途登录，核对权限、成员、仓库授权、销毁和吊销状态后再启动 worker。原应用 MASTER_KEY 丢失时只能恢复原值，不得自动重建；原卷身份、数据代际、UID/GID 和配额门禁仍必须通过。`vault-check` 只证明平台记录可读，不证明业务数据已能解密，必须完成原成功版本读取原数据的实际演练。

切换前故障可在冻结写入下恢复维护备份；切换后优先使用兼容新格式版本修复。全量恢复必须另行确定数据损失窗口，保留后续审计与撤销信息。封存目录、历史版本、临时文件、WAL、状态备份和 Docker 数据目录中的秘密副本，都需要责任人记录保留期限与到期清理结果；不要仅清理 current 或以全局 Docker prune 代替定向清理。


## 网关升级、运行注入与吊销边界

启用 B 前须同时升级本提交的 `ops/awesome-gateway`，以 `root:root 0755` 安装到 `/usr/local/libexec/awesome-gateway`。沿用 [运维指南](../ops/README.md) 的窄 sudo 权限，不能 sudo 执行可写 checkout。仅升级 Python API/worker 不会使旧 helper 支持新的 `quarantine` 操作。升级前后核验网关状态及已运行版本，再开放吊销功能。

吊销首先在 SQLite 同事务标记目标版本并固定受影响 job IDs；后台按原范围隔离流量、停止实例、核验清理。失败显示 `recovery_required`，保留域名/端口预留并阻止新部署及自动恢复；管理页的“重试停用核验”继续原操作，不重新扩大范围。外部供应商 Key 仍须由负责人另行撤销。

若中断的候选发布同时涉及不在本次吊销范围内的旧在线版本，或网关无法证明另一个当前版本已完全替代目标流量，保持人工恢复状态。不要修改数据库来伪造完成、释放端口或重新启用旧版本。普通运行密钥可在清理期间轮换，但新部署须等待隔离核验完成；下线/销毁与吊销不并行受理。

B 的版本检查与外部 Docker/Nginx 命令并非一个原子事务。命令已发出后遇到吊销，仍可能出现短暂动作交叠，随后由持久清理流程隔离；不承诺零暴露窗口，也无法撤回进程已经读到的值。D 阶段的审批与执行意图串行门禁仍待实现。

Linux 加密模式的临时 env 文件位于 `/dev/shm/awesome-deploy-<uid>-<state-path-id>` 的私有目录，要求实际 tmpfs；目录 `0700`、文件 `0600`，成功和异常后移除，worker 持锁启动时清理遗留文件。缺少 tmpfs 会明确失败，不回退到持久磁盘。Docker 为运行容器保存的环境元数据仍可能含明文；宿主机 root、平台同 UID 与 Docker 管理者仍处在高信任边界，不能把磁盘加密描述成对这些主体保密。
