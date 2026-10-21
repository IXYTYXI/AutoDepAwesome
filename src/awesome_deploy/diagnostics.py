"""Safe Chinese presentation of current and historical deployment diagnostics.

Stored errors and events remain immutable. Raw messages, suggestions and arbitrary
metadata are never copied into a public error: only fixed catalog text is used.
Do not add subprocess output or configuration values to this catalog interface.
"""

import re


PHASE_LABELS = {
    "queued": "排队", "source": "获取代码", "fetching": "获取代码", "prepared": "配置已校验",
    "preparing": "准备配置", "validating": "校验配置", "freeze": "固定部署输入", "frozen": "输入已固定",
    "reserved": "资源已预留", "build": "构建镜像", "building": "构建镜像", "built": "镜像构建完成",
    "start": "启动服务", "starting": "启动服务", "health": "健康检查", "https": "准备 HTTPS 证书",
    "certificate": "准备 HTTPS 证书", "publish": "切换路由", "publishing": "切换路由",
    "verify": "验证发布版本", "verifying": "验证发布版本", "rollback": "恢复上一版本",
    "retire": "清理旧版本", "cleanup": "清理资源", "failed": "失败处理", "complete": "完成",
    "recovery": "恢复运行状态", "storage_handoff": "持久存储交接",
    "deactivate": "关闭访问入口", "offline": "下线应用", "stop": "停止服务",
    "offline_queued": "等待下线", "offline_deactivate": "关闭访问入口",
    "offline_stop": "停止服务", "offline_cleanup": "清理运行资源", "offline_complete": "已下线",
    "offline_delete_preflight": "核验数据清除范围", "offline_delete_data": "清除业务持久数据",
    "offline_delete_secrets": "清除应用密码和密钥",
}


# Each entry is (title, message, ordered actions), all trusted constant strings.
_CATALOG = {}


def _register(codes, title, message, *steps):
    for code in codes.split():
        _CATALOG[code] = (title, message, steps)


_register("DEPLOYMENT_FAILED", "部署未完成", "部署执行失败，当前信息不足以确定具体原因。",
          "查看失败阶段和部署记录，核对对应配置。", "请运维人员结合受保护的服务器日志排查；修复后提交新的部署。")
_register("ACCESS_DENIED", "权限已失效", "当前用户或应用成员权限不允许继续此操作。",
          "请联系应用负责人或管理员核对权限。", "恢复授权后重新提交；旧任务不会因重新授权自动恢复执行。")
_register("IDENTITY_UNAVAILABLE", "身份暂时无法核验", "暂时无法确认飞书员工状态或应用可用范围。",
          "稍后重试；持续失败时请管理员核对飞书应用权限和网络。")
_register("AUTH_REAUTH_REQUIRED", "需要重新登录", "此操作需要最近 10 分钟内的飞书登录确认。",
          "在新标签页重新登录，返回页面检查登录状态后重试。")
_register("AUTH_STATE_INVALID", "登录请求已失效", "登录请求过期、已使用或与当前浏览器不匹配。",
          "从平台登录页重新发起飞书登录。")
_register("AUTH_BUSY", "登录请求较多", "暂时无法受理更多登录请求。", "稍后从登录页重试。")
_register("LAST_ADMIN_REQUIRED", "必须保留有效管理员", "此操作会使平台失去最后一位有效管理员，已被拒绝。",
          "先设置并核实另一位有效管理员，再变更当前账号；不要绕过平台权限检查。")
_register("SECRET_VERSION_CONFLICT", "密钥版本已变化", "当前密钥版本与提交时的版本不一致，本次未覆盖新版本。",
          "刷新密钥管理页面，核对当前版本后重新确认是否提交；不要直接重复提交旧请求。")
_register("SECRET_KIND_CONFLICT", "密钥类型或数据代际冲突", "密钥的持久类型或绑定的数据代际与当前操作不一致，平台拒绝变更。",
          "请管理员核对应用、密钥类型和原数据代际；保留原配置与业务数据。",
          "不能通过改名、修改引用或改写配置来解除主密钥保护。")
_register("SECRET_MASTER_PROTECTED", "应用主密钥受保护", "此密钥用于保护应用数据，不能通过普通轮换、吊销或配置覆盖来更换主密钥。",
          "保留原主密钥；需要更换时安排业务支持的数据重加密、校验和回退专项迁移。")
_register("SECRET_REVOKED", "固定密钥版本已吊销", "部署引用的密钥版本已吊销，平台禁止使用该版本启动、发布或恢复。",
          "在密钥管理中核对吊销记录，为普通运行密钥设置有效的新版本并提交新的部署。",
          "不要改回旧指针或绕过检查恢复已吊销版本；外部供应商凭证需另行撤销。")
_register("SECRET_REVOCATION_STATE", "吊销任务状态不允许此操作", "吊销任务当前状态或受理范围与请求不符，不能继续该操作。",
          "刷新密钥管理，核对原吊销任务及受影响版本；已经完成的任务无需重复执行。",
          "需人工处理时请管理员按原任务范围恢复清理，不要更改目标或把未核验状态视为完成。")
_register("SECRET_REVOCATION_FAILED", "密钥吊销尚未完成", "尚未确认所有使用该版本的服务和公开路由已停用，不能视为吊销完成。",
          "保持该版本的启动与恢复阻断，请管理员核对受影响实例、路由和原吊销记录。",
          "排除原因后按原任务范围继续清理与核验；外部供应商凭证需另行撤销。")
_register("VAULT_APP_UNREGISTERED", "应用尚未登记到加密密钥库", "当前应用尚未完成加密密钥库登记或迁移，暂不能访问其版本配置。",
          "请管理员核对应用归属与迁移状态，完成受控登记；不要重复创建应用或复制其他应用配置。")
_register("VAULT_NOT_INITIALIZED", "加密密钥库尚未就绪", "加密密钥库尚未完成显式初始化或受控迁移，当前操作已停止。",
          "请管理员核对初始化、迁移及恢复记录；已有数据时必须恢复原根密钥和原密钥记录，禁止直接重建。")
_register("VAULT_MAINTENANCE", "密钥库正在维护", "加密密钥库处于维护或迁移阶段，暂不接受新的部署和密钥变更。",
          "等待管理员完成维护并确认恢复服务后再提交，不要切回旧格式绕过维护状态。")
_register("VAULT_FORMAT_DOWNGRADE", "禁止降级密钥库格式", "当前状态已采用加密密钥库格式，平台拒绝降级为旧明文格式继续读写。",
          "使用支持当前密钥库格式的平台版本和配置恢复；保留原根密钥、状态与迁移记录。")
_register("VAULT_FORMAT_UNSUPPORTED", "密钥库格式不受支持", "平台无法识别密钥库格式或迁移阶段，已停止访问以保护现有记录。",
          "请管理员使用兼容平台版本核对格式和迁移记录；不要手工改写格式标记或重新初始化。")
_register("VAULT_IDENTITY_CONFLICT", "密钥库身份不一致", "密钥库绑定的企业、飞书应用或迁移身份与当前平台不一致。",
          "请管理员核对原身份与迁移记录，按显式身份迁移流程处理；不要按姓名或邮箱自动合并。")
_register("VAULT_KEY_UNAVAILABLE", "密钥库根密钥不可用", "密钥库所需的原根密钥版本缺失、损坏或访问权限不符合要求。",
          "请管理员从独立受限凭证存储或受保护备份恢复原根密钥及其权限。",
          "已有加密数据时禁止生成新根密钥替代原值；不要删除密文或改用应用主密钥、飞书凭证解密。")
