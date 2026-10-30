# AutoFix CI JSON 契约

`autofix ci-check` 是面向 CI 和脚本的无交互只读验证入口。它不调用模型、不修改源码、不生成 repair plan，也不自动提交或推送。

## 1. 基本命令

```powershell
autofix ci-check . `
  --command "python -m pytest -q --junitxml=pytest-results.xml" `
  --output "$env:TEMP\autofix-ci-result.json"
```

stdout 始终只有一个 JSON 文档。`--output` 使用临时文件和原子替换写入同一份内容，适合后续 artifact 上传。建议写到 CI runner 的临时目录，避免结果文件反过来使严格 clean gate 失败。默认不写普通 `.autofix/runs` 报告；需要时显式添加 `--save-run-report`。

## 2. 稳定退出码

| 退出码 | status | 含义 |
| ---: | --- | --- |
| 0 | `passed` | Git 策略与完整验证均通过 |
| 10 | `verification_failed` | 构建或测试实际失败，但不是高置信度环境故障 |
| 20 | `environment_error` | 缺少工具、依赖、Docker runtime 等环境问题 |
| 30 | `policy_rejected` | 非 Git、无 HEAD、脏工作区或无法确认 Git 策略 |
| 40 | `configuration_error` | 配置、构建系统、命令或参数组合无效 |
| 70 | `internal_error` | 未预期内部异常，或 CI 结果文件无法写入 |

这些是 `ci-check` 的契约码，不等于 pytest、编译器或脚本的原始退出码。原始退出码保留在 `build.exit_code` 和 `build.steps[].exit_code`。

## 3. JSON envelope v1

```json
{
  "schema": "autofix-ci-result",
  "version": 1,
  "created_at": "2026-10-29T00:00:00+00:00",
  "status": "passed",
  "exit_code": 0,
  "root": "E:\\project",
  "runner": "local",
  "repository": {},
  "build": {},
  "error": null
}
```

v1 固定上述十个顶层字段。未进入构建时 `build` 为 `null`；Git 检查尚未完成时 `repository` 可以为 `null`；错误状态在 `error` 中提供可操作说明。

仓库同时提交可供编辑器、流水线和外部工具读取的 [JSON Schema](../schemas/autofix-ci-result-v1.schema.json)。运行时代码和 schema 的常量、必需字段及退出码集合由回归测试锁定。

向后兼容原则：

- v1 内可以为嵌套对象增加字段，但不会删除或改变既有字段含义；
- 顶层不兼容变化必须提升 `version`；
- 自动化应同时校验 `schema` 和 `version`，不要只解析终端文本；
- build stdout/stderr 可能包含项目日志或敏感内容，上传前按源码日志级别管理。

## 4. 默认 Git 策略

默认要求：

- 当前路径属于 Git 仓库；
- 仓库存在 committed HEAD；
- tracked 与 untracked 状态均为空。

确实需要非 Git 验证时使用 `--allow-unversioned`；确实需要在脏工作区只读执行时使用 `--allow-dirty`。这些选项只放宽 CI 前置策略，不放宽 repair/apply-plan 的安全门禁。

## 5. GitHub Actions 示例

仓库的 `.github/workflows/ci.yml` 在 Windows 和 Linux 上使用同一命令，并通过 `actions/upload-artifact@v7` 上传 JSON 与 JUnit。官方 action v7 使用当前 GitHub-hosted runner runtime；自托管 runner 需要满足该 action 的最低 runner 版本要求。

```yaml
- name: Run tests through AutoFix CI contract
  run: >-
    autofix ci-check .
    --command "python -m pytest -q --junitxml=pytest-results.xml"
    --output "${{ runner.temp }}/autofix-ci-result.json"

- name: Upload evidence
  if: always()
  uses: actions/upload-artifact@v7
  with:
    name: autofix-ci-${{ runner.os }}
    path: |
      ${{ runner.temp }}/autofix-ci-result.json
      pytest-results.xml
```

该 workflow 只验证并上传证据，不调用 Ollama、不应用 patch、不创建 commit，也不修改远端分支。

## 6. 属性回归应用回执

当 promotion 已经由前置人工审核、生产修复已提交且检出保持干净时，可以在受保护的手动 job 中显式执行：

```yaml
- name: Apply and verify reviewed property regression
  shell: pwsh
  env:
    AUTOFIX_PROMOTION: ${{ inputs.promotion_path }}
  run: |
    autofix property-apply $env:AUTOFIX_PROMOTION `
      --project . `
      --apply `
      --output "${{ runner.temp }}/property-apply.json" `
      --json

- name: Upload property evidence
  if: always()
  uses: actions/upload-artifact@v7
  with:
    name: autofix-property-apply
    path: ${{ runner.temp }}/property-apply.json
    if-no-files-found: warn
```

`autofix-property-apply` 与通用 `autofix-ci-result` 是两个独立 schema：前者证明某个已晋升属性测试在固定 Git baseline 上通过新回归和 plan 原矩阵，后者描述普通无交互构建。流水线不应把二者混为同一个状态，也不应在 promotion 未经过审核时自动进入 apply job。`property-apply` 成功后只留下未提交测试文件；后续 commit/PR 仍是独立权限阶段。
