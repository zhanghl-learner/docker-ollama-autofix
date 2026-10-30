# AutoFix 中文使用手册

本文对应 AutoFix 0.19.0。AutoFix 在本机执行真实构建或测试，把失败信息和受限源码上下文交给本地 Ollama，验证模型给出的精确行级修改，并且只在修复通过完整构建矩阵后生成可应用方案或写回项目。

## 1. 先理解三个原则

1. `autofix run` 只负责执行和记录，不会调用模型，也不会修改源码。
2. `autofix repair` 默认是安全预览：Git 项目在 detached worktree、非 Git 项目在临时副本中修改和验证，原项目不变；验证成功才会生成 plan。
3. 推荐先预览，再用 `autofix apply-plan` 应用已经看过的精确结果。直接使用 `--apply` 更快，但应当配合版本控制。

AutoFix 修复的是“被实际命令暴露出来的错误”。如果命令只做语法编译，例如 `python -m compileall -q .`，运行时的 `NameError` 不会出现，也就没有可供修复的失败。可执行脚本应使用 `python 文件名.py`，有测试的项目应使用测试命令。

### 1.1 当前定位与适用范围

AutoFix 0.19.0 是本地优先、安全约束优先的代码修复工程原型，不是无人监管的通用开发代理。当前推荐用于：

- 能由固定命令稳定复现的编译、测试或局部逻辑失败；
- 有版本控制、可审查 diff 的个人项目或小团队内部试用；
- 源码不适合发送到云端、希望使用本地 Ollama 的场景；
- 中小型仓库中受限文件范围内的现有代码修改。

当前不建议直接用于：无测试的大型遗留系统、未审查的第三方构建脚本、生产主分支上的无人值守写入，以及把“测试通过”直接等同于业务正确或安全合格。完整的成熟度边界、下一阶段目标和验收标准见[开发路线图](ROADMAP_ZH.md)。

## 2. 环境要求

### 2.1 基础软件

- Python 3.11 或更高版本；离线包中的 Windows wheelhouse 当前要求 Python 3.11。
- Ollama，并已下载一个本地模型，例如 `qwen3:0.6b`。
- Docker Desktop（使用 `--docker` 或离线镜像功能时需要）。
- Git 不是运行必需项，但强烈建议所有待修复项目都使用 Git。

在 PowerShell 中检查：

```powershell
python --version
docker --version
docker run --rm hello-world
ollama --version
ollama list
```

准备模型：

```powershell
ollama pull qwen3:0.6b
```

### 2.2 从源码安装

在 AutoFix 仓库根目录执行：

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
& .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
autofix --version
autofix doctor
```

`Set-ExecutionPolicy` 是 PowerShell 命令，不能在传统 `cmd.exe` 中执行。在 cmd 中激活虚拟环境应使用：

```bat
.venv\Scripts\activate.bat
```

## 3. 一次配置，所有新项目继承

建议安装完成后只配置一次：

```powershell
autofix configure `
  --model qwen3:0.6b `
  --docker-image autofix-python:3.11 `
  --offline `
  --dependencies-bundled
