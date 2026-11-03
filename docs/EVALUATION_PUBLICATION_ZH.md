# 真实模型评测审核与版本化发布

`evaluate-suite --repair` 的原始 `suite.json` 是私有审计证据：其中可能包含本机绝对路径、构建输出、模型 diagnosis、源码 diff 和临时报告位置，不适合未经审核直接提交到公开仓库。`autofix evaluation-publish` 建立独立的人工审核与公开发布边界。

## 1. 发布前提

当前 publication v1 只接受 repair suite v5，并重新验证：

- 冻结 `autofix-benchmark.yml` 完整覆盖、来源、许可证和源码树 SHA-256；
- suite 的 `relative_root → contract_sha256` 与当前每份 `autofix-eval.yml` 逐字节一致；
- case 名称、语言和难度仍与清单一致；
- Ollama 模型 digest 已解析，不接受只有模型标签的报告；
- runner 为 local，且 suite 中声明语言所需的 Go/Node/编译器工具链均有真实版本；
- case 计数、通过数、失败数、通过率、suite passed 状态相互一致；
- 至少产生过一个模型 proposal，不能把未调用模型的环境失败冒充模型评测；
- 失败 suite 必须额外提供 `--accept-failed`。失败是有价值的基线，但不能被默认发布。

当前 Docker 报告使用 `container-unresolved`，因此不能发布为正式基线。后续只有加入镜像 digest 和容器内工具链证据后才会开放。

## 2. 两阶段操作

先运行真实评测并找到生成的 `suite.json`：

```powershell
autofix evaluate-suite .\examples\complex-validation `
  --repair `
  --model qwen2.5-coder:7b `
  --max-attempts 3
```

预览审核结果，不写文件：

```powershell
autofix evaluation-publish ".\.autofix\evaluation-suites\<UTC>\suite.json" `
  --benchmark .\examples\complex-validation `
  --release "2026.11-qwen25-coder-7b" `
  --reviewed-by "your-name" `
  --review-note "Reviewed model identity, per-case outcomes, diffs, and failures."
```

终端会打印原始 suite SHA-256。人工确认原始 JSON 中的逐轮 proposal、diff 和构建证据后，使用同一摘要显式发布：

```powershell
autofix evaluation-publish ".\.autofix\evaluation-suites\<UTC>\suite.json" `
  --benchmark .\examples\complex-validation `
  --release "2026.11-qwen25-coder-7b" `
  --reviewed-by "your-name" `
  --review-note "Reviewed model identity, per-case outcomes, diffs, and failures." `
  --publish `
  --expected-suite-sha256 "<预览打印的 64 位摘要>" `
  --output-root .\benchmarks\evaluation-results
```

目标为：

```text
benchmarks/evaluation-results/<benchmark-name>/<release>.json
```

同一 release 不可覆盖。若需要修订审核，使用新的 release 标识，不修改历史文件。命令不会执行 Git commit 或 push。

## 3. 公开文件包含什么

[`autofix-evaluation-publication` v1](../schemas/autofix-evaluation-publication-v1.schema.json) 保存：

- release、审核时间、审核人、审核说明，以及是否显式接受失败 suite；
- 冻结清单摘要、每个 fixture 的来源/commit/license/tree SHA；
- 原始 suite 的 SHA-256、AutoFix 版本、模型 digest/量化、runner 与工具链身份；
- 聚合指标；
- 每个 case 的相对路径、契约摘要、语言、难度、状态和通过结果。

它不会复制原始 `root`、`report_path`、完整 error、模型输出、源码 diff 或构建日志。原始 suite 应作为私有 CI/release artifact 保存；公开文件通过 `source_suite_sha256` 与它绑定，但不能从公开文件恢复私有内容。

## 4. 信任边界

publication 是“结构校验 + 内容摘要 + 人工声明”，不是数字签名，也无法证明审核人真实身份。能够同时篡改原始 suite 和发布文件的人仍可伪造证据。因此公开结论应同时依赖 Git 历史、CI artifact 保留策略和代码审查；未来可增加签名或构建来源证明，但不会用一个自算 SHA 冒充可信签名。
