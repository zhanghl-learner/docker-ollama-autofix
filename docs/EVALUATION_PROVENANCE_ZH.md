# GitHub Artifact Attestation 来源验证

本地 Ed25519 签名可以证明某个公钥持有者批准了 publication，但不能证明文件由哪一个 GitHub Actions 工作流、哪一个仓库版本产生。`autofix evaluation-verify-provenance` 调用 GitHub CLI 的 artifact attestation 验证器，并在成功后生成隐私最小化、不可覆盖的 AutoFix 回执。

本功能是**验证侧**协议：不创建 attestation、不申请 OIDC token、不上传文件，也不修改 Git。GitHub CLI 负责验证 Sigstore/GitHub 信任根、签名证书与 subject；AutoFix 负责收紧调用策略、二次核对 publication subject SHA-256，并保存所采用的精确身份约束。

## 1. 前置条件

- 安装支持 `gh attestation verify` 的 GitHub CLI；在线模式还需要 GitHub API 访问和相应认证。
- publication 已由 `evaluation-publish` 生成，并且调用者已通过独立渠道确认其 SHA-256。
- 已知预期的 `owner/repository`、签名工作流路径、源码 commit digest 和完整 Git ref。
- 生成端应使用 GitHub artifact attestation action 对 **publication 文件本身**签发 SLSA provenance；仅对 wheel、压缩包或另一份文件签发不能验证本 publication。