```

配置保存在：

```text
%USERPROFILE%\.autofix\settings.yml
```

新项目即使没有 `autofix.yml`，也会自动继承 Docker 镜像、Ollama 模型和离线策略；项目自己的构建命令仍由项目文件自动检测。若要使用另一个用户配置目录，可在启动终端前设置 `AUTOFIX_HOME`。

配置优先级从高到低为：

1. 当前命令行参数；
2. 项目根目录的 `autofix.yml` 或 `.autofix.yml`；
3. 用户级 `settings.yml`；
4. 项目自动检测结果。

> `autofix configure` 只保存默认值，不负责安装或启动 Docker Desktop。AutoFix 能在 Windows 常见安装目录寻找 `docker.exe` 和 `ollama.exe`，但 Docker 引擎仍必须处于运行状态。

## 4. 准备专用 Docker 镜像

仓库提供的镜像包含 pytest 和 AutoFix 常用 Python 依赖：

```powershell
docker pull python:3.11-slim
docker build --file docker/python.Dockerfile --tag autofix-python:3.11 .
docker image inspect autofix-python:3.11
```

如果目标项目依赖 PyTorch、NumPy、编译器或私有包，应创建项目专用镜像，并把依赖预装进去。AutoFix 在 Docker 运行时使用 `--pull never` 且默认断网，不会临时下载缺失内容。

例如：

```dockerfile
FROM autofix-python:3.11
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt
```

```powershell
docker build --tag my-project-builder:latest .
autofix configure --model qwen3:0.6b --docker-image my-project-builder:latest --offline --dependencies-bundled
```

## 5. 新项目的推荐工作流

进入项目根目录：

```powershell
cd E:\path\to\project
autofix git-status . --require-clean
autofix inspect .
autofix offline-check .
autofix run . --docker
```

`git-status` 是只读检查，不会执行 checkout、stash、commit 或清理文件。它报告仓库根目录、分支或 detached HEAD、基线 commit，以及 tracked/untracked 变更；`--require-clean` 在非 Git 目录或脏工作区返回退出码 1，适合在 CI 和修复前置步骤中使用。

`repair` 和 `apply-plan` 对 Git 项目强制使用已提交的干净基线：脏工作区或尚无 commit 的仓库会在构建、Ollama 请求和源码修改前停止，AutoFix 不会自动 stash、clean 或提交。干净仓库会从当前 HEAD 创建 detached 临时 worktree，初始失败复现、模型编辑和完整验证都在其中完成；成功预览只输出 plan，成功 `--apply` 才在字节级并发检查后写回已验证目标文件。非 Git 项目继续使用临时目录副本。

建议把 `.autofix/` 加入项目 `.gitignore`。即使尚未加入，repair 策略也只会忽略“当前项目 `.autofix/` 下的未跟踪审计产物”，避免 AutoFix 被自己的报告阻塞；`git-status` 仍会如实显示工作区 dirty。其他未跟踪文件、`.autofix` 中已经被 Git 跟踪的修改，以及仓库其他目录的修改仍会阻断 repair。

如果 `run` 已通过，项目没有需要修复的已知构建错误。如果失败，执行安全预览：

```powershell
autofix repair . --docker --max-attempts 3
```

预览成功时，AutoFix 会输出：

- 模型提议的摘要；
- 每个文件的 unified diff；
- 已在 detached Git worktree（非 Git 项目为临时副本）中验证通过的结论；
- `.autofix\plans\...json` 方案路径；
- `.autofix\patches\...patch` 独立补丁及其 SHA-256；
- 原项目未被修改的提示。

确认 diff 后，应用同一个方案，不再请求模型：

```powershell
autofix apply-plan ".\.autofix\plans\PLAN.json" --project .
```

成功预览和成功应用都会生成普通 unified diff 补丁。需要固定位置供 CI 上传时，使用一个尚不存在的目标文件：

```powershell
autofix apply-plan ".\.autofix\plans\PLAN.json" --project . `
  --patch-output ".\artifacts\verified-repair.patch"
git apply --check ".\artifacts\verified-repair.patch"
```

AutoFix 不覆盖已有 patch。报告同时记录 patch 路径、patch 原始字节 SHA-256 和规范化 diff SHA-256；前者验证文件传输，后者绑定 plan 中的逻辑变更。

若希望验证通过后创建本地提交，必须显式使用 `--commit`：

```powershell
autofix apply-plan ".\.autofix\plans\PLAN.json" --project . `
  --commit `
  --commit-message "fix: apply verified AutoFix repair"
```

提交前会再次检查仓库身份、HEAD、每个目标文件 bytes 和 Git 实际变更集合，只允许提交 plan 声明的现有 tracked 文件。提交使用精确 pathspec，并写入 `AutoFix-Diff-SHA256` trailer；Git hooks 不会被绕过。若 hook、用户身份或并发变更导致提交失败，已经验证的源码会保留供人工检查。该选项不执行 push、merge、stash、clean 或 amend。`repair` 的等价形式是 `autofix repair . --apply --commit`；预览模式拒绝 `--commit`。

plan v4 会自动复用预览时的 Docker runner、镜像、网络策略、资源限制、超时和命令矩阵，并校验 Git 仓库指纹、基线 commit、项目子路径和最终 diff SHA-256。再次传入 `--docker` 只是可选的一致性断言，不再是启用 Docker 所必需。

然后再次独立验证：

```powershell
autofix run . --docker
git diff
```

如果希望一次完成模型修复、验证和写回：

```powershell
autofix repair . --docker --apply --max-attempts 3
```

`repair --apply` 默认仍使用 `--isolate`。Git 项目在 detached worktree、非 Git 项目在临时副本里完成所有尝试；最终通过后才把合并结果写回原项目，写回前还会检查原文件在修复过程中是否被其他程序修改。

## 6. 给单个项目固定配置

自动创建：

```powershell
autofix init . --model qwen3:0.6b
```

