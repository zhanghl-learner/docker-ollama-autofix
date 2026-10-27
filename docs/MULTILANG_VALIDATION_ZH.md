# AutoFix 多语言闭环验证报告

本文记录 2026-10-23 至 2026-10-27 在 Windows 环境中完成的真实多语言验收，以及 AutoFix 0.19.0 对该闭环的自动化回归。基线成功组不是 mock：实际执行了本地 Ollama、真实编译器或测试运行器、隔离预览、plan 应用、独立重建与 undo。后续版本新增的 plan 确定性字段、多语言环境门禁、补丁安全和复杂用例评测由单元测试、CLI 集成测试和项目自举矩阵覆盖；下文会区分真实模型验收与后续回归。

## 1. 测试环境

| 组件 | 实际环境 |
| --- | --- |
| Python | 3.11.4 |
| Node.js / npm | v24.14.0 / 11.12.0 |
| Go | 1.21.2 windows/amd64 |
| CMake | 4.2.3，Visual Studio 2022 / MSVC 19.44 |
| Java / javac | 11.0.19 |
| Docker Engine | 29.8.2 |
| Docker 镜像 | `autofix-python:3.11`、`python:3.11-slim` |
| Ollama | 0.33.0 |
| 模型 | `qwen3:0.6b` |
| 缺失工具 | Cargo/Rust、Maven、Gradle |

Docker 本轮只对 Python 声明成功，因为本机没有 Node、Go、C++、Java 的专用离线镜像。其余成功组使用真实本机工具链。

## 2. 验收协议

每个成功样例执行：

1. `autofix inspect` 检测语言与构建系统；
2. `autofix run --no-save` 复现预期失败；
3. `autofix repair --max-attempts N` 请求真实 Ollama 预览；
4. 确认预览在隔离副本通过且原文件未变；
5. `autofix apply-plan` 应用审核过的确定性 plan；
6. 再次 `autofix run --no-save`，要求退出码 0；
7. `autofix undo`；
8. 再次运行，要求恢复为原始预期失败。

所有样例都故意把 `add(left, right)` 写成减法，测试要求通用加法。样例位于 `examples/multilang-validation/`，成功闭环后已执行 undo，因此仓库保留故障状态，便于重复验收。

## 3. 结果总览

| 样例 | 检测/实际命令 | 初始失败 | 模型预览 | plan 应用后 | undo 后 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| Python | python / pytest | `-1 != 5` | 1 轮通过 | `1 passed` | 恢复失败 | 完整成功 |
| Node.js | npm / `npm.cmd test` | 得到 `-1` | 1 轮通过 | `Node test passed` | 恢复失败 | 完整成功 |
| Go | go / `go test ./...` | 得到 `-1` | 早期提议因缩进被拒绝；改进后通过 | `ok` | 恢复失败 | 完整成功 |
| C++ | cmake / 配置、Release 构建、运行 exe | 得到 `-1` | 2 轮通过 | `C++ test passed` | 恢复失败 | 完整成功 |
| Java | maven 标记 / javac、java 矩阵 | AssertionError | 缩进安全归一化后 1 轮通过 | `Java test passed` | 恢复失败 | 完整成功 |
| Rust | cargo | cargo 不存在 | 未调用模型 | 不适用 | 不适用 | 环境门禁正确 |
| Maven | maven | mvn 不存在 | 未调用模型 | 不适用 | 不适用 | 环境门禁正确 |
| Gradle | gradle | gradle 不存在 | 未调用模型 | 不适用 | 不适用 | 环境门禁正确 |

Rust、Maven、Gradle 的 repair 报告中 `proposals` 均为 0，错误明确指出缺失的可执行程序。这是正确结果，不应把工具链缺失伪装成修复成功。

## 4. Python Docker 验证

Python 项目执行 `offline-check`，build-plan、offline-policy、dependencies、docker-engine、docker-image、ollama-api、ollama-model 七项全部为 ready。

基线验收当时使用显式 Docker 参数应用同一个审核 plan：

