# AutoFix 开发路线图与发布准备

本文是 AutoFix 0.19.0 之后的统一路线图。功能优先级、评测目标和发布检查以本文为准；版本号是规划目标，不代表已经发布。

## 1. 项目定位

AutoFix 是一个本地优先、安全约束优先的代码修复工程原型与评测工具。它围绕一个有限但可验证的闭环工作：复现构建或测试失败，让本地模型提出受限行级编辑，在隔离环境运行真实门禁，只把通过结果交给开发者审查和应用。

核心价值不是替代开发者，而是把概率性的模型建议放进确定性的工程边界：

- 模型负责诊断和提出候选编辑；
- schema、路径、旧文本和破坏性规则负责静态约束；
- 构建矩阵和退出码负责验收；
- plan、报告、history 和 rollback 负责审计与恢复；
- evaluation 契约负责比较版本与模型，而不是依赖成功截图。

当前目标用户是希望在本地或离线环境处理可复现故障、并愿意审查 diff 的开发者和小团队。当前不是通用自主开发代理、依赖安装器、恶意代码强沙箱或无人值守生产修复服务。

## 2. 当前可证明的基线

| 维度 | 0.19.0 已有证据 | 尚不能据此声称 |
| --- | --- | --- |
| 自动回归 | Windows 162 项通过；Linux Docker 161 项通过、1 项 Windows-only 跳过 | 覆盖所有平台和真实工具链组合 |
| 多语言闭环 | Python、Node.js、Go、C++、Java 的真实修复流程 | 对任意大型多语言仓库稳定成功 |
| Docker | Python 专用镜像下的离线、受限验证 | 容器是恶意项目的强安全沙箱 |
| 复杂评测 | Python、Node 两个多文件边界用例 | 两个样例代表实际总体成功率 |
| 模型比较 | `qwen3:0.6b` 为 0/2；`qwen2.5-coder:7b` 为 2/2 | 更大模型一定正确或没有回归 |
| 可恢复性 | preview、plan v4、apply、history、rollback、undo | 可以替代 Git 和人工审查 |

发布或简历表述必须区分自动测试、真实模型运行和合成环境错误测试。不得把“构建通过”扩写为“业务、安全或生产验证通过”。

## 3. 下一阶段总目标

下一阶段名称：**真实仓库安全接入与规模化评测**。

完成这一阶段后，项目应能在不覆盖开发者未提交工作的前提下，把修复放进可追踪的 Git 隔离环境；能批量运行一组版本固定的评测契约；能以机器可读报告回答“哪个模型、哪个版本、在哪些任务上成功或失败”。

优先级遵循三个原则：

1. 先保护用户工作，再扩展自动修改能力；
2. 先建立度量，再优化提示、上下文或模型路由；
3. 先保证失败可解释、可恢复，再追求更高通过率。

## 4. 里程碑

### 4.1 v0.20：Git 安全接入与 CI 契约

目标是让 AutoFix 可以安全进入真实仓库的日常工作流。

计划能力：

- 检测 Git 仓库、当前分支和脏工作区；默认不在存在未提交修改的原工作区直接 apply；
- 在临时 branch 或 `git worktree` 中执行模型修改和验证；
- 成功后输出 patch、变更摘要和可选 commit，不自动 push 或 merge；
- [x] 首个 CI 垂直切片定义 `ci-check` 稳定退出码与 JSON schema v1，区分通过、验证失败、环境错误、策略拒绝、配置错误和内部错误；
- [x] GitHub Actions 使用 `ci-check` 执行 pytest，并始终上传 JSON/JUnit 证据；不调用模型、应用 patch 或修改 Git；
- 报告记录基线 commit、工作树状态和最终 diff hash。

完成标准：

- 脏工作区测试证明原有未提交内容字节不变；
- worktree 成功、失败、取消和超时都能清理或留下明确可恢复信息；
- 本地 preview/apply 兼容行为不退化；
- CI 示例在干净克隆中完成单元测试和 fixture 基线验证；
- 新行为同步更新使用、架构、安全和 changelog 文档。

### 4.2 v0.21：聚合评测与多语言扩展

目标是从两个复杂案例升级为可比较的评测套件。

计划能力：

- [x] `autofix evaluate-suite` 以稳定顺序发现并批量执行 baseline 清单；
- [x] baseline 模式输出逐用例 JSON 和聚合 JSON/Markdown，并固定 schema v1；
- [x] 隔离执行批量模型 repair，并在相同契约上比较结果；
- [x] 指标包含通过率、首次通过率、平均尝试数、静态拒绝率、验证失败率、越界修改率、缺失必改文件率和耗时；
- Go、Rust、Java、C++ 分别增加跨文件业务故障和环境门禁；
- Python/Node 增加类型、异步、lint/format 或锁定依赖场景；
- 结果按 AutoFix 版本、模型名/digest、runner、工具链和契约 hash 分组。