_register("VAULT_TRANSACTION_REQUIRED", "密钥变更事务不完整", "密钥库变更未处于与授权和审计一致的受控事务中，平台已拒绝操作。",
          "请管理员检查平台调用路径和版本，修复后从受控入口重新提交；不要直接修改密钥库记录。")
_register("SECRET_SNAPSHOT_INVALID", "固定密钥快照不完整", "部署固定的应用、密钥身份或版本信息不完整，不能安全启动或恢复。",
          "请管理员核对原部署快照、原密钥版本及数据代际；保留原状态用于恢复。",
          "不能改用当前版本、生成新密钥或手工补写身份来绕过固定快照检查。")
_register("VAULT_MIGRATION_CONFLICT", "密钥库迁移信息冲突", "迁移清单、应用归属、原始文件或已有迁移记录不一致，迁移已停止。",
          "保留迁移现场，请管理员核对原清单、文件和任务记录；不要改写原值或跳过归属检查。")
_register("VAULT_RECOVERY_RECORD_REQUIRED", "缺少有效恢复记录", "密钥库操作需要完整的备份、根密钥恢复位置、责任人与保留期限记录。",
          "请管理员补齐恢复记录，并确认带时区的核验时间及晚于核验时间的保留截止时间，再重新预检。")
_register("VAULT_MIGRATION_BUSY", "尚有任务阻止密钥库迁移", "部署、下线、恢复或吊销清理尚未确认完成，暂不能迁移密钥库。",
          "先确认相关任务安全完成或按原任务处理恢复状态，再重新执行迁移预检。")
_register("VAULT_MIGRATION_REQUIRED", "现有状态需要显式迁移", "平台已有应用、历史任务或密钥记录，不能按空密钥库直接初始化。",
          "请管理员提供完整迁移清单，核对原应用、数据代际和密钥类型后执行受控预检与迁移。")
_register("UNSAFE_RUNTIME_ENV", "运行密钥临时目录不安全", "运行环境临时目录的路径、归属、权限或内存文件系统不符合要求，无法安全传递密钥。",
          "请管理员核对受控运行目录及内存文件系统，修复后重试；不要将密钥转存到普通临时文件绕过检查。")
_register("UNSAFE_WORKER_LOCK", "部署执行锁身份不安全", "部署执行锁的文件类型、归属或权限不符合要求，平台已停止执行。",
          "请管理员核对执行器及锁文件身份，确认没有并行任务后修复；不要删除正在使用的锁或绕过互斥检查。")
_register("APP_BUSY", "应用正在处理其他任务", "该应用存在排队、执行、恢复或旧版本清理任务，暂时不能下线。",
          "等待相关任务完成后刷新管理页面，再选择当前上线版本下线。")
_register("APP_OFFLINE_BUSY", "应用正在下线", "该应用有尚未完成的下线操作，暂时不能提交部署。",
          "在应用管理页面检查下线进度；需要恢复时先处理原下线任务，不要并行部署。")
_register("OFFLINE_RELEASE_CHANGED", "上线版本已变化", "页面中的版本已不是应用当前上线版本，本次未对新版本执行下线。",
          "刷新应用管理页面，核对当前应用及版本后重新操作。")
_register("OFFLINE_STATE", "下线状态不符合要求", "当前应用或下线任务状态不允许执行该操作。",
          "刷新管理页面并核对应用状态；已下线的应用无需重复下线。")
_register("OFFLINE_FAILED GATEWAY_DEACTIVATE_FAILED", "下线尚未完成", "尚未确认下线操作全部完成。如果选择删除数据，可能已有部分数据被清除；部署历史仍保留。",
          "查看管理页面的失败阶段，排查网关或运行资源状态。",
          "确认原因后使用原任务的重试下线按钮继续；不要删除持久卷或手工跳过资源确认。")
_register("OFFLINE_DATA_POLICY", "数据处理选项无效", "下线的数据处理方式不在允许范围内。",
          "刷新管理页面，选择保留数据或删除数据后重新确认。")
_register("OFFLINE_DELETE_CONFIRMATION", "删除确认文字不匹配", "尚未收到与目标应用一致的删除确认文字。",
          "核对目标应用，准确输入弹窗显示的确认文字；不需要清除数据时选择保留数据。")
_register("OFFLINE_DATA_CONFLICT", "无法确认数据清除归属", "该应用的数据或配置归属存在冲突，平台拒绝继续清除。",
          "请运维核对原应用、持久存储和秘密配置的归属，不能清除其他应用的资源。",
          "刷新管理页面确认是否已有下线操作；已有操作须按原任务及原策略处理。")
_register("SECRET_DELETE_CONFLICT", "应用配置清除尚未确认", "待清除的密码或密钥配置与已核验记录不一致，可能已完成部分清理。",
          "请运维核对原任务的配置归属和受保护文件状态；不要覆盖配置或扩大删除范围。",
          "确认原因后按原任务重试，保留部署历史用于核验。")
_register("STORAGE_IN_USE", "持久存储仍被使用", "目标存储仍有运行资源引用，平台未确认可以安全删除。",
          "请运维核对引用该存储的容器归属及停止状态，不要强行删除其他应用的容器。",
          "确认引用已按原任务解除后，再重试原下线操作。")
_register("STORAGE_DELETE_FAILED OFFLINE_DATA_DELETE_FAILED", "数据清除尚未完成", "业务数据或应用配置的清除未全部确认，可能已有部分内容被删除。",
          "请运维核对原下线任务及剩余资源；不要把当前状态视为数据完整保留或全部清除。",
          "排除原因后使用原任务、原删除策略继续，不能改用新任务绕过核验。")
_register("INVALID_REPOSITORY", "仓库地址不符合要求", "仓库地址必须是允许访问的 HTTPS Git 仓库地址。",
          "使用 https://github.com/所有者/仓库 或平台允许的 GitLab 仓库地址。",
          "地址不要包含令牌、账号密码、查询参数、端口或分支路径；私有仓库凭证由运维配置。")
_register("INVALID_REF", "代码版本格式错误", "分支、标签或提交编号格式不符合要求。",
          "填写实际存在的分支名、标签名或完整提交哈希；不要填写 Git 版本表达式或命令。")
_register("GIT_FAILED", "获取代码失败", "Git 未能完成仓库操作，可能与地址、版本、访问权限或网络有关。",
          "核对仓库地址和分支、标签或提交是否存在。", "私有仓库请运维确认读取权限和凭证配置。",
          "请运维检查服务器到代码托管服务的网络；确认原因后再部署。")
_register("GIT_TIMEOUT", "获取代码超时", "Git 操作超过平台配置的等待时间。",
          "请运维检查服务器到代码托管服务的网络与仓库可访问性。",
          "确认仓库规模和平台超时配置，排除网络问题后重新部署。")
_register("GIT_OUTPUT_LIMIT", "仓库操作输出超限", "Git 操作产生的输出超过平台允许范围。",
          "请运维检查仓库操作和凭证助手的异常输出，再确认是否可以重试。")
_register("SOURCE_TOO_LARGE", "仓库规模超限", "仓库体积或文件数量超过平台限制。",
          "减少仓库中不参与构建的大文件和生成文件，并提交精简后的版本。",
          "请运维核对仓库大小配额；当前导出文件数量上限为 100000。")
