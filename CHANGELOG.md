# Changelog

## 0.15.0 - 2026-10-23

### Added

- 新增 Node.js、CMake/C/C++、Go/cgo、Rust、Java/JDK、Cargo 离线缓存和 Docker 运行时的高置信度环境故障分类。
- Node.js 缺包仅在依赖已明确声明于 `package.json` 时提前停止；相对模块和未声明包仍交给代码修复闭环。
- 识别 npm 等脚本内部缺失的 POSIX/Windows 子命令，并给出工具链或专用镜像建议。

### Safety

- 环境诊断只扫描实际失败步骤，忽略此前成功步骤输出中的历史 warning，降低误报。
- 常见 Node 相对导入、Go 未定义符号、Rust 未定义值、Java `cannot find symbol` 和 C++ 语法错误均有负向测试，确保仍可 repair。
- 多语言环境门禁集成测试确认模型调用次数为零、源码不变且报告被保存。

### Documentation

- README、使用手册、架构文档和多语言验证报告同步补充环境门禁范围。
- 架构文档新增环境诊断决策图和规则边界说明。

## 0.14.0 - 2026-10-23

### Added

- repair plan v3 在有序验证命令之外绑定 runner、每步超时和完整 Docker 执行策略。
- Docker 策略包含镜像、CPU、内存、PID 上限和网络开关；`apply-plan` 默认复用，不依赖变化后的项目配置。
- `apply-plan --docker/--local`、`--timeout` 和 Docker 资源选项现在可作为显式一致性断言；与 plan 不同会在写入源码前停止。

### Safety

- Docker 配置新增严格运行时类型检查，并拒绝 NaN/Infinity CPU、布尔 PID、非布尔网络策略等异常值。
- plan v3 严格校验 runner、正有限超时和 Docker schema；本地 plan 不允许夹带 Docker 设置。

### Compatibility

- plan v1 继续回退当前配置；plan v2 继续复用绑定命令，并警告其运行环境仍来自 CLI。

### Verified

- 覆盖本地与 Docker 策略序列化、v2 兼容、runner/timeout/image 漂移拦截，以及源码零修改保证。

### Documentation

- 架构文档新增分层组件图、隔离修复时序图、plan v3 应用门禁图和 v1/v2/v3 兼容路径图。
- README 新增快速架构图，并统一 plan v3 自动复用 Docker 策略的示例与说明。

## 0.13.0 - 2026-10-23

### Added

- repair plan v2 保存预览通过时的完整有序验证步骤，包括每一步的显示名称与无 shell argv。
- `apply-plan` 默认复用 plan 内绑定的步骤，不再受预览后 `autofix.yml` 命令变化影响。
- `apply-plan --step` 仍可用于确认调用方预期，但命令必须与 plan 完全一致，否则在修改文件前停止。

### Compatibility

- 继续读取 plan v1；由于旧格式没有验证步骤，会明确警告并回退到当前项目配置。
- plan v2 对空步骤、空参数、非法字段和缺失验证元数据执行严格校验。

### Verified

- 覆盖 v2 序列化往返、配置漂移隔离、显式命令漂移拦截、v1 兼容及非法元数据拒绝。

## 0.12.0 - 2026-10-23

### Added

- 新增 Python、Node.js、Go、CMake/C++、Java 的真实本地模型闭环样例和验证报告。
- 缺少 Cargo、Maven、Gradle 等构建程序时，将退出码 127 分类为环境故障，模型调用保持为零。
- 行编辑可在旧文本全文件唯一时安全纠正偏移行号；单行仅缩进不同且唯一时复用源码真实缩进。
- 构建执行器为 Go 使用一次性 `GOCACHE`，并解析项目内相对可执行文件。

### Fixed

- Windows Node 检测改用 `npm.cmd`、`pnpm.cmd` 或 `yarn.cmd`，兼容无 shell 执行。
- 源码 apply 和 rollback 保留原始 LF/CRLF，undo 可以恢复修复前的文件 SHA-256。
- 非 Docker repair 预览不再错误建议给 `apply-plan` 添加 `--docker`。
- Ollama 提示明确禁止把单个测试期望值硬编码进实现，并要求保留语言缩进。

### Verified

- Python、Node.js、Go、C++、Java 均完成真实 qwen3:0.6b 预览、plan 应用、重新构建和 undo。
- Python 额外完成 Docker 离线就绪检查、受限容器 plan 验证和 undo 前后 SHA-256 一致性检查。

## 0.11.0 - 2026-10-23

### Added

- 配置新增互斥的 `build.commands` 有序构建矩阵。
- `run`、`repair` 和 `apply-plan` 支持可重复的 `--step` 临时矩阵。
- `init` 支持写入显式矩阵，并为 CMake 等多步骤检测结果自动生成 `build.commands`。
- `offline-check` 识别并显示配置的矩阵步骤数。

### Safety

- 每个矩阵步骤继续使用无 shell argv 执行。
- 步骤按顺序运行并在首个失败处停止；只有全部步骤通过才接受修复。
- `build.command` 与 `build.commands`、`--command` 与 `--step` 均拒绝混用，避免验证入口歧义。

## 0.10.0 - 2026-10-23

### Added

- 为多语言仓库增加显式构建系统选择。
- `inspect`、`init`、`run`、`offline-check`、`repair` 和 `apply-plan` 支持 `--build-system`。
- 项目配置支持持久化 `build.system`。
- 未检测到指定构建系统时，在执行前停止并列出实际候选。
- 新增完整中文使用手册和实现架构文档。
- 完整 v2 离线包自动携带 README、使用手册和架构文档，并将它们纳入 manifest 校验。
- 离线包构建复用本机已安装的构建工具，不再为隔离构建环境额外联网获取 setuptools。

### Verified

- 精确行级修改、隔离预览、hash 绑定 plan、确定性应用和 rollback 保持原有安全边界。
- 多构建系统选择覆盖 CLI、配置加载、离线检查和检测模型测试。

## 0.9.0 - 2026-10-21

### Added

- 本地 Ollama 多轮修复闭环。
- Docker 一次性工作区和受限容器执行。
- 环境故障分类、测试文件保护和破坏性修改拦截。
- 验证后 plan、修复历史、rollback 与 undo。
- 用户级全局配置和离线运行包。