完成标准：

- 至少覆盖五个生态，且每个主要生态不只包含算术替换；
- 套件包含成功、模型失败、静态拒绝和环境错误四类结果；
- 连续运行不会污染 fixture；
- 聚合报告不会丢弃失败用例，也不会混合契约已变化的分数；
- 发布文档提供完整复现命令和原始报告位置。

### 4.3 v0.22：安全加固与语义上下文

目标是在可度量的评测基础上提高真实仓库适应性，而不是通过放宽修改约束换取通过率。

计划能力：

- 可配置命令/路径策略和项目级信任声明；
- 非 root Docker 用户、资源上限和日志敏感信息脱敏；
- 从报错栈、import、符号和依赖关系选择上下文；
- 记录上下文选择依据，支持比较规则检索与语义检索；
- 在固定套件上做消融对比，确认上下文策略确实提高成功率或降低 token/延迟。

完成标准：安全门禁测试不减少；语义选择不能绕过路径策略；性能提升由固定契约的聚合评测证明。

### 4.4 v1.0 候选：受控团队试用

进入 1.0 候选前至少需要：

- 多平台 CI 和可复现发布包；
- 明确许可证、安全说明、支持矩阵和漏洞报告方式；
- 在若干非 fixture 仓库完成受控试用并记录失败类型；
- 稳定的配置、报告和 plan 兼容策略；
- 取消、超时、磁盘不足、Docker/Ollama 中断等故障恢复测试；
- 不依赖维护者本机隐含状态的安装与复现说明。

## 5. 暂缓事项

以下功能有扩展价值，但不应抢在 Git 隔离、聚合评测和安全策略之前：

- 自动 push、自动创建或合并 Pull Request；
- 多代理并行修改；
- 自动安装任意依赖；
- 大范围重构、创建/删除/重命名文件；
- Web 管理后台或多租户服务；
- 远端模型路由和云端源码上传；
- 仅为提高演示通过率而放宽测试保护或破坏性修改门禁。

## 6. 下一轮开发前检查清单

### 6.1 代码与测试

- [x] `python -m pytest -q`：Windows 当前 162 项通过；
- [x] `python -m compileall -q src tests`；
- [x] `autofix run . --docker --no-save`：两个步骤通过；Linux Docker 161 项通过、1 项 Windows-only 跳过；
- [x] 两个复杂 fixture 的 baseline evaluation 匹配预期失败；
- [x] 已保存 `qwen2.5-coder:7b` 的真实 evaluation 原始报告；报告位于 fixture 的 `.autofix/evaluations/`，默认不提交；
- [x] `git diff --check` 通过；当前只有 Windows 的 LF→CRLF 提示，无空白错误；
- [x] 已审核未跟踪文件和生成物；Java `.class` 已从版本控制移除并加入 `.gitignore`。

### 6.2 发布与仓库卫生

- [x] 0.19.0 源码、测试、fixture、CI 和文档已整理为统一发布提交；
- [x] 已添加 MIT License，并在 README 声明；
- [x] 已增加 Windows/Linux、Docker 自举和 wheel smoke test 的 CI workflow；README 徽章直接读取该 workflow 状态；
- [x] 已确认编译产物、模型和 `.autofix` 报告不会进入 Git；候选文件未发现明显密钥模式；
- [x] 已通过隔离 PEP 517 构建 wheel，并在临时虚拟环境验证版本和项目检测；
- [x] 已创建带说明的 `v0.19.0` tag，tag 指向完成上述验证的发布状态。

### 6.3 文档一致性

- [x] README、使用手册、架构、评测和验证报告采用统一定位；
- [x] 路线图明确适用边界、里程碑和完成定义；
- [x] 已复核当前版本号、相对链接、主要命令和验证输出；
- [x] 功能变更已同步更新 `CHANGELOG.md` 和受影响文档；
- [ ] 真实模型数字标注模型、日期、runner、工具链与 fixture 契约。

## 7. 建议的下一轮任务拆分

下一轮从 v0.20 的最小垂直切片开始：

1. [x] 定义只读 `GitWorkspaceStatus` / `GitChange` 数据模型和 direct-apply 策略；新增 `autofix git-status`，不修改 Git 或源码；
2. [x] 覆盖非 Git、干净 Git、tracked/untracked 脏工作区和 detached HEAD；CI 使用 `--require-clean` 验证干净检出契约；
3. [x] 在 preview/default-isolate 路径接入 detached worktree；基线、编辑和验证均不在原工作树运行，失败、异常和取消路径清理注册项；
4. [x] repair report v2 与 plan v4 记录仓库指纹、基线 commit、项目子路径和最终 diff SHA-256；v1–v3 继续兼容读取；
5. [x] 输出独立 patch 与原始字节 SHA-256；支持显式 `--commit` 创建只含验证文件的本地 commit，并保留 hooks 与人工确认边界；不自动 push 或 merge。