_register("UNSAFE_SOURCE", "代码文件不受支持", "代码导出包含不安全路径、符号链接或不支持的文件类型。",
          "将部署所需文件保存为仓库中的普通文件和目录，去除符号链接与越界路径。",
          "若仓库没有这些内容，请运维检查部署状态目录与导出过程。")
_register("INVALID_SNAPSHOT PIN_CONFLICT", "固定代码版本异常", "部署的固定提交信息无效或发生变化。",
          "保留当前部署记录，请运维核对固定提交和状态数据，不要直接改写运行中的记录。")
_register("INVALID_JOB RESOURCE_ID_INVALID JOB_STATE", "部署状态异常", "部署编号或资源状态不符合当前操作要求。",
          "刷新部署记录，确认操作对应的部署与应用；请运维检查状态后再提交。")
_register("INVALID_MANIFEST", "部署配置校验失败", "awesome.yaml 不符合平台部署配置要求。",
          "根据问题位置检查 awesome.yaml，并参考平台配置 schema 和示例。",
          "将修改提交到仓库，再部署包含该修改的分支或提交。")
_register("SUBDOMAIN_MISMATCH", "域名前缀不一致", "提交的域名前缀与 awesome.yaml 中 app.subdomain 不一致。",
          "将表单域名前缀与 awesome.yaml 的 app.subdomain 设置为相同值。",
          "提交配置修改后重新部署；更新已有应用时继续使用该应用的域名前缀。")
_register("INVALID_BUILD_PATH UNSAFE_BUILD_PATH", "构建路径不符合要求", "构建目录或 Dockerfile 缺失、越界或包含符号链接。",
          "确认 build.context 是仓库根目录下的相对目录，build.dockerfile 相对于该构建目录。",
          "将 Dockerfile 与构建文件提交到当前版本，去除绝对路径、上级目录引用和符号链接。")
_register("DOCKERFILE_MISSING", "Dockerfile 不符合要求", "没有找到大小符合限制的普通 Dockerfile 文件。",
          "检查 build.context 和 build.dockerfile，并提交实际 Dockerfile 文件。",
          "确认文件大小符合平台限制，且不是目录或符号链接。")
_register("UNSUPPORTED_DOCKERFILE", "Dockerfile 使用了不支持的语法", "Dockerfile 使用了当前构建器不支持的指令或格式。",
          "参考平台 Dockerfile 支持范围，使用明确的基础镜像和受支持指令。",
          "检查自定义前端、续行或 heredoc、RUN 附加标志、动态基础镜像和 COPY 阶段引用。")
_register("UNSUPPORTED_BUILD_ARGUMENT", "构建参数不受支持", "构建参数格式不正确，或试图覆盖保留的 BuildKit 配置。",
          "使用普通字符串构建参数，移除 BUILDKIT_ 和 BUILDX_ 等平台保留参数。")
_register("UNSUPPORTED_CAPABILITY", "当前平台不支持该能力", "配置或镜像使用了当前平台尚未开放的能力。",
          "平台支持 Web 服务及 services.<服务名>.storage 声明的单服务 /data 持久卷，请核对当前平台能力与配置 schema。",
          "去除子模块、任意主机挂载、镜像 VOLUME/ONBUILD 或其他不支持能力；需要持久存储时保留受控 storage 声明，不要关闭隔离限制。")
_register("UNSUPPORTED_ENVIRONMENT", "环境变量格式不受支持", "当前平台只支持不含换行和 NUL 的环境变量字符串。",
          "将环境变量值改为单行字符串；敏感信息通过平台秘密配置入口管理。")
_register("IMAGE_INVALID", "镜像信息无效", "镜像引用格式不正确，或无法确认镜像信息。",
          "检查镜像仓库、名称和标签，确认平台能够访问对应镜像。")
_register("IMAGE_UNPINNED BUILD_UNPINNED", "镜像版本未固定", "构建或镜像仓库未返回可固定的镜像标识。",
          "请运维检查镜像获取与构建结果，确认返回不可变的镜像摘要或镜像 ID。")
_register("HEALTH_FAILED", "服务健康检查失败", "候选服务未正常运行，或未在限定时间内通过健康检查。",
          "确认服务启动成功、监听 0.0.0.0，且实际端口与 services 中的 port 一致。",
          "检查 healthcheck 路径或命令、启动等待时间，以及非 root 和只读文件系统要求。",
          "修复后提交新版本；若此前已有成功版本，请查看应用当前状态确认其仍在服务。")
_register("DEPENDENCY_CYCLE", "服务依赖无法启动", "服务依赖关系存在循环，无法确定启动顺序。",
          "调整 dependsOn，去除循环依赖，并确认所有依赖服务都存在。")
_register("RESOURCE_QUOTA", "部署资源配额不足", "当前运行版本与候选版本合计超过 CPU 或内存配额。",
          "降低候选服务的 resources 配置，或请运维核对平台总配额。",
          "更新期间旧版与候选版会同时占用资源；不要直接删除仍在服务的旧版。")
_register("DISK_CAPACITY", "服务器可用存储不足", "部署所需的可用磁盘空间或 inode 不足。",
          "请运维检查独立 Docker 存储的空间和 inode，并按归属清理已确认无用的资源。")
_register("PORTS_EXHAUSTED", "可用端口不足", "平台的服务端口池已无可用端口。",
          "请运维核对端口预留记录和已完成清理的部署，再释放确认无用的端口。")
_register("STORAGE_UNSAFE", "存储隔离检查未通过", "Docker 存储不符合独立文件系统或容量边界要求。",
          "请运维核对 DockerRootDir、独立挂载和平台容量配置，保持存储隔离要求。")
_register("STORAGE_MISSING", "持久卷缺失", "部署记录中的持久卷已不存在，平台拒绝用空卷替代已有数据。",
          "暂停该应用部署，请运维核对受控卷及离机备份，按数据恢复方案处理。",
          "不要删除 storage 声明、改卷名或手工改写卷身份记录来绕过检查。")
_register("STORAGE_IDENTITY STORAGE_CONFLICT", "持久卷身份不一致", "卷的归属、实例标识、创建时间或驱动配置与受控存储记录不一致。",
          "请运维核对应用、服务与卷的所有权、实例标识、创建时间和 local 驱动配置，保留现有数据。",
          "不要清空或重新创建同名卷，也不要手工修改部署记录来绕过身份检查。")
_register("STORAGE_MOUNT_CHANGED", "持久存储挂载不符合要求", "容器的实际挂载与固定的受控 /data 卷不一致。",
          "请运维核对容器身份、卷名称、/data 挂载点与读写权限，移除不受支持的额外挂载。",
          "保持平台隔离规则；在确认数据和容器归属前不要启动替代实例。")
_register("STORAGE_HANDOFF_IDENTITY STORAGE_HANDOFF_STATE STORAGE_RECOVERY_REQUIRED", "持久存储交接需要确认", "平台无法确认卷交接期间的容器身份或停止状态，已阻断旧版本的自动恢复以避免双写。",
          "先请运维确认候选容器已停止并完成清理，再恢复旧版本及公开路由。",
          "保留卷、备份与部署记录，不要同时启动两个使用该卷的版本；应用回滚不会撤销数据写入。")
