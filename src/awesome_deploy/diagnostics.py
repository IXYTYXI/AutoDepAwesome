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
    "recovery": "恢复运行状态",
}


# Each entry is (title, message, ordered actions), all trusted constant strings.
_CATALOG = {}


def _register(codes, title, message, *steps):
    for code in codes.split():
        _CATALOG[code] = (title, message, steps)


_register("DEPLOYMENT_FAILED", "部署未完成", "部署执行失败，当前信息不足以确定具体原因。",
          "查看失败阶段和部署记录，核对对应配置。", "请运维人员结合受保护的服务器日志排查；修复后提交新的部署。")
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
          "仅部署无状态 Web 服务，核对平台能力与配置 schema。",
          "去除子模块、持久化挂载、镜像 VOLUME/ONBUILD 或其他不支持能力；不要关闭平台隔离限制。")
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
          "更新原应用时在表单选择对应应用；创建新应用时使用其他可用域名前缀。")
_register("APP_IDENTITY_CONFLICT", "应用身份不一致", "仓库地址或域名前缀与所选应用不一致。",
          "更新已有应用时使用其原仓库和域名前缀；部署其他仓库时选择创建新应用。")
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
          "通过安全配置入口检查名称与内容；内容不能含 NUL，且不能超过 16 KiB。")
_register("SECRET_NOT_FOUND", "缺少可用的秘密配置", "部署引用的秘密配置不存在或其固定版本无法读取。",
          "请运维在安全入口配置对应 secretRefs 引用，并确认固定版本可读取。")
_register("UNSAFE_SECRET_STORE UNSAFE_STATE_DIRECTORY", "状态目录保护检查失败", "平台状态或秘密配置目录的权限、路径不符合保护要求。",
          "请运维检查目录归属与权限，并去除符号链接；平台状态目录应仅允许运维服务账号访问。")
_register("AI_NOT_CONFIGURED", "AI 分析尚未配置", "平台尚未启用可用的 AI 分析接口。",
          "请运维配置 AI 接口地址、模型和服务端密钥；当前仍可使用本地修改建议。")
_register("AI_KEY_UNAVAILABLE", "AI 密钥不可用", "无法安全读取 AI 接口密钥。",
          "请运维通过安全入口配置密钥，并检查其读取权限；不要将密钥放入仓库或网页表单。")
_register("AI_BUSY AI_RATE_LIMITED", "AI 分析暂时繁忙", "AI 分析正在执行，或调用频率已达到限制。",
          "稍后再试，并先参考本地修改建议。")
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

ERROR_CODES = frozenset(_CATALOG)


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
    ("const", "部署配置字段不是平台支持的固定值。", "使用 schemaVersion: 1 和 kind: web，并核对其他固定字段。"),
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
              "env", "secretRefs", "dependsOn", "build", "resources", "healthcheck"}
    if len(tail) == 1 and tail[0] in simple:
        return "services.*." + tail[0]
    if len(tail) == 2 and tail[0] in {"env", "secretRefs", "dependsOn"} and tail[1]:
        return "services.*." + tail[0] + ".*"
    nested = {"build": {"context", "dockerfile", "target", "args"},
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
    known_reason = code == "INVALID_MANIFEST" and isinstance(reason, str) and reason in _MANIFEST_REASONS
    if known_reason:
        entry = _MANIFEST_REASONS[reason]
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