```powershell
autofix apply-plan PLAN.json --project examples\multilang-validation\python --docker
autofix undo examples\multilang-validation\python
```

在当前 plan v3 流程中，应先用 `autofix repair ... --docker` 生成绑定 Docker 策略的 plan；应用时可省略 `--docker`：

```powershell
autofix repair examples\multilang-validation\python --docker --max-attempts 3
autofix apply-plan PLAN.json --project examples\multilang-validation\python
```

此处的 v3 策略继承与漂移拒绝由 CLI 集成测试覆盖；不要把旧的本地 v1/v2 plan 手工改版本后当作 Docker v3 plan。

Docker 受限验证通过。undo 后 `calculator.py` 的 SHA-256 与应用前完全一致：

```text
FD706A6853D20F9CC84352F20BA642C7FBDA24FE7A7ACBC7BA49E4A50EF81944
```

这同时验证了临时 Docker 工作区、plan 原文件 hash、隔离提交、rollback 和 LF/CRLF 精确保留。

## 5. 测试中发现并修复的问题

### 5.1 Windows npm shim

自动检测原先生成 `npm`。PowerShell 能解析 `npm.ps1`，但 Python `subprocess` 的无 shell 执行无法直接使用它，结果为 WinError 2。检测器现在在 Windows 生成 `npm.cmd`、`pnpm.cmd` 或 `yarn.cmd`。

### 5.2 Go 用户缓存权限

Go 默认向用户级 `go-build` 缓存写入，在受限执行环境中得到 Access denied。执行器现在为每次构建创建一次性 `GOCACHE`，构建结束自动清理。

### 5.3 Windows 项目内相对 exe

Visual Studio 多配置生成器输出 `build/Release/calculator_test.exe`。Windows `CreateProcess` 配合单独 cwd 时没有按预期找到这个相对路径。执行器现在只对明确包含目录的 argv[0] 做项目内解析，并验证解析结果仍位于项目根目录。

### 5.4 小模型行号与缩进

`qwen3:0.6b` 在 Go 和 Java 中出现过行号偏移、tab 写成空格、8 空格写成 4 空格。验证器没有改成模糊 patch，而是仅接受全文件唯一的精确旧文本，或唯一的单行去缩进匹配，并用源码真实缩进重建。歧义候选继续拒绝。

### 5.5 换行字节恢复

旧实现使用 Python 通用换行读写，Windows 上 undo 可能把 LF 改成 CRLF，导致内容看似相同但 plan SHA-256 失效。0.12.0 使用 `newline=""` 精确读写，并把模型文本归一到源文件既有风格。LF、CRLF 参数化测试和真实 apply→undo hash 均已通过。

### 5.6 非 Docker 命令提示

repair 预览以前无论是否使用 Docker，都打印带 `--docker` 的 apply-plan 示例。现在仅在本次预览确实使用 Docker 时附加该参数。

### 5.7 验证命令漂移

0.14.0 的 plan v3 把预览通过的完整命令矩阵写入 `verification.steps`，并记录 runner、超时及 Docker 执行策略。CLI 集成回归会在生成 plan 后把 `autofix.yml` 改成一个必定失败的命令，再调用 `apply-plan`；应用仍使用 plan 中原始 pytest 步骤并通过。若调用方显式给出不同 `--step`、`--docker/--local`、`--timeout` 或镜像，应用会在源码保持不变时退出 2。旧 plan v1/v2 继续兼容并明确警告未绑定的部分。这一节描述的是确定性与安全回归，不额外声称重新调用了真实模型。

### 5.8 多语言环境门禁

0.15.0 为 Node、CMake/C++、Go/cgo、Rust、Java/JDK、Cargo 离线缓存和 Docker 增加第一批高置信度诊断。参数化测试覆盖 13 类环境输出；负向测试覆盖 Node 相对/未声明模块、Go 未定义符号、Rust 未定义值、Java `cannot find symbol` 和 C++ 语法错误。集成测试确认已声明 Node 依赖缺失时 proposals 为空、模型调用次数为 0、源码不变并生成 repair 报告。