_register("INTERRUPTED_STORAGE_HANDOFF", "中断的存储交接已恢复", "中断的持久存储更新已停止并清理候选，随后完成旧版本恢复。",
          "核对当前图片或其他数据仍可访问，并查看原部署失败原因后提交新版本。",
          "旧应用版本恢复不会撤销候选已经写入的数据；更新应保持数据格式兼容。")
_register("HOST_UNSUPPORTED HOST_UNAVAILABLE HOST_UNSAFE", "运行环境检查未通过", "服务器未满足受保护的 rootless Docker 运行环境要求。",
          "请运维检查 Linux、专用 rootless Docker socket、文件归属、cgroup v2 与 seccomp。",
          "不要将服务改为 root 或关闭隔离检查来绕过此错误。")
_register("BUILDER_UNSAFE", "构建器检查未通过", "专用构建器的身份、版本或资源隔离不符合要求。",
          "请运维检查固定版本的 rootless BuildKit、非 root 用户、CPU/内存限制和进程沙箱。")
_register("COMMAND_FAILED", "部署步骤执行失败", "部署命令未成功完成，详细原始输出不会直接展示。",
          "按失败阶段检查构建、启动或网关配置；请运维查看受保护的详细日志定位原因。")
_register("COMMAND_TIMEOUT", "部署步骤执行超时", "部署命令超过该步骤配置的等待时间。",
          "检查该阶段的构建或启动耗时、网络和资源情况，再由运维核对超时配置。")
_register("COMMAND_UNAVAILABLE COMMAND_INVALID", "部署执行器不可用", "部署执行器所需程序不可用或命令参数格式异常。",
          "请运维检查平台依赖程序、执行权限与部署配置。")
_register("GATEWAY_FAILED GATEWAY_PROTOCOL", "HTTPS 网关操作失败", "网关未能确认 HTTPS 证书、路由或公开访问检查结果。",
          "请运维按失败阶段检查域名解析、证书签发、网关配置及到候选服务的连接。",
          "核对部署和回滚状态后再尝试，避免直接覆盖正在使用的路由。")
_register("PORT_FORWARD_INVALID PORT_FORWARD_INVALID_RESPONSE PORT_FORWARD_UNSUPPORTED", "端口转发配置不兼容", "端口转发配置或 RootlessKit 响应不符合平台要求。",
          "请运维检查 RootlessKit builtin TCP4 能力、响应格式与已预留的 localhost 端口。")
_register("PORT_FORWARD_UNSAFE PORT_FORWARD_UNAVAILABLE", "端口转发服务不可用", "无法通过受保护的本地接口确认端口转发状态。",
          "请运维检查 RootlessKit socket 的路径、归属、权限与服务状态。")
_register("PORT_FORWARD_CONFLICT PORT_FORWARD_IDENTITY PORT_FORWARD_UNCONFIRMED", "端口转发状态需要核对", "转发归属或目标与部署记录不一致，或上次操作尚未确认。",
          "请运维核对完整转发目标、容器身份和端口预留；确认前保留相关资源。",
          "不要按临时转发编号盲目删除映射或重复创建。")
_register("EGRESS_UNSAFE", "运行外连安全条件未满足", "运行环境未满足受控公网外连所需的安全条件。",
          "请运维检查专用 Docker 运行环境；此模式要求关闭 Docker live restore 后重新核验。",
          "不要关闭隔离检查或绕过受控网络规则直接启动服务。")
_register("EGRESS_NETWORK_IDENTITY", "运行网络身份不匹配", "容器的网络归属、解析配置或出口与本次部署固定的身份不一致。",
          "请运维核对部署记录与现有网络连接，只处理已确认归属的资源。",
          "不要关闭身份核验、改写固定记录或替换未知归属的网络。")
_register("EGRESS_POLICY_INVALID", "运行外连策略无效", "平台配置或本次部署固定的运行外连策略未通过校验。",
          "请运维检查运行网络模式、受控解析与阻断地址配置，保留部署的固定输入供核验。",
          "不要关闭隔离检查或手工修改已有任务的固定网络策略。")
_register("EGRESS_POLICY_CHANGED", "固定运行网络策略不匹配", "本次部署固定的网络策略与当前平台要求不一致，平台已停止继续执行。",
          "请运维核对原任务策略与平台配置；已有任务不会自动升级，确认后提交新的部署。",
          "不要关闭隔离检查或改写旧部署的固定策略来绕过此错误。")
_register("EGRESS_NAMESPACE_UNSAFE", "运行网络身份检查未通过", "无法确认目标网络命名空间及运行进程的受保护身份。",
          "请运维核对运行进程、用户映射与网络命名空间归属，确认未指向宿主网络。",
          "不要关闭身份检查或改用宿主网络、特权容器来绕过限制。")
_register("EGRESS_UNAVAILABLE", "运行外连执行器不可用", "无法完成受控运行网络规则的安装或核验。",
          "请运维检查受保护的网络执行器、所需工具及运行状态，再确认是否可以重试。",
          "不要关闭隔离规则或自行放开全部出站流量。")
_register("EGRESS_GUARD_MISSING", "运行外连规则缺失", "未找到本次运行环境所需的受控出站规则，平台拒绝继续执行。",
          "请运维核对运行网络规则及其安装状态，恢复规则后重新核验。",
          "不要关闭规则检查或直接启动没有受控网络规则的容器。")
_register("EGRESS_GUARD_CHANGED", "运行外连规则发生变化", "运行网络规则与固定要求不一致，或安装后的核验未通过。",
          "请运维核对规则变更与运行环境身份，在确认保护完整前保留任务记录。",
          "不要关闭核验或手工改写固定部署策略来跳过此错误。")
_register("RESOURCE_CONFLICT", "运行资源归属冲突", "已有容器或网络与本次部署的归属及隔离要求不一致。",
          "请运维核对容器、网络标签和部署记录；只处理已确认归属的资源。")
_register("ACTIVE_CONTAINER_CHANGED ACTIVE_CONTAINER_MISSING ACTIVE_NETWORK_CHANGED", "当前版本运行资源异常", "当前版本的容器或网络缺失，或不再匹配已固定的身份与隔离要求。",
          "请运维检查当前运行版本的容器与网络，不要直接改写平台记录或替换未知归属的资源。")
_register("ACTIVE_RECOVERY_FAILED ACTIVE_RECOVERY_TIMEOUT", "当前版本恢复未完成", "当前运行版本未能完成恢复或健康确认。",
          "查看恢复阶段，检查容器、网络、转发与健康检查；平台会按恢复策略退避重试。",
          "若持续失败，请运维处理后再更新该应用。")
_register("ACTIVE_RECOVERY_BACKOFF", "正在等待恢复重试", "当前版本的恢复操作正在等待下一次重试窗口。",
          "等待恢复重试并查看应用状态；持续失败时请运维检查，不要反复提交同一操作。")
_register("ACTIVE_RECOVERY_STATE ACTIVE_RELEASE", "当前版本状态不允许此操作", "当前运行版本已经变化，或此操作会影响正在服务的版本。",
          "刷新应用与部署状态，请运维确认当前版本后继续。")
_register("RECOVERY_REQUIRED ROLLBACK_FAILED CLEANUP_FAILED CLEANUP_UNSAFE", "需要运维确认恢复", "回滚或资源清理尚未确认，相关资源可能仍被保留。",
          "先请运维确认网关、当前版本及候选资源状态，再完成恢复或清理。",
          "恢复确认之前不要重复部署该应用或手动释放未确认的资源。")
