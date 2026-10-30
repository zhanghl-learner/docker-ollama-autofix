# Changelog

## Unreleased

### Added

- 新增 `autofix analyze [PATH]`：默认只读发现项目声明的 Ruff、mypy、ESLint 和 Clippy；`--run` 仅执行本机已可用工具，并保存普通 BuildResult 证据。
- 新增严格的 `autofix-static-analysis` JSON schema v1，记录声明来源、固定 argv、可用性原因、执行请求和完整运行结果。
- 新增静态分析专用指南，说明发现规则、退出状态、显式 `repair --step` 集成和后续属性测试安全边界。
- 新增本机 `run/repair --with-static-analysis` 严格绑定：分析器追加到原验证矩阵，缺失任一声明工具即拒绝，相同 argv 自动去重。
- 新增 Python/Ruff 固定样例，稳定区分“编译成功”和“静态分析契约失败”。
- 新增 `autofix property-check`：默认预览 Python/Hypothesis 候选，`--run` 在原始与 plan 修复副本中执行稳定反例和双版本验收。
- 新增严格的 `autofix-property-check` JSON schema v1、absolute-value 固定样例和属性测试安全指南。
- 新增 `autofix property-generate`：本地 Ollama 基于 reviewed plan 生成 schema-constrained Hypothesis 候选，只保存为 `.autofix` 审核工件且不执行。
- 新增 `autofix-property-candidate` JSON schema v1，并在落盘前拒绝明显进程、网络、文件系统模块和动态执行调用。
- 新增 `autofix property-promote`：只把完整 `counterexample-confirmed` 证据转换为未应用的新测试 patch，拒绝覆盖和非测试目标。
- 新增 `autofix-property-promotion` JSON schema v1，绑定报告、候选、plan、目标和 patch SHA-256，固定记录 `applied: false`。
- 新增严格的 repair suite JSON schema v3；聚合 required-path context recall、Top-5 recall、平均命中排名、累计文件/字符、截断率和 fallback 比例，v1 baseline 与历史 v2 repair schema 保留。
- 终端和 suite Markdown 展示上下文召回、排名与发送规模，使“上下文未命中”和“模型生成失败”可以分别诊断。
- 新增可审计的 `SourceContext` / `ContextSelection`：从构建输出直接路径出发，有界遍历 Python（含包重导出和唯一模块后缀的 `src/` 布局）、JavaScript/TypeScript 与 C/C++ 本地依赖，并记录选择原因、测试属性、字符数和截断状态。
- repair/evaluation 报告按尝试保存上下文选择元数据；`evaluate-suite` 在请求 Ollama 前显示前六个入选文件及原因，模型提示收到同一相关性说明。
- 验证失败重试上下文新增 persistent original failures、new failures 与 diagnosis→控制流→diff 一致性审计，要求确认失败输入能够到达被编辑语句。
- 字节级重复候选触发 `DUPLICATE RECOVERY REQUIREMENT`，明确要求改变编辑坐标或 corrected code；后续一次请求使用受控 0.25 温度尝试离开已证伪答案。
- `evaluate-suite` 人类模式默认显示实时 case/attempt 进度；等待 Ollama 和验证期间使用动态 spinner，候选到达后显示 diagnosis、文件、行号与验证结果。
- 套件汇总后直接打印每个成功 repair 的 summary、diagnosis、修改路径和 verified unified diff；`--no-progress` 可关闭中间事件，`--json` 始终保持纯 JSON。
- repair 编排器新增可选结构化 `RepairProgressCallback`，套件包装为包含用例序号、路径和耗时的 progress event，UI 观察不会参与修复接受决策。
- repair 套件逐用例 JSON 在 `evaluation.repair_details` 内嵌完整结构化证据，包括 proposal、静态拒绝、失败 diagnosis、真实 diff、构建步骤和输出；无需在 fixture 下创建中间 repair 报告。
- repair 套件 Markdown 为失败用例展开逐轮证据，diff/输出展示限制为 4000 字符，完整内容保留在 JSON。
- `autofix evaluate-suite --repair --model MODEL` 逐用例执行隔离模型修复，不写回 fixture；支持本机 runner 和全套件共享镜像的 Docker runner。
- 新增 `autofix-evaluation-suite` schema v2，记录 AutoFix 版本、模型、runner、尝试上限、首次通过率、平均尝试数、静态拒绝率、验证失败率、越界修改率与缺失必改文件率；baseline 继续输出 v1。
- repair 异常携带内存态结构化结果，使 `--no-save` 下仍能准确统计静态拒绝和验证失败，无需依赖中间审计文件。
- 新增 baseline-only `autofix evaluate-suite`：递归发现并稳定排序 `autofix-eval.yml`，批量复现故障契约，不调用模型或修改源码。
- 聚合评测原子发布逐用例 JSON、`suite.json` 与 `suite.md`；`autofix-evaluation-suite` schema v1 固定通过率、错误分类、语言/难度分组、契约摘要和耗时。
- 无效清单、执行异常、故障签名不匹配和重复用例名都会保留在聚合结果中，不因单个坏用例丢弃整套失败证据。
- 新增无交互 `autofix ci-check`：stdout 固定输出 `autofix-ci-result` JSON v1，并使用保留退出码区分通过、验证失败、环境错误、策略拒绝、配置错误和内部错误。
- `ci-check --output` 通过同目录临时文件和原子替换写入与 stdout 相同的 JSON；默认要求干净且已有 HEAD 的 Git 仓库，默认不产生普通 run 报告。
- 新增学习导读、核心代码导读、动手实验和 CI 契约文档，README 提供统一入口。
- 新增只读 `autofix git-status [PATH]`，报告 Git 可用性、仓库根目录、分支或 detached HEAD、基线 commit、tracked/untracked 变更和 direct-apply 策略。
- `autofix git-status --require-clean` 在非 Git 目录或脏工作区返回 1，可作为 CI 和 repair/worktree 的前置门禁；`--json` 输出稳定结构。
- 新增真实临时仓库测试，覆盖非 Git、干净/未提交基线仓库、tracked/untracked 修改、rename、detached HEAD 和 CLI 干净门禁。
- Git repair 默认从 HEAD 创建 detached 临时 worktree；基线复现、模型编辑和完整验证都在其中运行，仓库子目录会映射到 worktree 内的对应位置。
- repair report 升级为 v2，新增 workspace 类型、仓库根目录和基线 commit；临时执行路径不会写入持久报告。
- repair plan v4 在 v3 执行策略之上绑定仓库指纹、基线 commit、项目子路径和规范化最终 diff SHA-256；v1–v3 保持兼容读取。
- 成功 repair/apply-plan 输出可移植 unified patch、patch 原始字节 SHA-256，并支持 `--patch-output` 写入 CI 指定的新文件。
- `repair --apply --commit` 与 `apply-plan --commit` 支持创建只包含验证文件的本地 commit，并写入 `AutoFix-Diff-SHA256` trailer。

