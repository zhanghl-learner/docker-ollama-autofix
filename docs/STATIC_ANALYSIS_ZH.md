# 静态分析使用与安全边界

`autofix analyze` 用于发现项目已经声明的静态分析器，并在用户明确添加 `--run` 时执行本机已经可用的工具。它不会安装依赖、创建配置、修改源码，也不会把“工具缺失”误报为代码缺陷。

## 1. 发现与执行

只读发现：

```powershell
autofix analyze .
autofix analyze . --json
```

显式执行所有已声明且可用的分析器：

```powershell
autofix analyze . --run
autofix analyze . --run --no-save
```

默认发现模式不会启动任何外部命令。`--run` 仍通过 AutoFix 的无 shell runner 顺序执行固定 argv，遇到首个失败即停止；默认报告写入 `.autofix/runs/`，`--no-save` 可关闭落盘。

## 2. 当前支持范围

| 分析器 | 声明依据 | 本机可用性 | 固定命令 |
| --- | --- | --- | --- |
| Ruff | `ruff.toml`、`.ruff.toml`、`[tool.ruff]` 或 Python 依赖声明 | 项目 `.venv` 或 PATH | `ruff check .` |
| mypy | `mypy.ini`、`.mypy.ini`、`setup.cfg`、`[tool.mypy]` 或 Python 依赖声明 | 项目 `.venv` 或 PATH | `mypy .` |
| ESLint | `package.json` lint script、ESLint 依赖或常见 ESLint 配置 | 本地 `node_modules/.bin/eslint` 存在；执行 lint script 时还要求对应包管理器在 PATH | 优先执行已声明 lint script，否则执行本地 `eslint .` |
| Clippy | `clippy.toml`、`.clippy.toml`、`[lints.clippy]` 或 `[workspace.lints.clippy]` | `cargo` 与 `cargo-clippy` 均在 PATH | `cargo clippy --all-targets` |

检测有意保守。例如全局安装了 Ruff、但项目既没有配置也没有依赖声明时，不会自动运行；Yarn PnP 等没有本地 ESLint 可执行文件的布局目前显示 unavailable。用户仍可通过已有的 `--step` 明确提供命令。

## 3. JSON 契约与退出状态

`--json` 输出遵循 [`autofix-static-analysis` v1 Schema](../schemas/autofix-static-analysis-v1.schema.json)，包含：

- 声明和可用分析器数量；
- 每个分析器的语言、声明来源、固定 argv 和可用性原因；
- 是否请求执行；
- 执行后的完整 `BuildResult`，包括 stdout、stderr、退出码和耗时。

状态规则：

- 只读发现成功返回 0，包括没有声明分析器；
- `--run` 但没有任何可用分析器返回 2；
- 分析器通过返回 0；
- 分析器失败时返回其非零退出码，并保留输出。

## 4. 把静态分析加入构建与修复验收

当前版本不会悄悄改变项目的默认 build plan。先运行 `autofix analyze .` 审核打印出的命令，然后使用便捷绑定：

```powershell
autofix run . --with-static-analysis
autofix repair . --with-static-analysis --model qwen2.5-coder:7b --max-attempts 3
```

静态分析步骤会追加在原构建/测试步骤之后，候选修改只有在完整矩阵全部通过时才会被接受。绑定采用严格策略：没有声明受支持分析器，或任一已声明分析器在本机不可用，命令都会返回配置错误，不会只运行部分工具造成虚假的绿色结果。相同 argv 的显式步骤会被去重。

`run` 可以直接使用项目 `.venv` 和本地 `node_modules`。`repair` 会在 detached worktree 中验证，而这些通常被 Git 忽略的目录不会复制过去，因此便捷绑定只接受 PATH 可用的 Python/Rust 工具；请先激活 Python 虚拟环境。项目本地 ESLint 应改用包含依赖的 Docker 镜像和显式 `--step`。这个检查发生在模型调用之前，避免把隔离环境缺依赖误判为代码修复失败。

便捷绑定目前只支持本机 runner，因为主机上的工具可用性不能证明 Docker 镜像中存在同一工具和配置。Docker 场景继续使用审核过的显式步骤：

```powershell
autofix repair . `
  --docker `
  --step "ruff check ." `
  --step "python -m pytest" `
  --model qwen2.5-coder:7b `
  --max-attempts 3
```

plan 会记录展开后的完整验证命令，因此后续 `apply-plan` 仍执行命令一致性与 provenance 检查，不依赖重新发现工具。

仓库提供 `examples/static-analysis/python-unused-import`：Python 编译能够通过，但 Ruff `F401` 失败，用于复现“可运行不等于通过静态质量门禁”。AutoFix 不会为该样例安装 Ruff。

## 5. 与逻辑错误和属性测试的关系

静态分析擅长发现类型、未定义名称、无效 API 使用、可疑控制流和生态特定 lint，但不能证明业务规则正确。属性测试适合补充边界值、不变量和状态转换，不过自动生成测试本身也可能错误。

下一阶段的属性测试采用以下边界：

1. 只在 detached worktree 或临时副本生成；
2. 先输出测试候选和生成依据，不直接写入用户测试目录；
3. 要求候选测试在原始代码上稳定失败，并在修复代码上稳定通过；
4. 生成测试不计为修复成功，生产代码仍必须通过原有完整矩阵；
5. 不自动安装 Hypothesis、fast-check 等依赖。

这保证静态分析和属性测试增加的是可审计失败信号，而不是绕过现有安全门禁的新写入渠道。