_register("INTERRUPTED_CLEANUP", "中断部署已完成清理", "上次中断的候选版本清理已完成。",
          "查看原部署失败原因；需要重新部署时提交新的请求。")
_register("INTERRUPTED_PUBLISH", "中断发布已回滚", "上次中断的发布已回滚。",
          "核对当前应用访问状态和原失败原因；修复后提交新的部署。")
_register("DOMAIN_CONFLICT", "域名前缀已被占用", "该域名前缀已经属于另一个应用。",
          "保留当前应用选择，填写其他空闲前缀，并确保所选代码版本中的 awesome.yaml 使用相同前缀。")
_register("APP_IDENTITY_CONFLICT", "应用身份不一致", "仓库地址或域名前缀与所选应用不一致。",
          "已有应用继续使用原仓库；仍绑定域名的应用使用当前前缀，已释放域名的应用可选择空闲前缀。",
          "部署其他仓库时选择创建新应用；平台不会替你修改业务代码。")
_register("SECRET_SCOPE_CONFLICT", "秘密配置属于其他应用", "该仓库与域名对应的秘密配置空间已有其他应用的部署记录，不能自动继承。",
          "需要保留原应用的数据和配置时，选择原应用重新部署。",
          "创建新应用时，使用该仓库未被其他应用使用过的空闲前缀，再通过控制台补充所需配置。")
_register("STATE_MIGRATION_FAILED", "平台状态升级未完成", "数据库结构或关联校验未通过，平台没有继续执行部署。",
          "请运维核对状态备份和数据库完整性，完成平台升级后再重试。")
_register("INVALID_SUBDOMAIN", "域名前缀格式错误", "域名前缀须为 1–7 位小写字母、数字或中划线，且不能使用保留或禁止名称。",
          "使用字母或数字开头及结尾的合规前缀，并与 awesome.yaml 的 app.subdomain 保持一致。")
_register("INVALID_APP_ID", "应用编号格式错误", "应用编号格式不正确。",
          "从应用列表重新选择已有应用，或选择创建新应用。")
_register("INVALID_IDEMPOTENCY_KEY IDEMPOTENCY_CONFLICT", "请求标识无效或冲突", "该请求标识格式不正确，或已经用于不同的部署内容。",
          "修改部署内容后使用新的请求标识；网页表单会自动生成标识。")
_register("NOT_FOUND", "记录不存在", "未找到指定的部署或应用记录。",
          "刷新列表并重新选择部署或应用，确认编号正确。")
_register("REQUEST_TOO_LARGE", "请求内容过大", "本次 API 请求超过 128 KiB 限制。",
          "缩减请求内容后重试；此限制与仓库中 awesome.yaml 的 256 KiB 文件限制不同。")
_register("INVALID_LENGTH INVALID_REQUEST", "请求格式不正确", "请求字段类型、必填内容或长度不符合接口要求。",
          "检查必填字段和字段类型，移除接口不支持的额外字段后重新提交。")
_register("INVALID_CONFIG", "运维配置无效", "平台运维配置包含无效字段或配置值。",
          "请运维参照配置示例核对字段、路径、域名、资源和网络限制。")
_register("AUTH_NOT_INITIALIZED", "运维账号尚未初始化", "平台尚未配置可用的运维登录账号。",
          "请运维通过 init 命令和安全输入流程初始化账号。")
_register("INVALID_CREDENTIALS PASSWORD_MISMATCH", "运维凭证格式错误", "账号或密码不符合要求，或两次密码输入不一致。",
          "按安全输入提示重新填写；用户名使用合规 ASCII 字符，密码长度为 12–1024 位。")
_register("CREDENTIALS_EXIST", "运维凭证已经存在", "现有运维凭证不能通过初始化流程覆盖。",
          "需要修改凭证时，请使用单独的安全轮换流程。")
_register("INVALID_SECRET INVALID_SECRET_NAME", "秘密配置不符合要求", "秘密配置名称或内容格式不符合平台要求。",
          "通过安全配置入口检查名称与内容；请填写 1–16384 字节的单行内容，不能含换行或 NUL。")
_register("SECRET_NOT_FOUND", "缺少可用的秘密配置", "部署引用的秘密配置不存在或其固定版本无法读取。",
          "在本次部署的配置表单中补充缺少的 secretRefs 引用，保存后继续部署。",
          "已有值不会显示或覆盖；状态异常时请运维核验原配置，避免替换应用仍需要的密钥。")
_register("SECRET_CONFIGURATION_NOT_AVAILABLE", "无法补充本次部署配置", "该任务没有可用的固定配置快照，或不是缺少配置导致的失败。",
          "重新选择因缺少秘密配置而失败的部署；若快照异常，请运维核验任务记录。")
_register("SECRET_CONFIGURATION_BUSY", "配置暂时不能保存", "相关部署、下线或恢复工作尚未完成，或执行器正在使用配置。",
          "等待相关任务完成后刷新配置状态，再保存缺少的值。")
_register("SECRET_ALREADY_CONFIGURED", "配置已经存在", "已有配置不能通过补齐入口覆盖，当前请求未替换已有值。",
          "刷新配置状态，只填写仍然缺少的配置；需要轮换已有密钥时请联系运维。")
_register("SECRET_MANAGED_INPUT", "配置由平台管理", "该配置由平台生成并私密保存，不接受浏览器提交或替换其值。",
          "刷新页面，只填写登录密码等需要手动设置的项目；已有平台密钥会继续复用。")
_register("SECRET_UNAVAILABLE", "配置状态不可用", "无法安全读取配置状态或保存缺少的值，平台没有自动修复或替换已有配置。",
          "刷新状态检查哪些值已保存；若仍有异常，请运维核验配置目录、权限和原有版本。")
_register("UNSAFE_SECRET_STORE UNSAFE_STATE_DIRECTORY", "状态目录保护检查失败", "平台状态或秘密配置目录的权限、路径不符合保护要求。",
          "请运维检查目录归属与权限，并去除符号链接；平台状态目录应仅允许运维服务账号访问。")
_register("AI_NOT_CONFIGURED", "AI 分析尚未配置", "平台尚未启用可用的 AI 分析接口。",
          "请运维配置 AI 接口地址、模型和服务端密钥；当前仍可使用本地修改建议。")
_register("AI_KEY_UNAVAILABLE", "AI 密钥不可用", "无法安全读取 AI 接口密钥。",
          "请运维通过安全入口配置密钥，并检查其读取权限；不要将密钥放入仓库或网页表单。")
_register("AI_BUSY AI_RATE_LIMITED", "AI 分析暂时繁忙", "AI 分析正在执行，或调用频率已达到限制。",
          "稍后再试，并先参考本地修改建议。")
_register("AI_ANALYSIS_EXPIRED", "AI 分析结果已过期", "本次 AI 分析结果已过期，或服务已重启。",
          "点击“重试 AI 分析”重新发起；也可以先按上方内置建议检查配置。")
_register("AI_TIMEOUT", "AI 分析超时", "AI 接口未在规定时间内完成响应。",
          "稍后再试；若持续超时，请运维检查接口连通性和响应速度。")
_register("AI_UNAVAILABLE", "AI 接口暂时不可用", "无法完成本次 AI 接口调用。",
          "先参考本地修改建议，请运维检查接口地址、网络和服务状态后重试。")
