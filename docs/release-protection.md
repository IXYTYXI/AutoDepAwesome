# 前端发布保护

维护可读的 `src/awesome_deploy/static/` 源码；发布工具只在本地生成独立的 `dist/protected-static/` 目录。它不修改源码、Python 服务、配置、凭证或线上版本，也不会调用 Docker。安装依赖后，转换通过 npm 包的本地 `obfuscate()` API 完成，不上传源码到在线混淆服务。

## 构建与核验

要求 Node.js 22 或更高版本。本次使用 Node.js 24.13.0 验证；正式构建应固定同一 Node 版本，并使用提交的 lockfile。

在仓库根目录执行：

```sh
npm --prefix tools/frontend-release ci --ignore-scripts
npm --prefix tools/frontend-release test
npm --prefix tools/frontend-release run build
npm --prefix tools/frontend-release run verify-ui
```

构建依赖固定为 `javascript-obfuscator 5.9.0`，传递依赖及下载完整性记录在 `package-lock.json`。工具目录的 `.npmrc` 也禁用安装生命周期脚本。依赖只用于构建，不需要随部署服务安装或发布。

输入和输出位置固定，命令不接受自定义路径或混淆参数。只读取 static 目录中的普通静态资源文件；拒绝符号链接、硬链接、隐藏文件、后端文件和 source map。源码路径和输出路径下的目录组件同样检查符号链接。输出已存在时立即报错，不删除或覆盖旧发布目录。重新构建前，由操作者确认并将此前的 `dist/protected-static/` 移到另外的归档目录，或仅删除这份旧产物。不要并发修改构建目录或源码；该工具不是针对恶意同 UID 进程的文件系统沙箱。

输出为同名静态资源和 `manifest.json`，没有后端、仓库、配置、密钥或依赖目录。JS 执行混淆；HTML、CSS、帮助文本及图片字体等静态资源按原字节复制。当前扩展名白名单：`.js`、`.css`、`.html`、`.txt`、`.svg`、`.png`、`.jpg`、`.jpeg`、`.webp`、`.ico`、`.woff`、`.woff2`。增加新的资源类型需要审查并修改白名单。

清单逐文件记录原始及产物 SHA-256、字节数、是否转换，并记录固定工具版本与选项。`verify-ui` 在测试之前检查完整文件列表、输出哈希以及当前源码哈希；额外文件、被修改的产物、源码更新后未重新构建都会拒绝。SHA-256 清单用于确认构建一致性，清单本身没有数字签名，不能抵抗攻击者同时替换清单和产物。

## 转换选择与兼容性

使用 `browser-no-eval`、压缩排版、局部标识符改名以及 25% 阈值的字符串数组。字符串不做 Base64/RC4 编码，不添加旋转循环或多层调用包装。每个 JS 文件有独立辅助标识符前缀，固定 seed 保持构建可复现。

明确关闭 `controlFlowFlattening`、`deadCodeInjection`、`debugProtection`、`selfDefending`、`renameGlobals`、`renameProperties`、`sourceMap` 等选项。不拦截调试器、不关闭控制台、不放宽 CSP。全局函数、变量和公开对象属性保留名称，避免跨脚本及已有测试入口被破坏；局部代码仍会转换。不要把未来引入的第三方库混入此目录直接转换，先单独评估兼容性与许可证。

现有 `tests/ui_*.test.cjs` 原样复制到临时目录，通过临时 static 符号链接加载保护后的文件。测试文件内容和断言均不修改、不筛选测试项。预加载检查只给原有 VM 上下文添加 `codeGeneration: {strings: false, wasm: false}`，让 `eval` / `new Function` 等运行时字符串求值失败；临时目录在结束后清理。

2026-10-23 本地结果：30 个静态文件，其中 8 个 JS；原文件总计 580,566 字节，产物 584,931 字节（清单除外，增加约 0.75%）。11 项发布工具测试及原有 122 项 UI 测试通过。原有 UI 套件覆盖 app、manage、auth、login、members、vault；document 与 api-credentials 的 JS 本次只做转换和语法解析，不能据此声称两个页面的全部交互已测试。VM 测试也不能替代真实浏览器的布局、完整 CSP 响应头、网络和性能验收。发布前应在受控预览或目标平台继续做这些验证。

本工具只生成可评审产物，不改变服务当前使用的静态目录。后续发布应将产物复制进独立的发布包或镜像构建目录，替换该副本内的 static 文件；不要覆盖工作区的可读源码。此步骤应由现有远端发布流程单独完成，本地 Docker Desktop 不需要启动。

## 安全边界与依据

混淆增加源码阅读、复制和静态分析的成本，浏览器最终仍能执行并观察这些代码，无法保证不可还原或隐藏发给浏览器的密钥。敏感值必须继续留在服务端。现有 AES-256-GCM 密钥库、DEK/KEK 分离、HMAC 完整性校验、高熵 API token 及其摘要存储是独立的安全措施，本工具没有修改它们。

官方依据：

- [javascript-obfuscator 官方文档](https://github.com/javascript-obfuscator/javascript-obfuscator)：本地 Node API、`browser-no-eval`、选项及性能取舍。发布固定依赖版本，不依赖文档默认值。
- [OWASP 静态分析防护](https://mas.owasp.org/MASVS/controls/MASVS-RESILIENCE-3/)：混淆的目标是增加理解程序内部逻辑的难度。
- [OWASP 密钥存储与加密](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)：使用标准加密、控制密钥访问、分离密钥与数据；同机应用失陷仍属于信任边界。
- [cryptography AESGCM](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)、[Python secrets](https://docs.python.org/3/library/secrets.html)、[Python HMAC](https://docs.python.org/3/library/hmac.html)：既有密码机制的官方说明。