这一顺序能够先固定安全契约，再实现外部行为。前三项已经形成 Git 检测→门禁→隔离验证的垂直切片；实现没有通过自动 stash、清理或覆盖来处理脏工作区。每个步骤都必须保持现有测试和多语言 fixture 基线不退化。

第一切片已在 2026-10-28 完成本机验证：171 项测试、两个复杂 fixture 基线、源码编译和 wheel 内容检查通过；GitHub Actions 结果需在推送本次提交后单独记录，不能沿用上一次提交的绿色状态。

第二切片已在 2026-10-28 完成本机验证：180 项测试覆盖 worktree 成功、失败、脏/未提交基线拒绝、模型异常、`KeyboardInterrupt`、构建超时、子目录映射和验证后写回；两个复杂 fixture 基线及源码编译继续通过。新提交仍需由 GitHub Actions 在 Windows/Linux 上独立确认。

第三切片已在 2026-10-28 完成本机验证：189 项测试覆盖 plan v4 正常往返、Git SHA-1/SHA-256 object ID、HEAD/仓库/子路径漂移、脏工作区、diff 摘要篡改、unversioned 身份变化、规范化顺序和远端 URL 隐私；v1–v3 回归、复杂 fixture 基线及源码编译继续通过。

第四切片已在 2026-10-28 完成本机验证：199 项测试覆盖 patch 确定性顺序、拒绝覆盖、字节摘要、`git apply --check`、commit 精确 pathspec、并发内容/索引变化、意外文件阻断和 CLI 端到端提交；Python/Node 复杂 fixture 基线、源码编译、workflow YAML 与 wheel 内容检查继续通过。commit 前再次核对 provenance、目标 bytes 和完整 Git 变更集合，保留 hooks，并明确不执行 push/merge。

同日使用 `qwen2.5-coder:7b` 完成独立 Git 项目的真实闭环。首次重放暴露主工作区 LF 与 detached worktree CRLF 导致的 diff digest 漂移；安全门禁在写入前停止。修复换行归一化并加入强制 `core.autocrlf=true` 回归后，重新生成的 plan v4 成功应用，指定 patch 通过反向检查，本地 commit 仅修改一个目标源码文件且包含 `AutoFix-Diff-SHA256` trailer。

第五切片于 2026-10-29 完成首轮本机验证：新增 `ci-check` JSON envelope v1、六类保留退出码、严格 Git/clean 默认策略、原子结果文件和仓库内 JSON Schema；209 项测试覆盖通过、普通验证失败、缺工具环境错误、脏/非 Git 仓库拒绝、配置错误、显式非 Git 运行、内部异常及 stdout/文件一致性。GitHub Actions 已切换为通过该契约执行 pytest 并上传 JSON/JUnit，推送后的 Windows/Linux 结果仍需独立确认。

第六切片于 2026-10-29 进入 v0.21：新增 baseline-only `evaluate-suite`，确定性发现多级清单、跳过生成目录、完整保留失败/无效/执行异常/重复名称，并原子发布逐用例 JSON、聚合 JSON 与 Markdown。`autofix-evaluation-suite` v1 固定总数、错误分类、通过率、语言/难度分组、契约摘要和耗时。模型 repair 聚合仍明确属于下一切片，baseline 通过率不得表述为模型成功率。

第七切片继续 v0.21：`evaluate-suite --repair` 对每个匹配故障契约执行隔离模型修复，v2 报告记录模型、runner、尝试上限、首次通过率、平均尝试数、静态拒绝率、验证失败率和范围失败率。baseline v1 保持不变；模型 digest、工具链身份和跨版本排行榜仍属于后续工作。

第八切片完善失败可解释性：终端不再把普通验证失败显示为空 `Error`，逐用例 JSON 内嵌完整 repair 证据，Markdown 展开 diagnosis、diff、失败步骤和有界输出。证据来自内存结果，不在 fixture 下额外产生单用例报告；baseline v1 和 repair v2 外层字段保持兼容。

第九切片完善长任务交互：repair 状态机新增只读 progress event，套件附加用例序号，CLI 默认用 spinner 和持久里程碑展示模型等待、候选诊断、编辑位置与验证结果；汇总后直接输出成功修复的 verified diff。`--no-progress` 可关闭中间输出，`--json` 保持严格纯 JSON。

第十切片针对 diagnosis 正确但编辑语义无效的失败模式增强重试：反馈标记 persistent/new failures，要求检查失败输入对编辑位置的可达性，并明确禁止在已保证 predicate 的分支内增加等价内层条件；字节级重复候选触发更强纠偏指令和受控 0.25 温度。安全门禁与真实构建验收保持不变。