_register("AI_AUTH_FAILED", "AI 接口认证失败", "AI 服务拒绝了当前配置的调用凭证。",
          "请运维检查服务端密钥、模型权限和接口配置。")
_register("AI_RESPONSE_INVALID", "AI 返回内容不可用", "AI 接口返回的内容不符合平台要求。",
          "先参考本地修改建议，请运维检查模型与接口响应格式。")
_register("DIAGNOSIS_NOT_AVAILABLE", "当前部署无需失败分析", "只有已失败或需要恢复处理的部署才能进行失败分析。",
          "等待运行中的部署结束，或从历史列表选择失败的部署。")

_register("STORAGE_START_UNCONFIRMED", "存储写入者启动状态未确认",
          "平台无法确认容器启动结果，已保留写入者登记以防止错误释放存储。",
          "核对容器的完整身份和实际运行状态，确认停止后再执行清理或恢复。")
_register("QUOTA_LEDGER_CORRUPT QUOTA_LEDGER_MISSING", "存储权威账本需要恢复",
          "存储账本缺失或完整性检查失败，平台已拒绝重新分配额度。",
          "保留现场并从一致备份核对恢复；不要删除初始化标记、清空账本或重新编号。")
_register("QUOTA_POOL_BLOCKED", "存储池处于持续阻断状态",
          "之前确认的池级异常尚未由管理员解除，新的存储写入入口已关闭。",
          "修复并核验原始异常后显式解除全池阻断，再逐项检查存储和写入者状态。")
_register("QUOTA_NOT_ENABLED QUOTA_BACKEND_REQUIRED", "存储硬配额尚未启用",
          "当前运行环境未启用并验证 XFS 存储硬配额，不能以普通卷替代。",
          "请运维核对存储后端、受限执行器和目标机验收结果，不要移除额度字段绕过要求。")
_register("QUOTA_MIGRATION_REQUIRED STORAGE_MIGRATION_REQUIRED", "持久数据需要迁移",
          "现有数据仍使用旧存储后端，尚未迁入受硬配额保护的目录。",
          "请运维按停写、备份、复制核验和重建容器流程迁移，保留原始数据及恢复记录。")
_register("QUOTA_UNAVAILABLE QUOTA_PROTOCOL QUOTA_RESPONSE_LIMIT", "配额查询暂不可用",
          "无法完成可信的配额查询；这不代表内核配额已经关闭。",
          "请运维检查存储执行器和查询结果；未知状态禁止新建、扩容和启动，但不能直接当作已确认失效。")
_register("QUOTA_BUDGET_EXCEEDED QUOTA_POOL_RESERVE QUOTA_PROJECT_EXHAUSTED", "存储额度不足",
          "服务、应用或数据池的可分配额度或保留空间不足。",
          "请运维核对字节和 inode 两维预算；保留数据与未完成操作仍占额度，不要按当前低使用量重复分配。")
_register("QUOTA_PENDING QUOTA_DELETE_PENDING", "存储操作尚未确认",
          "存储创建、扩容或删除仍需对账，相关额度继续保留。",
          "请运维用原操作标识重试并核对实际配额与剩余数据，不要因超时释放额度或重新分配身份。")
_register("QUOTA_EXPANSION_REQUIRED QUOTA_SHRINK_FORBIDDEN", "存储额度需要显式调整",
          "普通部署不修改有效额度；扩容需要独立操作，自动缩容被禁止。",
          "请运维先核对当前额度及版本号，再执行显式扩容；业务版本回滚沿用当前额度。")
_register("QUOTA_BLOCKED QUOTA_NOT_ENFORCED QUOTA_DIRECTORY_MISMATCH QUOTA_LIMIT_MISMATCH "
          "QUOTA_MAPPING_MISMATCH QUOTA_POOL_MISMATCH QUOTA_PROJECT_MISMATCH",
          "存储配额校验未通过", "数据池、目录、映射或双硬限与受控记录不符，相关写入应保持隔离。",
          "请运维核对并停止受影响写入者，修复后重新核验并显式解除阻断；不要关闭保护或创建空目录替代。")
_register("QUOTA_REFERENCE_MISMATCH QUOTA_REFERENCE_MISSING QUOTA_IDENTITY_INVALID",
          "持久存储身份无法确认", "配额账本中的稳定身份缺失或与部署记录不一致。",
          "请运维保留数据和账本，核对存储标识与代次；不要通过重建同名目录恢复。")
_register("QUOTA_WRITERS_ACTIVE QUOTA_WRITER_MISMATCH", "持久数据仍有写入者或归属不明",
          "无法确认所有相关写入者已停止或属于本次存储操作。",
          "请运维核对精确容器身份并确认停止后重试；未确认前不删除数据、不释放额度。")
_register("QUOTA_OPERATION_CONFLICT QUOTA_REVISION_CONFLICT", "存储操作版本冲突",
          "同一操作标识或额度版本对应了不同请求。",
          "查询当前操作结果与有效版本，复用原请求重试；需要新调整时使用新的操作标识及当前版本。")
_register("QUOTA_CALLER_FORBIDDEN QUOTA_CONFIG_INVALID QUOTA_CONFIG_UNTRUSTED",
          "存储执行器配置不可信", "调用身份或受限存储执行器配置不符合要求。",
          "请运维检查固定安装路径、文件所有者、权限和映射配置，不要扩大 sudo 权限绕过检查。")
_register("QUOTA_INVALID_REQUEST QUOTA_LIMIT_INVALID", "存储配额请求无效",
          "请求字段、双硬限或操作标识不符合受控协议。",
          "检查清单版本和正整数额度；不要提交宿主路径、项目编号、无限额或任意挂载参数。")

ERROR_CODES = frozenset(_CATALOG)


_HEALTH_REASONS = {
    "HEALTH_CONTAINER_EXITED": (
        "服务容器未运行", "健康检查时，候选服务容器未运行，可能已退出或尚未成功启动。",
        ("请运维核对受保护的启动日志，检查入口文件读取权限、启动命令及必需配置。",
         "确认非 root 用户能读取程序文件并写入声明的持久目录；不要改为 root 或关闭只读隔离。")),
    "HEALTH_OOM_KILLED": (
        "服务因内存不足被终止", "健康检查时，容器已停止，运行环境报告进程因内存不足被终止。",
        ("检查应用启动时的内存使用，并在平台配额内核对 services 中的 memoryMiB。",
         "降低应用内存占用或由运维确认可用配额后调整配置，不要关闭资源限制。")),
    "HEALTH_HTTP_STATUS": (
        "健康检查返回非成功状态", "健康检查收到 HTTP 响应，但状态码不在平台接受的 200–399 范围内。",
        ("确认 healthcheck.path 对应可访问的健康接口，并检查该接口所需依赖是否就绪。",
         "请运维核对健康接口返回状态；响应正文及业务日志不会在这里展示。")),
    "HEALTH_CONNECTION_FAILED": (
        "无法连接服务健康接口", "健康检查未能建立或保持到候选服务的连接。",
        ("确认服务已启动、监听 0.0.0.0，且实际端口与 services 中的 port 一致。",
         "请运维核对候选服务与受控端口转发状态，不要改用宿主网络或关闭隔离。")),
    "HEALTH_TIMEOUT": (
        "健康检查超时", "健康检查执行超时，或开始检查前已耗尽总等待时间。",
        ("核对服务启动耗时、健康接口或检查命令是否阻塞，以及依赖服务是否就绪。",
         "确认原因后再调整 healthcheck 的启动宽限和超时；不要仅扩大超时掩盖启动失败。")),
    "HEALTH_EXEC_FAILED": (
        "健康检查命令未成功", "容器内的健康检查命令未成功执行。",
        ("检查 healthcheck.command 的程序是否存在、非 root 用户是否可执行，以及返回码是否为零。",
         "命令必须在既有容器隔离条件下运行；详细输出请运维在受保护环境核对。")),
    "HEALTH_CHECK_ERROR": (
        "健康检查器无法确认状态", "健康检查器未能可靠确认候选容器或探针结果。",
        ("请运维核对 Docker 检查器、容器归属、探针协议和平台运行状态。",
         "当前证据不足以判断应用退出原因；不要关闭身份核验或直接删除未知归属资源。")),
}