如果已经存在配置，AutoFix 会拒绝覆盖。典型内容如下：

```yaml
version: 1
build:
  system: python
  commands:
    - python -m compileall -q src tests
    - python -m pytest
runtime:
  docker_image: autofix-python:3.11
  ollama_model: qwen3:0.6b
  offline: true
  dependencies_bundled: true
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `version` | 配置格式版本，当前为 `1` |
| `build.system` | 多构建系统项目中选择检测到的系统，例如 `python`、`cmake`、`cargo` |
| `build.command` | 真正用于判定成功或失败的单条命令 |
| `build.commands` | 按顺序执行的命令列表；与 `build.command` 二选一 |
| `runtime.docker_image` | 已在本机存在、包含项目依赖的镜像 |
| `runtime.ollama_model` | 已在 Ollama 中存在的模型名 |
| `runtime.offline` | 为 `true` 时禁止通过 `--allow-network` 开启容器网络 |
| `runtime.dependencies_bundled` | 声明镜像已经包含构建所需依赖，供离线检查使用 |

### Python 命令选择

自动检测顺序如下：

- 有 `tests/`、`pytest.ini` 或根目录测试文件：`python -m pytest`；
- 有可识别的根目录入口脚本：`python 脚本.py`；
- 只有被动库模块：`python -m compileall -q .`。

若自动结果不适合项目，直接修改 `build.command`，或临时覆盖：

```powershell
autofix run . --command "python nnModule.py"
autofix repair . --command "python nnModule.py" --max-attempts 3
```

### 构建矩阵

需要多重验证时，使用 `build.commands`：

```yaml
build:
  system: python
  commands:
    - python -m compileall -q src tests
    - python -m pytest
    - python -m build
```

每一步都由无 shell 执行器解析为 argv，按声明顺序运行；第一步失败后立即停止。只有全部步骤退出码均为 0，`run` 才算成功，repair 提议才会被接受。每一步使用相同的 `--timeout` 上限。

也可以临时传入可重复的 `--step`：

```powershell
autofix run . `
  --step "python -m compileall -q src tests" `
  --step "python -m pytest"

autofix repair . `
  --step "python -m compileall -q src tests" `
  --step "python -m pytest" `
  --max-attempts 3
```

`--command` 与 `--step` 互斥。`--step` 会覆盖配置中的 `build.command` 或 `build.commands`。`init` 同样接受多个 `--step`；对于 CMake 这类检测器本身返回多步计划的项目，`init` 会自动写成 `build.commands`。

## 7. 支持的项目检测

| 标记 | 默认命令 |
| --- | --- |
| Python 测试 | `python -m pytest` |
| Python 可执行入口 | `python <entry>.py` |
| 其他 Python | `python -m compileall -q .` |
| `CMakeLists.txt` | 配置后执行构建 |
| `pom.xml` | `mvn test`，Windows 优先 `mvnw.cmd` |
| `build.gradle` / `.kts` | `gradle test`，Windows 优先 `gradlew.bat` |
| `package.json` | npm、pnpm 或 yarn 的 `test`；无 test 时尝试 `build` |
| `Cargo.toml` | `cargo test` |
| `go.mod` | `go test ./...` |

多种构建系统同时存在时，默认使用第一个匹配。可以持久配置或临时选择：

```yaml
build:
  system: cargo
```

```powershell
autofix inspect . --build-system cargo
autofix run . --build-system cargo --dry-run
autofix repair . --build-system cargo --max-attempts 3
```

选择值必须与实际检测结果一致。支持的值为 `python`、`cmake`、`maven`、`gradle`、`npm`、`pnpm`、`yarn`、`cargo` 和 `go`。命令行 `--build-system` 覆盖项目配置；`build.command` 或 `--command` 仍可进一步指定该系统的实际入口。

## 8. 常用命令速查

### 8.1 复杂用例评测

仓库维护者可以用 `autofix-eval.yml` 固定故障、完整验证矩阵和允许修改的文件。先确认用例确实失败：

```powershell
autofix evaluate examples\complex-validation\python-order-service
```

再让本地模型在隔离副本中尝试修复并计分：

```powershell
autofix evaluate examples\complex-validation\python-order-service `
  --repair --model qwen3:0.6b --max-attempts 3
