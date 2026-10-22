# 旧卷迁移到 XFS 配额卷

平台版本：0.9.1。

`migrate-legacy-storage.py` 是受控维护工具，不是自动部署路径。原发布 snapshot、commit、镜像保持不变；平台 `storage_migrations` 保存完整 source → target 关联。运行时使用目标引用的副本，持久发布引用仍保留原 source，历史删除流程同时覆盖目标与尚未清理的原卷。没有迁移记录的旧卷仍拒绝在 XFS 后端启动。

当前工具仅支持**单服务、扁平普通文件、单硬链接、文件 UID/GID 等于 runtime_user、无特殊权限位**。未知目录、链接、设备、文件所有者及额外目标内容都拒绝复制。复制使用已审查的本地不可变 Python 镜像，容器无网络、只读根目录、无 capabilities、以 runtime_user 运行。原卷只读挂载在 `/source`，helper 返回的精确目标挂载到 `/data`。复制 writer 通过现有 helper admission 登记，完成后确认退出、定向删除容器并 retire。没有 host root 解包、chown 或任意宿主路径入口。

## 执行前

- 部署含迁移模块的同一平台版本，保持生产业务镜像不变；先确认 XFS helper、映射和池已完成验收。
- 确认每应用所有存储合计不超过 50 MiB；本工具为唯一服务申请 50 MiB，inode 额度必须显式提供并受 helper 的应用/服务/全局预算约束。
- 保留平台数据库和数据池账本的一致备份。原卷在验收后仍保留，直到明确进行精确实例清理。
- 记录当前活动 release、完整原卷实例、业务容器 ID、immutable copy image ID。copy image 必须已有 Python 3.11+，不得含 Docker `VOLUME` 声明造成额外挂载。
- 先使用默认计划模式。计划通过只读 SQLite 读取现有状态，不分配目标、不停容器、不修改数据库。必须由 worker 账号运行。

```sh
/opt/awesome-deploy/.venv/bin/python /path/to/reviewed/migrate-legacy-storage.py \
  --config /etc/awesome-deploy/config.json \
  --release-id <当前release> --service web --operation-id <稳定唯一操作ID> \
  --copy-image sha256:<已审查本地Python镜像ID> --max-inodes <已批准inode硬限>
```

正式执行前，管理员停止 `awesome-api.service`、`awesome-worker.service`；脚本要求两者均为 loaded/inactive/dead，并持有 worker.lock。既有业务在这一时刻仍可运行，脚本只停止指定活动 release 的精确旧写入者。其他业务不停止。再次运行相同命令并加 `--apply`。

## 阶段与中断

1. `prepared`：目标必须为空且已设置真实双硬限。源卷、目标代际、活动 release、snapshot/镜像摘要均持久保存。停止所有原卷引用者后，由受限 copy writer 复制并 fsync；对比路径、内容 SHA-256、模式、UID/GID、大小的整体摘要。
2. `committed`：只有来源和目标摘要相同、活动发布及来源引用未变化才提交关联。此时尚未允许业务向目标写入。
3. 先撤销精确旧转发并持久记录，再移除精确旧容器。使用同一 frozen job 和镜像经过 `start`、`health`、`recover_active` 重建并核验容器/网络/转发。每次重试和启动前都重新确认该 release 仍是当前活动版本且没有未完成部署或下线操作。
4. `writes_started`：业务 guard 在给 Docker 启动许可前持久记录此状态，包括启动结果不确定的情况。之后禁止直接回旧卷，只允许向前恢复，或另行制定停写后的反向迁移。
5. `source_removed`：业务验收后可明确删除完整身份匹配且无引用的旧卷，再记录删除完成。旧名被另一个实例重建时，后续数据删除必须拒绝，不会把同名当作原备份。

复制容器中断时工具保留其精确 ID 与 helper writer 登记；先检查、停止并确认退出、定向移除该复制容器，再 retire 后重试。不要使用 prune。业务启动中断后再次 `--apply` 只恢复目标，不重新复制来源。工具不自动修改全局 storage_backend；管理员必须在两应用迁移并验收后将其设为 xfs-project，再恢复 API/worker。

在 `prepared` 或 `committed` 且**尚未有业务启动意图**时，可在维护锁内调用 `StorageMigrations.rollback(operation_id)`。回退关联后，仅在确认复制 writer 已停止、业务 target 尚无写入并恢复原容器/转发之后，才能继续 local 服务。此方法不负责容器恢复，也不自动清理 target。已放弃的目标须通过 helper 精确代际删除并确认归零后，用**新 operation_id、新 target generation**重新迁移；旧审计记录保留。禁止复用已回退目标代际。

正常最终清理按顺序使用 `ManagedStorage.remove(app_id, service, spec, source)` 完整实例核验和 `StorageMigrations.source_removed(operation_id, source)`。不能直接 `docker volume rm <名字>` 后假定完成。平台之后的“删除数据”流程也会处理保留 source。

## 验证范围

定向测试覆盖身份变化、摘要不一致、活动版本变化、启动前回退、启动意图锁定、历史引用/删除、同名源卷被替换、旧操作重试、prepared 目标业务禁入和受限文件复制。Mac 文件系统不保留 setuid 位，因此该特定复制拒绝测试在本地跳过，已在服务器独立临时目录中的 Linux 测试通过；临时源码、测试数据和上传归档均已清理。真实 rootless/XFS 复制、维护中断恢复和业务读写验收须由操作者在维护窗口执行；隔离单元测试不代表生产已迁移。