这些是可重复的自动化安全回归，不替代真实工具链缺失验收。基线中的 Cargo、Maven、Gradle 可执行程序缺失仍是真实运行结果；新增的编译器/linker/JDK 文本模式目前通过合成 `BuildResult` 验证，后续应加入独立临时机或容器中的真实缺组件样例。

### 5.9 注释审计发现的保护回归

0.16.0 在补齐英文 docstring 时发现，Python 顶层定义删除检查位于换行归一化函数的提前 `return` 之后，实际不可达。修复后新增一个小文件用例：替换内容没有删除超过一半的非空行，但删除了公开顶层函数，必须以 `removes top-level definitions` 拒绝。这验证了两类破坏性门禁彼此独立，而不是仅依赖文件长度比例。

### 5.10 Linux 容器中的 Windows 模拟测试

发布前 Docker 自举运行到 `test_find_executable_discovers_per_user_docker` 时，测试通过修改共享 `os` 模块的 `os.name` 来模拟 Windows。Linux 上 `pathlib.Path` 因此错误选择 `WindowsPath`；测试失败后 pytest 生成报告时也读取了被污染的平台状态，最终显示 `INTERNALERROR`，掩盖了原始失败。

平台判断现封装为 `runtime.tools._is_windows()`，测试只替换该模块内函数，不再修改解释器级平台状态。这个回归说明本机 Windows 测试通过不能替代 Linux Docker 自举，两者应同时保留在发布检查中。

修复后 Docker 又暴露出相对可执行文件测试与容器策略的冲突：pytest 的 `tmp_path` 位于 `/tmp`，而加固容器有意使用 `noexec` 挂载 `/tmp`。旧测试即使设置执行位也不能直接启动临时 shell 脚本。现在路径解析由不执行文件的跨平台单元测试覆盖，Windows `CreateProcess` 的真实执行由仅限 Windows 的独立测试覆盖；没有为了让测试通过而移除容器的 `noexec` 安全限制。

最终复验结果为：Windows 本机 162 项通过；Linux Docker 的 compileall 与 pytest 两个步骤均通过，其中 161 项通过、1 项 Windows-only 集成测试按设计跳过。Docker 仍保持只读根文件系统和 `/tmp:rw,noexec,nosuid` 策略。

## 6. 模型质量观察

安全闭环通过不等于代码风格完美。0.6B 模型曾：

- 尝试把 Go 返回值硬编码为单个测试期望值；
- 把 Go/C++ 的结束花括号合并到 return 行；
- 写错缩进或行号。

AutoFix 能拒绝旧文本不匹配、用第二个测试阻止简单硬编码，并通过真实构建排除语法或行为错误；但只要编译器接受，格式较差的代码仍可能通过。因此生产项目应：

1. 使用覆盖多个输入的测试，避免单样例过拟合；
2. 将 formatter 的 check 模式和 linter 加入 `build.commands`；
3. 对 C++、Go、Java 优先使用更强的代码模型；
4. 保持 preview-first，人工审查 diff 后再 apply-plan。

## 7. 复现命令

以 Node 为例：

```powershell
$project = "examples\multilang-validation\node"
autofix inspect $project
autofix run $project --no-save
autofix repair $project --max-attempts 3
autofix apply-plan "$project\.autofix\plans\PLAN.json" --project $project
autofix run $project --no-save
autofix undo $project
```

其他成功样例只需替换最后一级目录为 `python`、`go`、`cpp` 或 `java`。plan 文件名每次生成不同，应使用 repair 输出的准确路径。

缺失工具链门禁可复现为：

```powershell
autofix repair examples\multilang-validation\rust-missing
autofix repair examples\multilang-validation\maven-missing
autofix repair examples\multilang-validation\gradle-missing
```

预期结果是环境错误、退出码 2、repair 报告存在且 proposals 为 0。

## 8. 结论