```

`evaluate --repair` 不会修改原用例。0.18.0 起，退出码、失败步骤或输出文本与 `expected_failure` 不符时，不会调用模型；这可以阻止 `ModuleNotFoundError` 等环境错误冒充业务故障。最终构建通过但修改了清单范围外的文件，或者没有命中 `required_change_paths`，仍判为失败。详细清单格式、指标和扩展规范见 [复杂项目评测指南](EVALUATION_ZH.md)。

批量确认一组 fixture 的故障基线时运行：

```powershell
autofix evaluate-suite examples\complex-validation
```

命令递归查找 `autofix-eval.yml`，跳过 `.git`、`.autofix`、虚拟环境、依赖与构建产物目录。全部清单均会执行；单个清单损坏、名称重复、故障签名不匹配或执行异常不会让其余用例消失。默认报告原子写入 `.autofix\evaluation-suites\<UTC>\`，其中包含 `suite.json`、`suite.md` 和 `cases\*.json`。至少一个用例失败时退出 1；找不到用例或无法创建报告时退出 2。`--json --no-save` 可用于只读取 stdout 的自动化检查。

默认模式只验证每个故障契约能否稳定复现，不调用 Ollama，也不表示模型修复成功。批量模型评测使用：

```powershell
autofix evaluate-suite examples\complex-validation `
  --repair `
  --model qwen2.5-coder:7b `
  --max-attempts 3
```

每个有效用例仍通过 `evaluate_project()` 单独进行故障签名 preflight，并在 detached Git worktree 或非 Git 临时副本中修复；原 fixture 不写回。repair 聚合使用 [JSON v3](../schemas/autofix-evaluation-suite-v3.schema.json)，在 v2 的模型、runner、尝试次数和修复率指标上增加上下文召回率、Top-5 召回率、必改文件平均排名、发送文件/字符数、截断率和 fallback 比例；历史 v2 schema 保留，baseline 继续使用 JSON v1。终端表格后的 `Failed case details` 显示完整失败摘要；逐用例 JSON 的 `evaluation.repair_details` 保存 proposal、静态拒绝和每轮验证失败的 diagnosis、diff、构建步骤与完整输出；Markdown 展开同样证据，但单段 diff/输出最多展示 4000 字符，完整内容以 JSON 为准。`--docker` 模式要求所有用例共享一个明确的 `--docker-image` 或套件级配置镜像；当前不会为混合语言用例自动切换多个镜像。

上下文召回以清单的 `required_change_paths` 为真值，只统计实际形成首轮上下文的 repair 用例。`required_context_recall` 表示必改路径是否进入首轮上下文，Top-5 指标进一步检查它是否足够靠前；多轮累计的 `context_files_sent` 与 `context_chars_sent` 用于估算模型输入成本。它们衡量检索质量，不代表模型一定能生成正确修改。

人类可读模式默认启用实时进度：每个用例开始时显示基线复现；请求 Ollama 前显示本轮上下文文件和 `build-output`、`local-import:<来源>` 或 `project-source-fallback` 选择原因；等待 Ollama 和运行验证期间保持动态 spinner；收到 proposal 后显示 summary、diagnosis、目标文件、起始行和替换行数；静态拒绝或验证失败立即输出原因。全部结束后，`Verified repair changes` 会直接打印每个成功用例最终通过验证的 unified diff。自动化不需要动态输出时使用 `--no-progress`；`--json` 会强制静默进度，只在 stdout 输出一个合法 JSON 文档。

0.19.0 起，每个模型候选必须包含可审计的 `diagnosis`。候选通过静态校验但构建失败时，AutoFix 保存本轮 diff、构建输出和新增失败测试，然后把工作区恢复到初始故障状态；下一轮从干净源码重新修复，而不是叠加在错误修改上。完全重复的编辑和恢复到已验证失败状态的循环会被拒绝。CLI 的 `verification failures` 表示“能够应用但未通过构建”的候选数量，它与静态 `rejections` 是不同指标。

当原失败在候选验证后仍存在，重试上下文会明确标注 persistent failure，并要求模型检查失败输入是否能到达被编辑语句。若 diagnosis 指出某个 guard 排除了边界输入，提示要求修改 guard 或更早的可达控制流，而不是在不可达分支内再嵌套同一条件。完全重复候选仍计入模型请求上限；下一轮会收到 `DUPLICATE RECOVERY REQUIREMENT`，并把重试温度从 0.15 有限提高到 0.25，以尝试离开已证伪答案。所有新候选仍必须通过相同静态门禁和完整验证矩阵。

