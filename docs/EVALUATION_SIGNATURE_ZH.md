# 评测发布的 Ed25519 签名与验证

publication 的 SHA-256 只能检测内容变化，不能证明是谁批准了结果。`autofix evaluation-sign` 为已经审核发布的 publication 生成独立 Ed25519 签名；`autofix evaluation-verify-signature` 只有在调用者显式提供可信公钥时才返回成功。

## 1. 生成和保管密钥

AutoFix 不自动创建、上传或托管密钥。可以用项目已依赖的 `cryptography` 在受控环境创建 Ed25519 PEM 密钥：

```powershell
python -c "from pathlib import Path; from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; k=Ed25519PrivateKey.generate(); Path('evaluation-signing-key.pem').write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())); Path('evaluation-signing-key.pub.pem').write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))"
```

私钥必须放在仓库外、限制文件权限并通过独立备份或密钥管理系统保护，绝不能提交到 Git。当前 v1 只读取未加密 PKCS8 PEM Ed25519 私钥，便于无交互 release job；这不等于建议在普通 CI 工作区长期保存明文私钥。公钥可以公开，但其分发渠道才是信任根。

## 2. 显式签署

先独立计算准备签署的 publication 摘要：

```powershell
$publication = ".\benchmarks\evaluation-results\autofix-complex-boundary-v1\2026.11-qwen25.json"
$digest = (Get-FileHash -Algorithm SHA256 $publication).Hash.ToLowerInvariant()
```

确认文件、release 和摘要后再调用私钥：

```powershell
autofix evaluation-sign $publication `
  --private-key "D:\secure\evaluation-signing-key.pem" `
  --signer "release-reviewer" `
  --expected-publication-sha256 $digest `
  --output "$publication.signature.json"
```

输出必须不存在，AutoFix 不覆盖旧签名。签名声明使用规范化 JSON 和域分离 `autofix-evaluation-signature-v1`，覆盖 publication SHA-256、schema/version、release、签署时间、signer 标签和公钥 DER 指纹；publication 本身仍按原始字节计算摘要，因此空格或换行变化也会使验证失败。

## 3. 使用可信公钥验证

验证方必须从独立可信渠道获得公钥，然后执行：

```powershell
autofix evaluation-verify-signature $publication `
  "$publication.signature.json" `
  --public-key ".\trusted\evaluation-signing-key.pub.pem"
```

CI 使用机器可读结果：

```powershell
autofix evaluation-verify-signature $publication `
  "$publication.signature.json" `
  --public-key ".\trusted\evaluation-signing-key.pub.pem" `
  --json
```

成功退出码为 `0`；摘要、publication 身份、公钥指纹或 Ed25519 签名任一不匹配时返回 `1`。verification JSON 不包含本机路径，可以作为 CI artifact；格式见 [`autofix-evaluation-signature-verification` v1](../schemas/autofix-evaluation-signature-verification-v1.schema.json)。

## 4. 信任边界

- `signer` 是被签名的显示标签，但标签本身不建立现实身份；可信身份来自调用者选择的公钥及其分发/轮换制度。
- `signed_at` 被签名但由本机时钟生成，不是可信时间戳服务证明。
- 签名证明持有私钥的一方批准了这些精确字节，不证明模型正确、审核充分或 CI 没有被攻破。
- 本 Ed25519 协议没有证书链、吊销列表、硬件密钥、GitHub OIDC 或 Sigstore transparency log。
- 平台 CI provenance 已由独立的[GitHub Artifact Attestation 来源验证](EVALUATION_PROVENANCE_ZH.md)提供验证侧闭环；它不会把本签名、Git commit SHA 或普通文件摘要重新命名为供应链证明，也不会取代本地审核授权。