### Changed

- 上下文排序从“生产文件统一优先”升级为“错误输出直达生产文件→直达文件的本地生产依赖→直达测试→其余有界源码”，以在固定字符预算内减少无关文件竞争。
- 模型提示新增通用可达性规则：若 guard 排除失败输入，应修改 guard 或更早控制流；禁止在 predicate 已由外层分支保证时添加等价内层条件。

### Safety

- 静态分析发现不会因 PATH 中偶然存在工具而自动运行；执行必须显式使用 `--run`，不安装依赖、不创建配置、不修改源码，并要求项目本地工具真实存在且在 POSIX 上可执行。
- 静态分析便捷绑定不根据主机工具状态推断 Docker 镜像能力；Docker 用户必须通过显式 `--step` 绑定镜像内已审核命令。
- repair 不把原项目 ignored `.venv`/`node_modules` 路径带入 detached worktree；仅接受 PATH 可达工具，否则在模型调用前给出隔离依赖错误。
- 属性候选是可执行代码，默认不执行；运行时不写用户测试树，忽略依赖/缓存/符号链接，并要求原始失败、修复通过和 plan 原矩阵通过。
- 模型生成和候选执行保持独立命令；生成端没有 runner 路径，输出目录固定，元数据明确记录 `executed: false`。
- 候选晋升只生成 `/dev/null` new-file patch，不写测试目录、不调用 `git apply`；报告 evidence、候选 bytes 和 plan 状态必须重新匹配。
- 上下文枚举使用可剪枝目录遍历，不进入依赖、缓存、构建产物或符号链接目录；import 图只在已枚举项目源码内解析并设节点上限，歧义模块不猜测。
- 脏 Git 仓库和尚无 commit 的仓库在构建、Ollama 请求和源码修改前停止；AutoFix 不自动 stash、clean、commit、push 或 merge，只有显式 `--commit` 才能在验证后创建本地提交。
- worktree 在成功、验证失败、模型异常和 `KeyboardInterrupt` 后定向移除；清理失败不会自动全局 prune，以免影响用户的其他 worktree。
- 非 Git 项目继续使用临时副本；验证成功的 `--apply` 仍需通过原文件字节级并发检查才写回目标文件。
- Git-bound plan 在仓库身份、HEAD、项目子路径或工作区状态漂移时于写入前停止；检测到 `.git` 但 Git 不可用时不再降级为非 Git 流程。
- repair 策略仅排除当前项目 `.autofix/` 下的未跟踪审计产物，避免工具被自己的报告阻塞；原始 `git-status` 仍显示 dirty，其他未跟踪或 tracked 修改继续阻断。
- 本地 commit 前重新检查仓库指纹、HEAD、目标文件 SHA-256 和完整变更集合；不覆盖 patch、不提交审计产物或无关文件、不绕过 hooks，也不执行 amend/push/merge。