上下文选择并不等于允许修改。AutoFix 会优先发送构建输出直接引用的文件，并有界追踪这些文件能够保守解析到的本地 Python、JavaScript/TypeScript、C/C++ 依赖；因此测试经 `__init__.py` 重导出调用的实现也能提前。Python 的 `src/包名/...` 布局在模块后缀唯一时可识别，存在歧义则退回普通扫描。测试仍默认只读，外部依赖和项目外路径仍被排除。repair JSON 的 `context_selections` 按尝试记录原因、字符数和截断状态，不保存额外的源码副本，可用于判断失败究竟来自模型能力还是上下文缺失。

| 命令 | 用途 | 是否改源码 |
| --- | --- | --- |
| `autofix doctor` | 检查 Python、Git、Docker、Ollama 与 Ollama API | 否 |
| `autofix configure ...` | 保存所有项目继承的用户默认值 | 否；会写用户配置 |
| `autofix inspect .` | 检测项目和候选命令，可用 `--build-system` 选择 | 否 |
| `autofix git-status .` | 只读显示 Git 身份、HEAD 和 tracked/untracked 变更 | 否 |
| `autofix git-status . --require-clean` | 非 Git 或脏工作区返回 1，供 CI/前置门禁使用 | 否 |
| `autofix analyze .` | 只读发现项目已声明的 Ruff、mypy、ESLint、Clippy | 否 |
| `autofix analyze . --run` | 执行已声明且本机可用的分析器，不安装缺失工具 | 否；默认写运行报告 |
| `autofix run . --with-static-analysis` | 在原构建矩阵后追加全部已声明分析器 | 否；缺失任一工具即拒绝 |
| `autofix repair . --with-static-analysis` | 把全部已声明分析器绑定为 repair 验收门禁 | 仅在 `--apply` 后写回 |
| `autofix property-check CANDIDATE --project . --plan PLAN` | 预览 Hypothesis 候选及绑定计划 | 否 |
| `autofix property-check CANDIDATE --project . --plan PLAN --run` | 在原始/修复隔离副本执行差分属性验证 | 否；默认写审计报告 |
| `autofix property-generate . --plan PLAN --model MODEL` | 本地模型生成待审 Hypothesis 候选 | 仅写 `.autofix/property-candidates/`，不执行 |
| `autofix property-promote REPORT --project .` | 把确认候选转换为新测试文件审核 patch | 仅写 `.autofix/property-patches/`，不应用 |
| `autofix init .` | 创建项目配置 | 仅创建配置 |
| `autofix offline-check .` | 检查断网修复所需资源 | 否 |
| `autofix run .` | 执行构建/测试并写运行报告 | 否 |
| `autofix run . --step CMD --step CMD` | 临时运行有序构建矩阵 | 否 |
| `autofix run . --dry-run` | 只打印将执行的命令 | 否 |
| `autofix repair .` | 隔离预览并生成验证过的 plan | 否 |
| `autofix repair . --apply` | 隔离验证成功后写回 | 是 |
| `autofix apply-plan PLAN --project .` | 应用已审核且 hash 未过期的 plan | 是 |
| `autofix ci-check . --output ci-result.json` | CI 无交互验证与稳定 JSON/退出码 | 否 |
| `autofix evaluate-suite PATH` | 批量执行评测故障基线并保存聚合证据 | 否 |
| `autofix evaluate-suite PATH --repair --model MODEL` | 隔离执行批量模型修复并聚合计分 | 否 |
| `autofix evaluate-suite PATH --repair --no-progress ...` | 关闭 spinner 和中间里程碑，仍保留最终人类报告 | 否 |
| `autofix history .` | 查看修复历史 | 否 |
| `autofix undo .` | 撤销最近一次尚未撤销的成功修复 | 是；先备份当前版本 |
| `autofix rollback REPORT --project .` | 按指定报告撤销 | 是；先备份当前版本 |
| `autofix bundle create DIR --project .` | 创建离线运行包 | 不改项目源码 |
| `autofix bundle verify DIR` | 校验离线包清单和 SHA-256 | 否 |
| `autofix bundle install DIR` | 导入 Docker 镜像与 Ollama 模型 | 修改本机运行环境 |

每个命令的完整参数以本机安装版本为准：

```powershell
autofix --help
autofix analyze --help
autofix property-check --help
autofix property-generate --help
autofix property-promote --help
autofix repair --help
autofix apply-plan --help
autofix evaluate-suite --help
autofix ci-check --help
autofix bundle --help
```

静态分析的声明规则、JSON v1 契约、退出状态以及如何通过 `--step` 加入 repair 验收矩阵，见[静态分析使用与安全边界](STATIC_ANALYSIS_ZH.md)。

