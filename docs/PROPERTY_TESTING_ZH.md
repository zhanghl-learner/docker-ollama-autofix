# 属性测试候选协议与安全边界

`autofix property-check` 用于审核并差分验证一个 Python/Hypothesis 测试候选。当前阶段不让模型直接写入项目测试目录，也不把“生成了测试”当作修复成功；它只回答一个更窄、可审计的问题：候选反例是否能稳定区分原始版本和已审核的修复计划。

## 1. 两阶段操作

如果已经有人工或外部工具创建的候选，可以直接从下面的 preview 开始。也可以让本地 Ollama 依据已审核 plan 的 before/after 修改生成一个待审候选：

```powershell
autofix property-generate .\my-project `
  --plan .\my-project\.autofix\plans\PLAN.json `
  --model qwen2.5-coder:7b
```

生成命令只向本地模型提供 plan 摘要及被修改文件的 before/after 内容。模型响应必须匹配固定 JSON schema，候选还要通过 64 KiB、Python AST、Hypothesis、`@given`、顶层 `test_*` 和明显副作用能力检查。合法文件只保存到 `.autofix/property-candidates/`，同时保存不含源码副本的元数据；它不会执行候选，也不会写入项目 `tests/`。

先预览，不执行候选：

```powershell
autofix property-check .\candidate_property.py `
  --project .\my-project `
  --plan .\my-project\.autofix\plans\PLAN.json
```

终端会显示候选源码、SHA-256、隔离目标路径和绑定的 plan。确认候选代码可信后再显式执行：

```powershell
autofix property-check .\candidate_property.py `
  --project .\my-project `
  --plan .\my-project\.autofix\plans\PLAN.json `
  --run
```

`pytest` 与 `hypothesis` 必须已经安装在当前环境；AutoFix 不会安装它们。默认报告写入项目的 `.autofix/property-checks/`，`--no-save` 可以关闭。

## 2. 接受协议

执行模式建立两个临时项目副本，并把候选放在临时的 `.autofix_property_candidates/` 下：

1. 原始副本连续执行候选两次；两次都必须失败；
2. 两次失败的规范化输出摘要必须相同，避免接受明显不稳定的反例；
3. 在另一个副本应用 plan 中经过校验的生产代码修改；
4. 修复副本连续执行候选两次；两次都必须通过；
5. 再执行 plan 绑定的原验证矩阵；全部通过后才得到 `counterexample-confirmed`。

任何一步不满足都会返回非成功 verdict，并且不会修改原项目：

| Verdict | 含义 |
| --- | --- |
| `preview` | 仅审核，未执行候选 |
| `counterexample-confirmed` | 原始版本稳定失败，修复版本和原矩阵稳定通过 |
| `baseline-not-failing` | 候选不能在原始版本复现问题 |
| `baseline-unstable` | 两次原始失败证据不一致 |
| `fixed-candidate-failed` | 候选在修复版本仍失败 |
| `fixed-verification-failed` | 候选通过，但修复版本破坏了 plan 原验证矩阵 |

## 3. 候选与 plan 限制

候选必须满足：

- UTF-8 Python 文件，最大 64 KiB；
- 导入 Hypothesis；
- 使用 `@given`；
- 至少定义一个顶层 `test_*` 函数；
- 不是符号链接；
- 不导入 `os`、`subprocess`、`socket`、`requests`、`httpx`、`urllib`、`pathlib`、`shutil`、`tempfile` 等明显副作用模块；
- 不直接调用 `open`、`exec`、`eval`、`compile` 或 `__import__`。

当前只接受带验证步骤、使用本机 runner 的 repair plan。加载 plan 时会重新检查源文件哈希、修改内容和已有 provenance；属性测试不能成为绕过 plan 审核的第二条写入通道。候选本身是可执行代码，因此默认 preview 是重要的人工安全边界。

## 4. JSON 契约和退出码

`--json` 遵循 [`autofix-property-check` v1 Schema](../schemas/autofix-property-check-v1.schema.json)，保存候选摘要、固定 argv、每轮隔离输出、失败签名、最终 verdict 和报告路径，不重复保存候选源码。

`property-generate --json` 遵循 [`autofix-property-candidate` v1 Schema](../schemas/autofix-property-candidate-v1.schema.json)，明确记录模型、plan、候选摘要、元数据路径以及 `executed: false`。

- preview 成功：退出 0；
- `counterexample-confirmed`：退出 0；
- 差分协议未确认：退出 1；
- 候选、依赖、plan 或策略错误：退出 2。

## 5. 固定样例

仓库的 `examples/property-testing/python-absolute` 包含错误实现、项目外的 Hypothesis 候选和哈希绑定的本机 repair plan。生成能力只负责提出待审候选，不代表候选可信或修复成功。所有生成结果仍必须经过人工 preview 和本页的双版本验收门禁。

## 6. 把已确认候选晋升为审核 patch

只有保存为 JSON 且 verdict 为 `counterexample-confirmed` 的报告可以晋升：

```powershell
autofix property-promote .\my-project\.autofix\property-checks\REPORT.json `
  --project .\my-project
```

晋升时会重新检查项目根、报告保存路径、两次 baseline 失败签名、两次 fixed 通过、原矩阵通过、候选 SHA、plan 哈希/provenance 和目标路径。默认目标是 `tests/test_autofix_property_<sha>.py`；也可以使用 `--target` 指定一个尚不存在且能被识别为测试文件的项目相对 `.py` 路径。

输出位于 `.autofix/property-patches/`，包含 Git-compatible new-file patch 和 JSON 元数据。元数据固定记录 `applied: false`；命令不会创建测试文件，也不会调用 `git apply`。人工检查后可先执行：

```powershell
git apply --check ".\.autofix\property-patches\PATCH.patch"
```

是否真正应用、提交仍由开发者决定。这样生成模型、差分验证和持久测试写入保持三个独立权限阶段。