GitHub 官方参考：[验证命令](https://cli.github.com/manual/gh_attestation_verify)、[创建和使用 artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)、[离线验证](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/verify-attestations-offline)。

## 2. 在线验证

```powershell
$publication = ".\benchmarks\evaluation-results\autofix-complex-boundary-v1\2026.11-qwen25.json"
$digest = (Get-FileHash -Algorithm SHA256 $publication).Hash.ToLowerInvariant()
$commit = git rev-parse HEAD

autofix evaluation-verify-provenance $publication `
  --repo "zhanghl-learner/docker-ollama-autofix" `
  --signer-workflow "zhanghl-learner/docker-ollama-autofix/.github/workflows/release.yml" `
  --source-digest $commit `
  --source-ref "refs/tags/v0.19.0" `
  --expected-publication-sha256 $digest `
  --output "$publication.provenance-verification.json"
```

AutoFix 固定传入以下策略，不能由宽松选项覆盖：

- `--repo`，不接受仅 owner 范围；
- `--signer-workflow`，必须精确到 `.github/workflows/*.yml` 或 `.yaml`；
- `--source-digest` 与完整 `--source-ref`；
- GitHub Actions OIDC issuer `https://token.actions.githubusercontent.com`；
- SLSA provenance v1 predicate；
- SHA-256 subject；
- `--deny-self-hosted-runners`；
- JSON 结果，供 AutoFix 再次检查 statement subject。

验证失败退出码为 `1`，不会创建回执。`--json` 让 stdout 只输出成功回执或稳定错误 envelope，适合 CI。

### 仓库内置的受保护发布工作流

仓库提供 [`.github/workflows/evaluation-publication-provenance.yml`](../.github/workflows/evaluation-publication-provenance.yml)，用于完成真实签发和独立验收。它只能手动运行，并强制从 Git tag 调度。运行前应在 GitHub 仓库设置中创建 `evaluation-publication` Environment，至少启用 required reviewers，并限制允许部署的 tag；没有这些仓库端规则时，workflow 中的 environment 名称本身不能代替审批策略。

准备步骤：

1. 使用 `evaluation-publish` 生成并审核 publication；
2. 将文件提交到 `benchmarks/evaluation-results/<benchmark>/<release>.json`，其中 `<release>` 必须和 JSON 内的 `release` 一致；
3. 创建并推送受保护 tag；
4. 在 Actions 中选择 **Evaluation publication provenance**，从该 tag 运行；
5. 输入仓库相对 `publication_path` 和独立计算的 `expected_sha256`。

工作流包含两个权限隔离的 job：

- `attest` 具有 `contents: read`、`id-token: write`、`attestations: write`，检查 tag、Git HEAD、tracked path、无符号链接、publication 身份和摘要后，使用固定 commit 的 `actions/attest-build-provenance` 签发；
- `verify` 只有读取权限，通过前一 job 输出的 artifact ID 下载精确文件，安装当前 tag 中的 AutoFix，然后用 `evaluation-verify-provenance` 绑定 repository、workflow、tag commit 和 tag ref。它没有 OIDC 或 attestation 写权限。

成功运行保留两个 90 天 Actions artifacts：原始 `publication.json` 和 `provenance-verification.json`。Attestation 本身由 GitHub 存储。所有外部 Actions 都固定到完整 commit；升级 action 时必须同时更新固定 SHA、版本注释和工作流契约测试。

## 3. 离线验证

在联网且可信的准备机下载 bundle 与 trusted root，再通过独立介质传入离线环境：

```powershell
gh attestation download $publication --repo "zhanghl-learner/docker-ollama-autofix"
gh attestation trusted-root > trusted_root.jsonl
```

离线机必须同时指定二者：

```powershell
autofix evaluation-verify-provenance $publication `
  --repo "zhanghl-learner/docker-ollama-autofix" `
  --signer-workflow "zhanghl-learner/docker-ollama-autofix/.github/workflows/release.yml" `
  --source-digest $commit `
  --source-ref "refs/tags/v0.19.0" `
  --expected-publication-sha256 $digest `
  --bundle ".\sha256-$digest.jsonl" `
  --trusted-root ".\trusted_root.jsonl" `
  --json
```

只给 bundle 而不提供 trusted root 会在调用 `gh` 前停止。回执记录 bundle 和 trusted root 的 SHA-256，但不复制其内容或本机路径。离线 bundle 只能证明签发时证书中的仓库身份；仓库后来是否改名或同名重建，需要额外核对稳定 repository ID。在线 API 模式更适合确认当前仓库名称关系。

## 4. 回执与 schema

成功结果符合 [`autofix-evaluation-provenance-verification` v1](../schemas/autofix-evaluation-provenance-verification-v1.schema.json)，包含：

- publication 原始字节 SHA-256、version 和 release；
- exact repository、signer workflow、source digest/ref；
- 固定 OIDC issuer、predicate type 与 self-hosted runner 拒绝策略；
- 在线/离线模式以及离线信任材料摘要；
- 通过验证的 attestation 数量、GitHub CLI 版本首行和完整 `gh` JSON stdout 摘要。

回执不保存绝对路径、证书正文、predicate、源码、日志或 token。指定 `--output` 时目标必须不存在，防止历史证据被覆盖。

## 5. 三个不同的信任域

| 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| publication SHA-256 | 文件字节没有变化 | 谁创建或批准了文件 |
| Ed25519 signature | 指定可信公钥批准了精确 publication 和签名声明 | GitHub workflow 身份、可信时间、构建隔离 |
| GitHub attestation verification | GitHub/Sigstore 验证的工作流证书对匹配 subject 作出 SLSA 声明，并满足精确身份策略 | 模型结论正确、审核充分、predicate 中所有用户可控字段都可信 |

这三类证据可以同时存在，但不可互相替代。GitHub 官方特别说明：certificate 与 verified timestamps 属于密码学保护身份信息，而 predicate 可由发起工作流控制；高保障发布应把构建和 attestation 放在受保护的 reusable workflow，并限制修改权限。

## 6. 当前边界与后续工作

- 普通 AutoFix 命令不生成或上传 attestations；只有显式手动、tag-bound、environment-gated 的专用 workflow job 获得 `id-token: write` / `attestations: write`。
- 回执证明验证当时采用的策略和结果，不是透明日志镜像、撤销状态快照或长期可用性保证。
- 当前固定 GitHub.com OIDC issuer、SLSA v1 和 GitHub CLI；GitHub Enterprise Server、自定义 Sigstore 根与 OCI artifact 尚未开放。
- 工作流尚不自动创建 GitHub Release，也不上传私有 suite、模型日志或源码 diff。下一阶段可增加经过同一 digest/provenance 门禁的 release asset 发布，并验证 release asset 与 attested subject 是同一字节。
