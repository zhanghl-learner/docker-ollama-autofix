# AutoFix 复杂项目评测指南

本文对应 AutoFix 0.19.0，说明如何把不同语言的故障项目做成可重复、可比较、不会污染原样例的评测用例。

## 1. 为什么需要独立评测契约

普通 `autofix repair` 的目标是修复当前项目；`autofix evaluate` 的目标是回答四个不同问题：

1. 用例是否真的能稳定复现失败；
2. 模型建议能否通过完整验证矩阵；
3. 模型是否只修改预期的生产文件；
4. 相同清单与模型在后续版本中是否退化。

因此评测清单与日常 `autofix.yml` 分离。前者描述验收真值和修改范围，后者描述项目运行配置。

## 2. 快速开始

先验证仓库自带的故障基线：

```powershell
autofix evaluate examples\complex-validation\python-order-service
autofix evaluate examples\complex-validation\node-checkout-service
```

绿色的 `Baseline is valid` 表示声明的命令如期失败。它不是修复成功。然后运行真实本地模型评测：

```powershell
autofix evaluate examples\complex-validation\python-order-service `
  --repair --model qwen3:0.6b --max-attempts 3
```

该命令内部固定使用 `apply=false` 和 `isolate=true`。成功方案只用于计分，不会写回 fixture。需要容器工具链时添加 `--docker --docker-image <image>`；容器默认断网。

若 fixture 位于 Git 仓库中，`--repair` 要求整个仓库已提交且工作区干净，并从当前 HEAD 创建 detached 临时 worktree；脏仓库和尚无 commit 的仓库会在 Ollama 修复前停止。纯 baseline 评测仍只复现契约，不进入 repair worktree。非 Git fixture 继续使用临时目录副本。该策略确保批量评测不会通过自动 stash、clean 或覆盖来隐藏维护者的本地修改。

## 3. 清单格式

每个用例根目录放置 `autofix-eval.yml`：

```yaml
version: 1
name: python-order-service-boundary
language: Python
difficulty: intermediate
commands:
  - python -m compileall -q src tests
  - python -m unittest discover -s tests -v
expected_failure:
  exit_code: 1
  failed_step: 2
  output_contains:
    - test_bulk_discount_starts_at_ten_units
    - "AssertionError: 2500 != 2250"
  output_excludes:
    - ModuleNotFoundError
allowed_change_paths:
  - src/order_service/pricing.py
required_change_paths:
  - src/order_service/pricing.py
tags:
  - multi-file
  - boundary-condition