### CI

- Windows/Linux 测试任务在编译和 pytest 前验证干净 Git 检出契约。
- Windows/Linux pytest 改由 `autofix ci-check` 执行，并通过 `actions/upload-artifact@v7` 始终上传 JSON/JUnit 证据；workflow 不调用模型或自动修改仓库。

### Verified

- Windows 本机 279 项 pytest、`compileall`、Python/Node 复杂 fixture 基线与聚合套件、实时进度/终端 diff、语义重试、静态分析绑定、属性候选生成/验证/晋升检查通过。
- 使用本机 `qwen2.5-coder:7b` 从 absolute-value fixture 的 reviewed plan 生成待审 Hypothesis 候选；生成阶段未执行，随后独立差分验证得到两次稳定 baseline failure、两次 fixed pass、原矩阵通过和 `counterexample-confirmed`。
- 使用真实 confirmed 报告生成默认新测试 patch，`git apply --check` 通过；元数据保持 `applied: false`，检查后项目目标测试文件仍不存在。
- 隔离 PEP 517 wheel 构建通过，并确认 `autofix/repository.py`、`autofix/agent/artifacts.py`、`autofix/evaluation_suite.py`、`autofix/static_analysis.py` 与 `autofix/property_testing.py` 已包含在生成包中。
- 使用 `qwen2.5-coder:7b` 在独立 Git 仓库完成失败复现、detached-worktree 预览、plan v4 重放、指定 patch 输出和显式本地 commit 闭环；最终 commit 只包含目标源码并保留 diff trailer。

### Fixed

- `evaluate-suite --repair` 对“候选安全应用但验证失败”的用例不再显示空白 `Error`；终端表格后新增不会被列宽截断的完整失败摘要。
- detached Git worktree 因 `core.autocrlf` 使用 CRLF、而调用方原文件仍为 LF 时，隔离结果现在会在生成 report/patch/plan 和写回前规范化到原文件换行风格，避免安全的 diff SHA-256 校验错误拒绝同一方案。
- Windows 临时工作区在 8.3 短路径（如 `RUNNER~1`）与长路径混用时，源码上下文收集不再因纯词法 `relative_to()` 比较而失败；收集入口现在统一规范化项目根路径，并保留解析后路径的项目边界检查。
- 新增非规范根路径与已解析构建输出路径混用的回归测试，覆盖 GitHub 托管 Windows runner 暴露的路径身份问题；同时验证构建输出引用的项目外源码仍不会被收集。
- Windows 相对项目可执行文件集成测试改用临时 `.cmd`，不再复制系统 `cmd.exe`；保留 `shell=False` 的真实执行覆盖，同时兼容 GitHub 托管 Windows runner 的安全策略。
- CI 保存 pytest JUnit 结果，并把失败测试与异常发布为 GitHub Check annotation，避免托管 runner 日志不可匿名下载时丢失诊断信息。

## 0.19.0 - 2026-10-27

### Added

- 模型协议新增必填 `diagnosis`，要求在编辑前给出可审计的因果文件、源码行和分支行为说明；诊断写入 repair 报告和后续重试上下文。
- 失败验证报告保存每轮实际 diff、构建结果和新增失败测试；evaluation 指标新增 `verification_failure_count`。
- 新增精确重复 proposal 与已验证失败源码状态的循环检测。

### Changed

- 默认每轮验证失败后恢复到同一基线，下一候选不再叠加于错误源码；只有通过完整矩阵的候选才进入最终变更。
- retry 上下文改为最多三轮累计历史，标注失败编辑位置和由该 diff 新引入的 unittest/pytest 失败。
- retry 指令移到源码上下文之后，并使用低温度 `0.15` 避免确定性重复；首轮仍保持温度 0。
- 删除容易将小模型锚定到“减法改加法”的提示示例，加入通用边界输入与分支可达性检查。

### Fixed

- Windows 工具发现测试不再修改进程级 `os.name`；平台分支改由模块内 `_is_windows()` 封装，使 Windows fallback 能在 Linux Docker 中测试而不会让 pytest/pathlib 错误实例化 `WindowsPath`。
- 相对项目可执行文件测试将路径解析改为跨平台确定性单元测试，并单独保留 Windows `CreateProcess` 集成测试；Linux 加固容器不再尝试从故意设为 `noexec` 的 `/tmp` 直接启动临时脚本。