属性测试候选的 preview、双版本验证、verdict 和 JSON v1 契约，见[属性测试候选协议](PROPERTY_TESTING_ZH.md)。

### CI 无交互验证

```powershell
autofix ci-check . `
  --command "python -m pytest -q --junitxml=pytest-results.xml" `
  --output "$env:TEMP\autofix-ci-result.json"
```

`ci-check` 默认要求干净且已有 commit 的 Git 仓库，stdout 只输出一个 JSON 文档，不调用 Ollama、不修复源码，也不创建 commit。退出码 0/10/20/30/40/70 分别表示通过、验证失败、环境错误、策略拒绝、配置错误和内部错误；构建工具的原始退出码保留在 JSON 的 `build` 中。完整 schema、兼容规则与 GitHub Actions 示例见 [CI JSON 契约](CI_CONTRACT_ZH.md)。

## 9. Docker 隔离参数

常用覆盖项：

```powershell
autofix run . --docker `
  --docker-image my-project-builder:latest `
  --docker-cpus 4 `
  --docker-memory 4g `
  --docker-pids 512 `
  --timeout 600
```

容器执行具有以下约束：

- 项目先复制到临时目录，容器不会挂载原始项目；
- 根文件系统只读；
- 删除全部 Linux capabilities；
- 启用 `no-new-privileges`；
- 限制 CPU、内存和进程数；
- `/tmp` 为受限 tmpfs；
- 默认 `--network none`；
- 镜像只从本机取，不自动 pull；
- `.git`、`.autofix`、缓存、虚拟环境、构建目录、`node_modules`、`target` 和符号链接不会复制。

只有在线构建确实需要下载依赖时才使用 `--allow-network`。若配置中 `runtime.offline: true`，该选项会被拒绝；应先明确修改项目配置，而不是绕过策略。

## 10. 修复安全选项

默认策略：

- Git 仓库必须干净且存在 HEAD commit；不自动 stash、clean、commit、push 或 merge；
- Git 的初始失败复现、模型修改和验证在 detached 临时 worktree 完成；
- 测试文件只作为行为上下文，不允许模型修改；
- 只允许修改项目内已经存在的 UTF-8 源码或受支持配置文件；
- 一次提议最多 20 个行级编辑、最多 5 个文件，总编辑文本最多 200,000 字符；
- 旧文本必须与指定行完全匹配，编辑范围不能重叠；
- 大文件被删除一半以上，或 Python 顶层类/函数被删除时拒绝；
- 只有真实构建/测试通过，才接受结果。

0.16.0 为 Python 顶层函数、异步函数和类的删除保护增加了独立回归测试。即使修改后的文件仍超过原文件一半，只要未显式允许破坏性修改，删除已有顶层定义仍会被拒绝。

以下开关会扩大风险边界，不能作为常规选项：

```powershell
autofix repair . --allow-test-changes
autofix repair . --allow-destructive-changes
autofix repair . --no-isolate
```

`--keep-failed` 仅适用于 `--no-isolate`，会保留验证失败的修改；默认隔离模式中失败副本会被丢弃，因此两者冲突。

若异常退出时 Git 提示 worktree 清理失败，先运行 `git worktree list` 确认只剩失效的 `autofix-worktree-*` 路径，再执行 `git worktree prune`。AutoFix 不会自动全局 prune，以免影响用户创建的其他 worktree。

## 11. 报告、方案和撤销

项目内会产生：

```text
.autofix/
├── runs/               # run 的命令、输出、退出码和耗时
├── repairs/            # repair 的提议、拒绝原因、diff 和验证结果
├── plans/              # 已在隔离副本验证过的可应用方案
├── patches/            # 可单独审查、传输和 git apply 的 unified diff
├── rollback-backups/   # 撤销前的当前文件备份
└── rollbacks/          # 撤销回执
```

方案包含目标文件的完整替换结果、预览时原文件的 SHA-256，以及当时真正通过的有序验证策略。plan v4 继承 v3 的步骤、local/Docker runner、每步超时和 Docker 资源策略，并新增 provenance：Git 仓库指纹、基线 commit、项目子路径与最终 diff SHA-256。仓库指纹是 origin 标识（无 origin 时为根提交集合）的 SHA-256，不保存远端 URL 明文。它用于一致性校验，不是数字签名，能够自行重写 plan 的攻击者仍可重新计算摘要。

若预览后人工编辑文件、推进 HEAD、切换到另一仓库或改变项目根路径，`apply-plan` 会在修改源码前停止。若之后只修改 `autofix.yml`，应用仍使用已审核方案绑定的验证策略，不会静默换成新的、更弱门禁。

可重复传入 `--step` 作为调用方的预期确认；所有 argv 必须与 plan 一致，否则在修改源码前停止：

```powershell
autofix apply-plan ".\.autofix\plans\PLAN.json" --project . `
  --step "python -m compileall -q ." `
  --step "python -m pytest"