```

字段含义：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `version` | 是 | 当前只能为 `1` |
| `name` | 是 | 稳定、可读的用例标识 |
| `language` | 是 | 报告分组标签，不改变执行器 |
| `difficulty` | 是 | 建议使用 `basic`、`intermediate`、`advanced` |
| `commands` | 是 | 无 shell 执行的有序验证矩阵，遇到首个失败即停止 |
| `expected_failure` | 否，但复杂用例强烈建议 | 预期非零退出码、1-based 失败步骤、必含/禁含输出；至少声明一项 |
| `allowed_change_paths` | 是 | 模型最终方案允许触及的现有文件 |
| `required_change_paths` | 否 | 成功方案必须触及的文件，且必须是 allowed 的子集 |
| `tags` | 否 | 例如 `multi-file`、`boundary-condition`、`concurrency` |

`expected_failure.output_contains` 和 `output_excludes` 都是大小写敏感的精确子串数组；同一文本不能同时出现在两者中。`failed_step` 从 1 开始，且不能超过 `commands` 数量。未声明 `expected_failure` 的旧 v1 清单仍兼容，但只能判断“是否失败”，不适合严谨基准。

v1 有意只支持精确文件路径，不支持 glob。这样修改范围没有 `src/**` 一类的解释歧义。

## 4. 通过条件与指标

基线模式要求初始验证失败，并且满足 `expected_failure` 的所有断言。初始即通过说明 bug 已消失、测试失效或 fixture 被污染；失败原因不匹配则通常意味着环境、依赖或用例启动方式有误。两种情况都退出 1。

repair 模式必须同时满足：

- 初始验证失败；
- 初始失败签名匹配；不匹配时模型调用次数必须为 0；
- 隔离副本中的最终完整矩阵通过；
- `unexpected_change_paths` 为空；
- `missing_required_paths` 为空。

JSON 的 `metrics` 还包含提议数、静态拒绝数、验证失败候选数、实际修改路径及验证步骤数。报告默认写入 `.autofix/evaluations/<UTC>.json`，对应的 repair 细节写入 `.autofix/repairs/`。`contract_sha256` 用于确认比较对象没有变化。

`rejection_count` 表示候选在构建前被拒绝，例如 no-op、旧文本不匹配、重复编辑或修复循环；`verification_failure_count` 表示候选可安全应用，但应用后的完整矩阵仍失败。后者的 repair 报告会保存模型 diagnosis、真实 diff、新增失败测试和构建输出。失败候选随后从隔离工作区撤销，下一轮始终面对同一初始源码。

无安全候选、明确环境错误或 Ollama 协议/连接错误也属于评测结果。命令会退出 1，但仍保存 evaluation JSON；若 repair 已产生审计报告，评测报告会引用它并从中保留实际提议数和拒绝数。清单格式错误、Docker 配置错误等无法开始评测的问题退出 2。

机器读取示例：

```powershell
autofix evaluate . --repair --json
```

不要把 mock client 的单元测试结果与真实 Ollama 分数混在一起。发布报告必须写明模型、工具链、runner、镜像与日期。不同模型大小也必须分开统计；本项目的复杂边界用例中，`qwen3:0.6b` 未通过，而 `qwen2.5-coder:7b` 在 0.19.0 重试协议下完成 Python 2/2-attempt 和 Node 1/1-attempt 修复。

## 5. 复杂用例设计准则

高价值用例应满足：

1. 至少有一个跨文件调用链，避免仅凭报错行机械替换；
2. 每个行为至少覆盖正常值、边界值与一个相邻值，阻止硬编码单个断言；
3. 验证矩阵包含语法/编译检查和行为测试；成熟项目再加入 formatter、linter、类型检查；
4. 测试文件不放入 `allowed_change_paths`，除非用例就是测试修复；
5. 故障保持最小且有唯一意图，避免一个用例同时混入环境缺失和多个无关 bug；
6. 不依赖网络。第三方依赖应锁定并放入专用 Docker 镜像或离线缓存；
7. 初始故障、正确修复和预期修改范围都应先由维护者人工确认。

## 6. 下一阶段：从样例验证到规模化评测

当前 Python 和 Node 多文件用例证明了 v1 契约能够区分有效基线、环境漂移、越界修改和真实修复，也记录了 `qwen3:0.6b` 与 `qwen2.5-coder:7b` 的能力差异。但两个复杂用例不能代表任意真实项目，更不能单独支撑生产成功率结论。

第一段聚合能力已经实现：

```powershell
autofix evaluate-suite examples\complex-validation
autofix evaluate-suite examples\complex-validation --json --no-save
```

命令按稳定路径顺序递归发现清单，执行所有 baseline 契约，并保留通过、契约不匹配、无效清单、执行异常与重复名称。默认一次性发布 `.autofix/evaluation-suites/<UTC>/suite.json`、`suite.md` 和逐用例 JSON；发布采用同目录暂存后原子重命名，避免中断留下看似完整的半份聚合报告。JSON 遵循 [`autofix-evaluation-suite` v1 schema](../schemas/autofix-evaluation-suite-v1.schema.json)，包含总数、通过/失败数、错误分类、通过率、语言/难度分组、每份契约 SHA-256 与耗时。

默认命令只做确定性 baseline，不调用模型。它回答“套件是否仍能稳定复现声明的故障”，不能回答“模型能否修好”。baseline 报告继续遵循 v1，保证第一段聚合结果的含义不变。

第二段已经加入隔离 repair 聚合：

```powershell
autofix evaluate-suite examples\complex-validation `
  --repair `
  --model qwen2.5-coder:7b `
  --max-attempts 3
```

每个清单先独立匹配 `expected_failure`，不匹配时该用例的模型调用数保持为 0。匹配后复用单用例的 detached Git worktree/临时副本、路径策略、精确旧文本、破坏性检查与完整验证矩阵；无论成功或失败都不写回 fixture。repair 聚合遵循 [`autofix-evaluation-suite` v2 schema](../schemas/autofix-evaluation-suite-v2.schema.json)，增加 `autofix_version`、`model`、`runner`、`max_attempts` 和以下指标：

| 指标 | 含义 |
| --- | --- |
| `repair_eligible` | 初始故障签名匹配、具备修复资格的有效用例数 |
| `repair_attempted` | 至少得到一个模型 proposal 的用例数 |
| `model_not_attempted` | 有效 repair 结果中 proposal 数为 0 的用例，包括 preflight/环境门禁 |
| `first_attempt_pass_rate` | 首个 proposal 无静态拒绝、无验证失败并最终通过的用例数 / attempted |
| `average_attempts` | proposal 总数 / attempted；不是成功用例专属平均数 |
| `static_rejection_rate` | 静态拒绝数 / proposal 总数 |
| `verification_failure_rate` | 可应用但构建失败的候选数 / proposal 总数 |
| `unexpected_change_rate` | 出现越界修改的用例数 / attempted |
| `missing_required_change_rate` | 未触及必改文件的用例数 / attempted |

分母为 0 时比例固定为 `0.0`，同时保留原始计数，使用者不能把 0% 误读为已经发生过成功评测。模型连接/协议失败、环境门禁和无安全候选仍作为失败用例保留；`--no-save` 只禁止落盘，不会清空静态拒绝与验证失败的内存统计。

### 6.1 失败证据与报告层级

repair 用例失败不一定存在异常：候选可能通过全部静态门禁，却没有修复行为测试。此时终端 `Error` 不能留空。套件会生成一条稳定摘要，例如“3 proposals passed static checks but failed verification；最后失败步骤及退出码”，并在表格后用 `Failed case details` 完整输出，避免 Rich 表格宽度截断关键信息。

长时间运行时，CLI 通过结构化 progress event 展示当前状态，而不是猜测子进程输出：

1. `[case/total] ... reproducing`：读取清单并执行故障基线；
2. `preparing isolation`：创建 detached worktree/临时副本并确认隔离基线；
3. `attempt N/M, waiting for Ollama`：模型 HTTP 请求进行中，spinner 会持续刷新；
4. `proposal`：显示 summary、diagnosis 及编辑文件/行号；
5. `verifying`：候选已通过静态校验，正在运行完整命令矩阵；
6. `failed verification`、`rejected` 或 `verified successfully`：本轮确定性结果；
7. 汇总后的 `Verified repair changes`：打印最终被真实构建接受的 unified diff。

`--no-progress` 只关闭中间事件，不改变修复、报告或最终 diff。`--json` 自动关闭所有人类进度和 diff，保证 stdout 可直接交给 JSON 解析器。

保存的证据分为两层：

- `suite.json` 与 `cases/*.json`：`evaluation.repair_details` 保存完整 `RepairResult`，包括 proposal 元数据、静态拒绝、每轮 diagnosis、实际 unified diff、完整 `BuildResult` 和 stdout/stderr；
- `suite.md`：为每个失败 repair 展开尝试编号、原因、diff、失败步骤和输出；单个 diff 或输出超过 4000 字符时截断，并提示查看 JSON。

这些证据直接来自内存中的隔离修复结果。`evaluate-suite` 仍以 `save_report=False` 调用单用例评测，所以不会为了取得细节而在每个 fixture 下额外生成 `.autofix/repairs`；最终只原子发布套件目录。静态拒绝与环境错误若已经形成结构化 repair 结果，也通过同一字段保留。原 fixture 源码仍逐字节不变。

后续评测工作按以下顺序推进：

1. [x] 实现一次运行多个清单的 baseline 聚合命令，保留每个原始结果并固定 JSON schema v1；
2. [x] 在套件中加入隔离模型 repair，汇总首次通过率、平均尝试数、静态拒绝率、验证失败率、越界修改率和耗时；
3. 每个主要生态至少加入基础、跨文件和环境门禁三类用例，并保留模型不能解决的用例；
4. 增加来自真实开源仓库的最小化、许可兼容故障，同时记录上游来源和简化过程；
5. 在 CI 中运行不调用真实模型的契约/安全回归；真实模型评测单独运行并记录硬件、模型 digest、工具链和日期；
6. 比较版本时要求 `case.name` 与 `contract_sha256` 均一致，避免把清单变化误判为能力提升。

聚合报告必须同时展示失败和拒绝，不能只展示成功案例。达到多语言、重复运行和版本回归的最低规模前，项目对外表述应使用“工程原型/内部试用”，而不是“可无人值守修复任意项目”。

## 7. 新语言扩展顺序

下一批建议按下列顺序加入，每个语言至少包含一个复杂成功用例和一个环境门禁用例：

| 阶段 | 语言/生态 | 重点场景 |
| --- | --- | --- |
| A | Go | 多 package、table-driven tests、错误传播 |
| B | Java | 多 package、接口实现、javac/Maven 两类 runner |
| C | C++ | 多 translation unit、CMake configure/build/test |
| D | Rust | module/trait、Cargo offline、borrow/type errors |
| E | Python/Node advanced | async、类型检查、锁定依赖、lint/format gate |

模型 repair 聚合已经实现，跨版本排行榜和模型 digest 尚未实现。比较结果时必须同时使用 `case.name + contract_sha256 + model + runner`，不要把 baseline suite 通过率当作模型成功率，也不要只比较“最终是否通过”；越界修改与缺失必改文件同样是失败。具体版本里程碑见[开发路线图](ROADMAP_ZH.md)。