第十一切片进入 v0.22 语义上下文的最小垂直实现：构建输出直接引用文件优先，随后有界遍历 Python import、JavaScript/TypeScript 相对 import/require 与 C/C++ 本地 include，把经包重导出到达的生产实现排在无关回退源码之前。每轮 `ContextSelection` 同时进入模型提示、终端进度和 repair/evaluation JSON，记录原因与截断状态；项目边界、文件预算、测试只读和可编辑路径枚举保持独立门禁。下一步是在固定套件上增加上下文消融指标，而不是立即引入向量数据库。

第十二切片完成上下文消融度量：repair suite 升级为 JSON v3，在保留 v1/v2 schema 的同时新增 required-path recall、Top-5 recall、平均排名、累计文件/字符、截断和 fallback 指标，并同步终端与 Markdown。由此可以区分“必改文件没进入上下文”和“模型看见文件但修复失败”。下一阶段开始接入静态分析信号：先识别项目已经声明并安装的 Ruff、mypy、ESLint、Clippy 等命令，作为显式验证步骤运行，不自动安装依赖。属性测试随后采用隔离生成、先展示后验证的实验模式，不直接写入用户测试目录，也不以生成测试本身作为修复成功。

第十三切片实现声明式静态分析入口：`autofix analyze` 默认只读发现 Ruff、mypy、ESLint、Clippy，`--run` 才通过无 shell runner 执行已声明且本机可用的固定命令；缺失工具返回可操作状态但不自动安装。新增机器可读 JSON v1 schema、项目 `.venv` 路径处理、POSIX 执行权限检查和 CLI 回归。当前通过 `--step` 显式加入 repair，下一切片再设计 plan 绑定快捷选项和静态分析评测 fixture；属性测试继续排在其后。

第十四切片增加本机 `run/repair --with-static-analysis`：在原验证矩阵后严格追加全部项目声明的可用分析器，任一工具缺失即拒绝，重复 argv 去重，Docker 场景不根据主机状态猜测镜像能力。新增“编译通过、Ruff F401 失败”的固定样例，plan 保存展开后的步骤而不是动态发现规则。下一阶段进入隔离式属性测试候选协议：先限定 Python/Hypothesis 的预览、反例复现和双版本验证，不直接写入用户测试目录。

第十五切片实现 Python/Hypothesis 候选协议：默认只预览可执行源码，`--run` 才在两个临时副本中要求原始版本两次稳定失败、plan 修复版本两次通过、原验证矩阵继续通过。候选有 64 KiB/UTF-8/AST/`@given` 边界，不进入用户测试目录；plan 在执行前重新验证哈希和 provenance。新增 JSON v1 schema、固定 absolute-value 样例和失败 verdict 回归。下一阶段可以研究模型生成候选，但生成端只能产出待审文件，不能绕过本协议或把生成测试计作修复成功。

第十六切片实现本地模型候选生成：`property-generate` 只读取 plan 摘要及已审核修改的 before/after 文件，使用独立严格 JSON schema 取得 rationale/property/content；候选在落盘前通过既有 Hypothesis 结构检查和进程、网络、文件系统、动态执行静态拒绝，只能保存到 `.autofix/property-candidates/`，元数据固定为 `executed: false`。生成与 `property-check --run` 保持权限分离，下一阶段侧重把已确认候选转为人工审核的持久回归测试 patch，而不是自动写入测试树。

第十七切片实现 confirmed candidate 晋升：`property-promote` 重新验证报告完整 evidence、候选 SHA、plan diff/provenance、项目身份和新测试目标，输出 Git-compatible new-file patch 与 `applied: false` JSON；拒绝未确认/残缺报告、候选漂移、越界或非测试路径以及覆盖已有文件。命令不写测试树、不自动 apply/commit。下一阶段评估人工确认后的显式 apply 工作流及 CI artifact 集成，仍不允许默认写回。

## 8. 文档维护规则

以后每轮迭代至少检查：

- CLI 行为变化：更新 `README.md` 与 `USAGE_ZH.md`；
- 状态机、安全边界或数据格式变化：更新 `ARCHITECTURE_ZH.md`；
- 指标、fixture 或模型结果变化：更新 `EVALUATION_ZH.md` 与 `MULTILANG_VALIDATION_ZH.md`；
- 版本可见变化：更新 `CHANGELOG.md`；
- 优先级或完成标准变化：只在本文件维护主版本，再从其他文档链接到这里。

文档中的能力必须能指向代码、测试或保存的真实验证结果；规划中的能力始终使用“计划”“目标”或未勾选状态，不与已实现功能混写。