```

`apply-plan` 无须重复指定 `--docker` 或资源参数；v4 默认自动复用预览策略。也可以显式传入 `--docker`、`--local`、`--timeout` 或 Docker 资源选项作为一致性断言，值不同就拒绝执行。

旧 plan v1 没有绑定验证步骤；v2 绑定步骤但未绑定 runner；v3 绑定完整执行策略，但没有 Git provenance 与最终 diff 摘要。读取器继续兼容这些格式并保留相应警告；对重要项目建议重新运行 preview 生成 v4 plan。

查看及撤销：

```powershell
autofix history . --limit 20
autofix undo .
autofix rollback ".\.autofix\repairs\REPORT.json" --project .
```

撤销同样进行精确匹配：当前文件必须仍符合报告记录的已应用 diff。撤销前会备份当前文件并写回执。AutoFix 的备份不能代替 Git。

`.autofix/plans` 可能包含项目源码全文，应视为敏感数据；不要未经审查上传到公共仓库。

## 12. 制作和安装离线包

先确保配置、镜像、模型和依赖全部就绪：

```powershell
autofix offline-check .
autofix bundle create dist\autofix-offline --project .
autofix bundle verify dist\autofix-offline
```

输出目录必须不存在。完整 v2 离线包包含：

- AutoFix 及其依赖的 Python wheelhouse；
- `install.ps1`；
- Docker 镜像归档；
- Ollama GGUF 与便携 Modelfile；
- `autofix.yml`；
- README、变更记录、使用和实现文档（从源码仓库创建完整包时）；
- 列出所有载荷大小与 SHA-256 的 manifest。

制作机器需要已经安装满足 `pyproject.toml` 要求的 setuptools。AutoFix 构建自身 wheel 时不创建需要联网下载构建工具的隔离环境；若运行依赖尚未进入本机 pip 缓存，仍应在断网前完成 bundle 创建。

在目标 Windows 机器预先安装 Python 3.11、Docker Desktop 和 Ollama，然后在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& "D:\transfer\autofix-offline\install.ps1"
autofix doctor
```

也可在已经安装 AutoFix 的机器上只导入镜像和模型：

```powershell
autofix bundle install D:\transfer\autofix-offline
```

`bundle install` 不安装 wheelhouse；完整首次安装请使用 `install.ps1`。所有载荷在导入前都会先验证 manifest。模型权重和项目配置可能受许可或保密要求约束，复制前应确认授权。

## 13. 常见问题

### `docker` is not recognized

当前终端没有拿到 Docker CLI 路径，或者 Docker Desktop 尚未安装。先启动 Docker Desktop，再关闭并重新打开 VS Code/终端。检查：

```powershell
where.exe docker
docker version
docker info
```

若 Windows 的普通 cmd 可以运行而 VS Code PowerShell 不行，说明 VS Code 进程仍持有旧 PATH；完全退出 VS Code 后重新打开。AutoFix 也会尝试 Docker Desktop 的标准安装路径。

### Docker 客户端存在，但连接不到 daemon

这不是 PATH 问题。启动 Docker Desktop，等待状态变成 Running，再执行 `docker run --rm hello-world`。公司策略、WSL 2 或虚拟化未启用时，应先修复 Docker Desktop 本身。

### `Set-ExecutionPolicy` is not recognized

你正在 cmd 中运行 PowerShell 命令。打开 PowerShell，或者执行：

```bat
powershell -ExecutionPolicy Bypass -File install.ps1
```

### `The project already passes; no repair was requested`

当前 `build.command` 返回成功，所以 AutoFix没有失败证据。先手工执行同一命令。如果真正错误只在运行脚本时出现，将命令从 `compileall` 改为入口脚本或测试命令。

```powershell
autofix run . --dry-run
autofix run . --command "python nnModule.py"
autofix repair . --command "python nnModule.py" --max-attempts 3
```

如果使用 `build.commands`，请检查每个步骤是否真正覆盖目标代码。AutoFix 会在第一项失败时停止，所以把快速、确定的静态检查放前面，把较慢的完整测试放后面。

