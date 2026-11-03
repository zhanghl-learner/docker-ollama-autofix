# 冻结评测基准与趋势比较

AutoFix 的修复成功率只有在“同一批用例、同一故障契约、同一源码版本”上比较才有意义。本协议为 `evaluate-suite` 增加两道独立门禁：运行前验证冻结语料，运行后比较同构 suite 指标。它不调用模型、不修改用例，也不替代评测本身。

## 1. 冻结清单

评测根目录可以提供 `autofix-benchmark.yml`：

```yaml
version: 1
name: autofix-complex-boundary-v1
cases:
  - path: python-order-service
    source:
      kind: synthetic
      repository: local://docker-ollama-autofix
      revision: python-order-service-v1
      license: MIT
    tree_sha256: <64 位摘要>
```

每个被 `autofix-eval.yml` 发现的用例必须在清单中出现且只能出现一次。`source.kind` 区分仓库自建的 `synthetic` fixture 与固定上游提交的 `upstream` fixture；repository、revision 和 license 均不能为空。upstream 来源额外要求 HTTPS 仓库地址和 7-64 位 Git commit，不接受 branch、tag 或浮动版本。

当前套件包含两个 synthetic 用例，以及一个来自 `more-itertools` v11.2.1、固定到 `4aeb4338ae1407d8da9723e86da99efeb446f719` 的 upstream 用例。该目录保留原 MIT `LICENSE`，并在 `UPSTREAM.md` 记录原文件/函数、最小裁剪、类型适配和故障注入。验证阶段不克隆仓库或联网。

树摘要按排序后的项目相对路径、文件长度和原始 bytes 计算，排除 `.git`、`.autofix`、依赖、缓存和构建目录。符号链接、超过 10,000 个文件或 64 MiB 的用例会直接拒绝。源码、测试或 `autofix-eval.yml` 任一字节变化都会造成 drift，必须经过审核后显式更新 revision 和摘要。

验证命令：

```powershell
autofix benchmark-verify .\examples\complex-validation
```

CI 证据：

```powershell
autofix benchmark-verify .\examples\complex-validation `
  --output .\artifacts\benchmark-verification.json `
  --json
```

匹配退出 0，检测到源码 drift 退出 1，清单、路径或 I/O 错误退出 2。JSON 遵循 [`autofix-benchmark-verification` v1](../schemas/autofix-benchmark-verification-v1.schema.json)，指定输出使用不可覆盖的原子发布。

## 2. 运行评测

冻结验证只证明输入一致，不代表修复成功。验证后仍使用原命令生成 suite：

```powershell
autofix evaluate-suite .\examples\complex-validation `
  --repair `
  --model qwen2.5-coder:7b `
  --max-attempts 3
```

repair suite v4 保存模型名、Ollama 内容 digest、格式、family、参数规模、量化级别、runner、尝试上限、AutoFix 版本，以及 Python/OS/机器架构。需要比较的 `suite.json` 应作为 CI artifact 或 release evidence 保留；digest 无法解析时会明确写为 `null`，不伪造模型身份。

## 3. 比较趋势

```powershell
autofix evaluation-trend .\current\suite.json `
  --baseline .\baseline\suite.json
```

趋势命令首先要求：

- 两份 JSON 都是受支持的 `autofix-evaluation-suite`；
- mode 同为 baseline 或同为 repair；
- `relative_root → contract_sha256` 集合逐项相同；
- repair 的模型名/digest/量化元数据、runner、尝试上限和运行环境身份逐项相同；
- 待比较指标均为数字。

只有满足这些条件才计算 current - baseline。输出同时绑定两份 suite 的 SHA-256，并保存完整运行身份；AutoFix 版本允许不同，因为它正是趋势比较的主要变量。通过率、首次通过率和上下文召回率越高越好；平均尝试、静态拒绝、验证失败、越界修改、缺失必改文件、截断与 fallback 比例越低越好。运行耗时暂不作为失败门禁，因为共享机器抖动会制造噪声。

CI 可使用：

```powershell
autofix evaluation-trend .\current\suite.json `
  --baseline .\baseline\suite.json `
  --output .\artifacts\evaluation-trend.json `
  --fail-on-regression `
  --json
```

存在方向性退化时 `--fail-on-regression` 返回 1；不可比较返回 2。结果遵循 [`autofix-evaluation-trend` v1](../schemas/autofix-evaluation-trend-v1.schema.json)。

## 4. 当前限制和下一步

当前真实上游覆盖仍只有一个 Python/MIT 用例，样本量不足以形成通用排行榜。下一步应增加不同语言和许可证的固定上游样本，并把容器镜像 digest、编译器/Node 版本纳入 runner 身份。即使 suite 通过，数字也只表示这些冻结故障契约上的结果，不能外推为任意真实项目成功率。