_MANIFEST_REASONS = {
    "MANIFEST_MISSING": (
        "缺少部署配置文件", "仓库根目录未找到 awesome.yaml 普通文件。",
        ("在仓库根目录新增 awesome.yaml，并按实际服务填写构建方式、端口、健康检查和路由。",
         "文件名必须准确为 awesome.yaml，且不能是目录；将文件提交到本次部署使用的分支或提交。")),
    "MANIFEST_TOO_LARGE": (
        "部署配置文件过大", "awesome.yaml 超过 256 KiB 文件大小限制。",
        ("精简 awesome.yaml，使 UTF-8 编码后的文件大小不超过 256 KiB。",
         "不要在部署配置中嵌入大段文件或二进制内容；提交精简后的版本再部署。")),
    "MANIFEST_ENCODING": (
        "部署配置编码不正确", "awesome.yaml 不是有效的 UTF-8 文本。",
        ("将 awesome.yaml 转存为 UTF-8 文本并确认 YAML 内容正确。", "提交编码修正后的文件再部署。")),
}

_MANIFEST_DETAILS = {
    "Repository must contain an awesome.yaml within its size limit.": (
        "部署配置文件缺失或超限", "历史记录表明：根目录的 awesome.yaml 可能缺失或超过 256 KiB，无法进一步区分。",
        ("检查此次固定提交的仓库根目录中是否存在 awesome.yaml 普通文件。",
         "确认文件大小不超过 256 KiB，修复后提交新版本再部署。")),
    "Repository root must contain an awesome.yaml file.": _MANIFEST_REASONS["MANIFEST_MISSING"],
    "Manifest exceeds the 256 KiB size limit.": _MANIFEST_REASONS["MANIFEST_TOO_LARGE"],
    "awesome.yaml must contain UTF-8 text.": _MANIFEST_REASONS["MANIFEST_ENCODING"],
}


def _manifest_detail(english, message, step):
    _MANIFEST_DETAILS[english] = ("部署配置校验失败", message, (step, "提交修改后的 awesome.yaml 再部署。"))


for _english, _message, _step in (
    ("All mapping keys must be strings.", "YAML 字段名必须是字符串。", "将数字、布尔值等字段名改为字符串。"),
    ("Duplicate mapping key.", "YAML 中存在重复字段。", "删除同一层级的重复字段，保留唯一且正确的值。"),
    ("Manifest contains too many nodes.", "配置内容过于复杂，超过节点数量限制。", "精简配置结构与条目数量。"),
    ("YAML aliases, anchors and explicit tags are not supported.", "不支持 YAML 别名、锚点或显式类型标签。",
     "将引用展开为普通字段，移除锚点、别名和类型标签。"),
    ("Manifest nesting exceeds the depth limit.", "配置嵌套超过 32 层限制。", "减少 YAML 嵌套层级，按平台 schema 调整结构。"),
    ("Manifest is not valid supported YAML.", "awesome.yaml 不是平台支持的有效 YAML。", "检查缩进、引号、冒号及字段结构。"),
    ("Manifest root must be a mapping.", "awesome.yaml 的最外层必须是键值对象。",
     "在顶层填写 schemaVersion、app、services 和 routes，不能使用单一字符串或数组作为根节点。"),
    ("Build paths must be safe repository-relative paths.", "构建路径必须是安全的仓库相对路径。",
     "使用相对路径，移除绝对路径、反斜杠和控制字符。"),
    ("Build paths cannot contain traversal or empty components.", "构建路径包含上级目录引用或空路径段。",
     "移除 ..、重复斜杠和 .git 路径，确保路径位于批准的构建目录内。"),
    ("Dockerfile must identify a file.", "Dockerfile 路径必须指向文件。", "填写构建目录内实际 Dockerfile 文件的相对路径。"),
    ("Subdomain is reserved or contains a prohibited word.", "域名前缀包含保留名称或禁用词。", "更换合规且未被占用的域名前缀。"),
    ("CPU allocation exceeds the configured limits.", "CPU 配额不在平台允许范围内。", "按平台 CPU 配额范围调整 resources.cpu。"),
    ("Memory allocation exceeds the configured limits.", "内存配额不在平台允许范围内。",
     "按平台内存配额范围调整 resources.memoryMiB，使用整数 MiB。"),
    ("Healthcheck timeout cannot exceed its interval.", "健康检查超时时间不能大于检查间隔。",
     "将 timeoutSeconds 设置为不大于 intervalSeconds 的值。"),
    ("An environment name cannot also be a secret reference.", "同一环境变量同时出现在 env 与 secretRefs 中。",
     "为同一变量选择一种来源，敏感值使用 secretRefs。"),
    ("workingDir cannot contain traversal.", "工作目录不能包含上级目录引用。", "将 workingDir 改为不含 .. 的合规绝对路径。"),
    ("Dependency references an unknown service.", "服务依赖引用了不存在的服务。", "确保 dependsOn 引用的名称在 services 中定义。"),
    ("Service dependency graph contains a cycle.", "服务依赖关系存在循环。", "调整 dependsOn，去除直接或间接循环依赖。"),
    ("Route path must be a literal canonical URL path.", "路由路径不是规范的 URL 路径。",
     "使用以 / 开头的固定路径，移除重复斜杠、. 和 .. 路径段。"),
    ("Duplicate route path and match mode.", "路由路径与匹配方式重复。", "删除重复路由，保证相同路径和 match 组合唯一。"),
    ("Storage hard limits must be positive integers.", "存储容量和 inode 硬限必须为正整数。",
     "将 sizeMiB 与 maxInodes 填为不带小数点的正整数，范围须符合平台存储策略。"),
    ("Storage names cannot be shared by services.", "不同服务不能共享同一个 storage.name。",
     "为每个服务使用独立的存储名称，不要让多个服务共同写入同一持久卷。"),
    ("Existing storage cannot be removed, renamed or moved.", "已有存储不能删除、改名、移动，所属服务也不能改名。",
     "保持原服务名称及 storage.name、mountPath 配置；需要迁移数据时先备份并安排受控迁移。"),
    ("Route references an unknown service.", "路由引用了不存在的服务。", "将 routes 中的 service 设置为已定义的服务名称。"),
):
    _manifest_detail(_english, _message, _step)