### Documentation

- 将项目定位明确为“本地优先、安全约束优先的代码修复工程原型与评测工具”，补充当前适用范围、人工审查责任和不能由构建通过推导出的结论。
- 下一阶段统一调整为“真实仓库安全接入与规模化评测”，优先建设 Git worktree/脏工作区保护、CI 契约、聚合评测、安全加固和语义上下文。
- 新增开发路线图与发布准备清单，并同步 README、使用手册、架构、评测指南和多语言验证报告。
- 修正文档中仍描述“失败候选累积修改”的旧流程；0.19.0 实际采用同一源码基线重试并累计失败证据。

### Repository

- 添加 MIT License，将生成的 Java `.class` 文件从版本控制移除并加入忽略规则。
- 通过隔离 PEP 517 构建生成 `autofix_cli-0.19.0-py3-none-any.whl`，并在临时虚拟环境完成版本与项目检测 smoke test。
- 新增 GitHub Actions CI：Windows/Linux 测试与复杂 fixture 基线、Ubuntu Docker 自举及 wheel 构建/安装 smoke test。

### Verified

- `qwen2.5-coder:7b` 在 Python 订单边界用例第 2 个 proposal 将 `quantity > 10` 修复为 `quantity >= 10`，评测通过。
- 同一模型在 Node 免邮边界用例第 1 个 proposal 完成修复，评测通过。
- 两次 evaluation 均保持原 fixture 不变，修改路径和必改路径契约全部满足。
- Windows 本机 162 项 pytest 通过；Linux Docker 自举的 compileall 与 pytest 步骤均通过，其中 161 项通过、1 项 Windows-only 测试按设计跳过。

## 0.18.0 - 2026-10-26

### Added

- `autofix-eval.yml` 新增可选 `expected_failure`，支持预期退出码、1-based 失败步骤、必含输出和禁含输出。
- evaluation JSON 新增 `failure_contract_matched` 与逐项 mismatch 原因。

### Safety

- repair 评测先执行确定性 preflight；失败签名不符时不调用模型，proposal 数为 0。
- 防止导入错误、缺少依赖或错误构建步骤仅因“退出非零”而冒充有效业务故障。

### Fixed

- Python `src/` 复杂用例在测试入口显式设置导入路径，使原项目和隔离副本行为一致，不再依赖 `sitecustomize` 启动时机。

### Verified

- Python 与 Node 复杂基线均同时显示 `initial failure: confirmed` 和 `failure contract: matched`。
- 使用真实 `qwen3:0.6b` 重新评测后，两者均进入模型边界并准确记录 3 个 proposal、3 个 rejection；原 fixture 保持不变。

## 0.17.0 - 2026-10-26

### Added

- 新增 `autofix evaluate` 与严格的 `autofix-eval.yml` v1 契约，用于验证故障基线并生成可比较的 JSON 报告。
- `--repair` 只在隔离副本运行模型修复，按允许/必改文件范围、构建结果、提议数和拒绝数计分，不修改原始用例。
- 新增 Python 订单定价和 Node.js 结算服务两个多文件、边界条件、多步骤复杂用例。

### Safety

- 评测清单拒绝未知字段、绝对路径、路径穿越、项目外解析、重复路径、不存在文件及不属于允许范围的必改路径。
- 报告保存清单 SHA-256；用例若初始即通过会被判为无效，而不是虚报修复成功。

### Documentation

- 新增复杂项目评测指南，并同步 README、架构图、多语言验证报告和路线图。

### Verified

- 单元测试覆盖失败基线、隔离修复不改原文件和危险路径拒绝。
- Python/Node 两个复杂用例均已在真实本地运行器确认故障基线。
- 真实 `qwen3:0.6b` 复杂用例评测为 0/2；无效/no-op 提议均被拒绝，失败仍保存完整 evaluation 报告且 fixture 字节不变。

## 0.16.0 - 2026-10-26

### Documentation

- `src/autofix` 的全部 Python 模块和公共 API 补充英文 docstring。
- 为精确行编辑、上下文边界、plan、隔离提交、rollback、离线 bundle 和多语言诊断补充设计意图注释。
- 架构文档新增英文注释维护规范，强调解释约束与原因而不是逐行复述代码。

### Fixed

- 恢复 Python 顶层函数、异步函数和类的破坏性删除检查；该检查此前误置于换行归一化函数的 `return` 之后而不可达。

### Verified

- 新增删除顶层公共函数的回归测试。
- 使用 AST 审计确认所有源码模块与公共符号都有 docstring。

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