在这台机器已经安装的真实工具链范围内，AutoFix 已完成 Python、Node.js、Go、C++、Java 五种语言的源码修改闭环；Python 额外完成 Docker 离线隔离验证。Rust、Maven、Gradle 因工具缺失未声称修复成功，但环境门禁行为符合设计。

因此准确结论是：AutoFix 的语言无关 repair 管线能够处理多种文本源码和构建矩阵；最终覆盖范围仍取决于真实工具链、项目测试质量、Docker 镜像内容以及本地模型能力。

## 9. 0.17.0 复杂项目评测起点

`examples/complex-validation/` 新增两个不依赖第三方库的多文件用例：

| 用例 | 跨文件调用 | 验证步骤 | 故障 | 允许修改 |
| --- | --- | ---: | --- | --- |
| `python-order-service` | tests → package export → pricing | compileall + 4 个 unittest | 十件商品的折扣边界 | `src/order_service/pricing.py` |
| `node-checkout-service` | test → checkout → pricing | 两个 syntax check + 4 个 assertion | 5000 分的免邮边界 | `src/pricing.js` |

两个用例均已用 `autofix evaluate ... --no-save` 确认能够稳定复现初始失败。随后使用真实本地 `qwen3:0.6b`、`--max-attempts 3` 进行隔离评测，结果为 0/2：Python 的首次候选改错了输入校验语句，后续候选旧文本无法锚定；Node 三次都对 `checkout.js` 提交 no-op，没有修改实际边界条件所在的 `pricing.js`。两次最终构建均未通过，原 fixture 未改变，安全校验按预期拒绝了无效方案。

这不是框架失败，也不会包装成成功结论：它表明 0.6B 模型在跨文件定位上不足。评测过程中还发现“模型被安全拒绝时只有 repair 报告”的可观测性缺口；0.17.0 已修复为无论模型成功、验证失败或提议被拒绝，均生成 evaluation JSON，并保留提议数、拒绝数、错误和 repair 报告引用。既有第 3 节五语言结果仍是早期真实模型闭环记录。

第一次由用户独立复现 Python 用例时，任意非零退出码被错误接受为有效基线，实际失败是 `order_service` 导入失败。0.18.0 增加 `expected_failure` 后，两个复杂用例都绑定了退出码、失败步骤、业务断言文本和环境错误排除文本；Python fixture 也在测试入口显式加载 `src/`。重新验证显示两个基线均为 `failure contract: matched`，真实模型评测均产生 3 个 proposal 和 3 个 rejection，不再把环境失败计为模型结果。

0.19.0 使用真实 `qwen2.5-coder:7b` 重新运行两个复杂用例。Python 首轮错误修改折扣计算，并新增一个测试失败；系统记录该 diff、恢复基线，第二轮将第 10 行 `quantity > 10` 改为 `quantity >= 10`，四项测试全部通过。Node 首轮把 `subtotalCents > 5000` 改为 `>= 5000` 并通过。最终结果为 2/2，两个 evaluation 的修改路径、必改路径和禁止越界规则全部满足，原始 fixture 保持故障状态以便复测。

## 10. 证据边界与下一轮基线

上述结果证明的是：语言无关修复管线能够在当前样例和工具链上完成检测、受限编辑、真实验证、审计与撤销；v1 evaluation 能可靠区分基线错误、模型失败和合约通过。它尚不能证明对任意仓库、任意语言或生产故障具有稳定成功率。

下一轮验证不再以增加一个简单 `add()` 样例为目标，而以真实仓库接入风险为中心：

- Git 脏工作区不得被静默覆盖，修复应在 branch/worktree 中隔离；
- 同一评测集能够批量运行并生成聚合指标；
- Go、Rust、Java、C++ 至少各有跨文件业务故障和环境门禁；
- CI 能验证 schema、安全门禁和 fixture 基线，真实模型结果与 mock 测试分开；
- 所有公开数字包含模型、版本、runner、工具链、日期和失败样本。

这些条件的版本顺序与完成定义统一维护在[开发路线图](ROADMAP_ZH.md)，避免验证报告和实现计划分别漂移。