for _rule, _message, _step in (
    ("required", "部署配置缺少必填字段。", "对照平台 schema 补齐该位置的必填字段。"),
    ("type", "部署配置字段类型不正确。", "按 schema 使用字符串、数字、布尔值、对象或数组；环境变量值必须是字符串。"),
    ("oneOf", "部署配置不符合唯一方案要求。",
     "服务的 image 与 build 必须且只能选择一个；健康检查须完整使用 http 或 exec 方案。"),
    ("additionalProperties", "部署配置包含不支持的额外字段。", "对照平台 schema 删除或修正未知字段。"),
    ("pattern", "部署配置字段格式不符合要求。", "检查域名前缀、服务名称、路径或变量名称的允许格式。"),
    ("const", "部署配置字段不是平台支持的固定值。", "按所选 schemaVersion 的 schema 核对固定字段；服务 kind 为 web，存储 mountPath 为 /data。"),
    ("enum", "部署配置字段不在允许的选项中。", "对照平台 schema 选择有效选项，例如 exact/prefix 或 started/healthy。"),
    ("minimum", "部署配置数值低于允许下限。", "按 schema 和平台资源配额调整数值。"),
    ("maximum", "部署配置数值超过允许上限。", "按 schema 和平台资源配额调整数值。"),
    ("minLength", "部署配置字符串长度不足。", "填写必需内容并满足 schema 的最小长度。"),
    ("maxLength", "部署配置字符串超过长度上限。", "缩短内容，使长度符合 schema 限制。"),
    ("minItems", "部署配置列表缺少必要条目。", "按 schema 补齐列表内容。"),
    ("maxItems", "部署配置列表条目过多。", "减少条目，使数量符合 schema 限制。"),
    ("minProperties", "部署配置对象缺少必要条目。", "补齐服务或配置条目。"),
    ("maxProperties", "部署配置对象条目过多。", "减少条目，使数量符合 schema 限制。"),
    ("not", "部署配置使用了被禁止的字段组合或名称。", "检查互斥字段以及平台保留的构建参数名称。"),
):
    _manifest_detail(f"Manifest field does not satisfy the {_rule} rule.", _message, _step)

# Re-localizing our own fixed result is safe and preserves useful specificity.
_MANIFEST_DETAILS.update({entry[1]: entry for entry in tuple(_MANIFEST_DETAILS.values())})


def _safe_path(value):
    """Keep schema structure; erase user-defined service, variable and secret names."""
    if not isinstance(value, str) or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_.*-]*", value):
        return ""
    if value in {"", "awesome.yaml", "repository", "ref", "subdomain", "appId", "port_forward",
                 "schemaVersion", "app", "services", "routes"}:
        return value
    parts = value.split(".")
    if len(parts) == 2 and parts[0] == "app" and parts[1] in {"name", "description", "subdomain"}:
        return value
    if (2 <= len(parts) <= 3 and parts[0] == "routes" and re.fullmatch(r"0|[1-9][0-9]{0,2}", parts[1])
            and int(parts[1]) < 128 and (len(parts) == 2 or parts[2] in {"path", "match", "service", "stripPrefix"})):
        return value
    if (len(parts) < 2 or parts[0] != "services"
            or not (parts[1] == "*" or re.fullmatch(r"[a-z][a-z0-9-]{0,30}", parts[1]))):
        return ""
    tail = parts[2:]
    if not tail:
        return "services.*"
    simple = {"kind", "port", "image", "command", "entrypoint", "workingDir",
              "env", "secretRefs", "dependsOn", "build", "resources", "healthcheck", "storage"}
    if len(tail) == 1 and tail[0] in simple:
        return "services.*." + tail[0]
    if len(tail) == 2 and tail[0] in {"env", "secretRefs", "dependsOn"} and tail[1]:
        return "services.*." + tail[0] + ".*"
    nested = {"storage": {"name", "mountPath", "sizeMiB", "maxInodes"}, "build": {"context", "dockerfile", "target", "args"},
              "resources": {"cpu", "memoryMiB"},
              "healthcheck": {"type", "path", "command", "intervalSeconds", "timeoutSeconds",
                              "retries", "startPeriodSeconds"}}
    if len(tail) == 2 and tail[1] in nested.get(tail[0], set()):
        return "services.*." + ".".join(tail)
    if len(tail) == 3 and tail[:2] == ["build", "args"] and tail[2]:
        return "services.*.build.args.*"
    return ""


def localize_error(error: dict | None, phase: str = "") -> dict | None:
    """Return safe presentation fields without changing the stored machine code."""
    if error is None:
        return None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = "DEPLOYMENT_FAILED"
    entry = _CATALOG.get(code, _CATALOG["DEPLOYMENT_FAILED"])
    reason = error.get("reason")
    reasons = {"INVALID_MANIFEST": _MANIFEST_REASONS, "HEALTH_FAILED": _HEALTH_REASONS}.get(code, {})
    known_reason = isinstance(reason, str) and reason in reasons
    if known_reason:
        entry = reasons[reason]
    elif code == "INVALID_MANIFEST" and isinstance(error.get("message"), str):
        entry = _MANIFEST_DETAILS.get(error["message"], entry)
    title, message, steps = entry
    result = {"code": code, "title": title, "message": message,
              "path": _safe_path(error.get("path", "")), "suggestion": steps[0], "steps": list(steps)}
    if known_reason:
        result["reason"] = reason
    return result


_EVENT_MESSAGES = {
    "Preparing the pinned repository source.": "正在获取并固定仓库代码，随后检查部署配置。",
    "Stopping the previous release before attaching persistent data.": "正在停止旧版本，确认退出后将持久卷交接给候选版本。",
    "Storage recovery requires operator attention; automatic active recovery is blocked.": "持久存储恢复需要运维确认，旧版本自动恢复已阻断，以避免双写。",
    "Freezing deployment inputs.": "正在固定部署配置、镜像和秘密配置版本。",
    "Building candidate images.": "正在构建候选版本镜像。",
    "Starting the isolated candidate release.": "正在启动隔离的候选版本。",
    "Checking candidate health.": "正在检查候选版本健康状态。",
    "Ensuring HTTPS is available.": "正在确认 HTTPS 证书可用。",
    "Publishing the candidate release.": "正在将路由切换到候选版本。",
    "Verifying the public HTTPS route.": "正在验证公开 HTTPS 路由。",
    "Deployment is active and verified.": "部署已生效，公开访问验证通过。",
    "Previous release cleanup needs attention; resources remain reserved.": "旧版本清理需要运维处理，相关资源继续保留。",
    "Restoring the previous public route.": "正在恢复上一版本的公开路由。",
    "Rollback requires manual recovery; all resources remain reserved.": "回滚需要人工恢复，全部相关资源继续保留。",
    "Cleanup requires manual recovery; resources remain reserved.": "清理需要人工恢复，相关资源继续保留。",
    "Interrupted publication recovered.": "中断的发布已完成恢复处理。",
    "Deployment failed safely.": "部署失败，本次候选版本已完成安全清理。",
    "Stopped containers for the active release were restarted and checked.": "当前版本已停止的容器已重新启动并通过检查。",
    "Active containers could not be recovered; release and routes remain unchanged. Recovery will retry with backoff.":
        "当前版本的容器恢复未完成，版本指向和路由保持不变；恢复操作将退避重试。",
}
_EVENT_MESSAGES.update({value: value for value in tuple(_EVENT_MESSAGES.values())})


def localize_event(event: dict) -> dict:
    """Translate historic fixed messages without mutating event storage."""
    result = dict(event)
    message = event.get("message")
    result["message"] = (_EVENT_MESSAGES.get(message, "此部署阶段已记录状态，请结合部署结果查看。")
                         if isinstance(message, str) else "此部署阶段已记录状态，请结合部署结果查看。")
    return result