### 缺少依赖、编译器或运行时组件时不 repair

这是有意设计：缺少安装依赖或工具链属于运行环境问题，模型改源码通常会制造错误。0.15.0 的高置信度门禁覆盖：

- Python 包和原生 DLL；
- `package.json` 已声明但运行环境缺失的 Node.js 包；
- npm 等脚本内部缺失的命令；
- CMake 找不到 C/C++ compiler 或 Ninja 等生成工具；
- Go/cgo 找不到 C compiler；
- Rust linker 缺失或 Cargo 离线缓存不完整；
- `JAVA_HOME`、JDK compiler 和目标 release 不匹配；
- Docker daemon 不可用或绑定镜像不存在。

把依赖或工具加入本地环境/专用 Docker 镜像，再重试。相比之下，`NameError: name 'torch' is not defined` 是代码中缺少 import，允许进入修复流程；Node 相对模块缺失或未声明包也不会仅凭错误文本被当成环境故障，因为它可能是源码路径或拼写错误。

同样地，构建命令返回 127 时表示工具不存在。例如缺少 `cargo`、`mvn` 或 `gradle`，AutoFix 会记录报告并在调用模型前停止。应安装工具链、加入项目 wrapper，或使用包含它的 Docker 镜像。

环境门禁触发时可在最新 `.autofix\repairs\*.json` 中确认：`proposals` 应为空，`error` 会给出建议动作。若普通源码错误被误分类，请保留原始构建输出作为规则负向测试，不要用 `--allow-destructive-changes` 绕过；该开关与环境分类无关。

### Windows Node 项目提示找不到 npm

0.12.0 起自动检测在 Windows 使用 `npm.cmd`、`pnpm.cmd` 或 `yarn.cmd`，避免无 shell 执行器误选 PowerShell shim。旧项目配置若写的是 `npm test`，可以改为：

```yaml
build:
  system: npm
  command: npm.cmd test
```

### Windows 上 plan 或 undo 因换行变化失效

0.12.0 起源码读写禁用通用换行转换，LF 与 CRLF 都按原始字节风格保留。应用后 undo 应恢复与修复前相同的 SHA-256。旧版本已经改写过换行的文件无法让旧 plan hash 重新匹配，需要重新生成预览 plan。

### `No safe repair proposal` / `Destructive replacement rejected`

模型多次给出空修改、错误行号、整文件覆盖、修改测试或删除过多代码，安全校验拒绝了它。原项目没有被修改。可尝试：

- 确保构建输出清楚指向错误文件和行号；
- 使用更适合代码的本地模型；
- 增加到 `--max-attempts 3` 或最多 5；
- 缩小项目或提供更精准的 `--command`；
- 只有确认确实要改测试/做大范围重构时，才启用相应风险开关。

### 预览有 diff，但没有 plan

提议没有通过真实构建验证。AutoFix 不会为失败状态创建 plan，原项目保持不变。查看 `.autofix/repairs` 中的最后报告和验证输出。

### pytest 显示 warnings，但命令 passed

只要退出码为 0，warnings 不代表构建失败。可以单独升级相关依赖消除警告，不影响 AutoFix 判定。

### 修改了错误文件或结果不符合预期

立即检查 `git diff`。若是 AutoFix 已应用的最近修复：

```powershell
autofix undo .
```

不要手工删除剩余代码来“补救”；先撤销到可验证状态，再改进构建命令或换模型重新预览。

## 14. 推荐实践

1. 项目先提交 Git，工作区尽量干净。
2. 让 `build.command` 覆盖真实故障，而不只是语法检查。
3. 为项目制作包含全部依赖的固定版本 Docker 镜像。
4. 日常使用“预览 → 检查 diff → apply-plan → 独立 run → git diff”。
5. 保持测试文件默认只读，不把“改测试让它通过”当作代码修复。
6. 不对不可信仓库执行自定义构建命令；构建脚本本身就是可执行代码。
7. 将 `.autofix` 报告保留到审查完成，但对 plan 和日志按源码敏感级别管理。
8. 重要项目始终使用 Git；AutoFix 的隔离、hash 和撤销是第二道防线，不是版本控制替代品。
9. 把 AutoFix 视为“生成并验证候选补丁”的工具；合并前仍需代码审查、项目自己的安全检查和独立回归。

跨语言真实验证过程和当前工具链边界见[多语言闭环验证报告](MULTILANG_VALIDATION_ZH.md)。

