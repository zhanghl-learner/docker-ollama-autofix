# JavaScript / fast-check 属性候选协议

`autofix property-js-check` 是 JavaScript 逻辑回归的实验性差分验证入口。它不会让模型生成或写入测试，也不会执行 npm install；默认只展示候选，只有显式 `--run` 才在两个临时项目副本中执行。

## 1. 候选边界

候选必须是最大 64 KiB 的 UTF-8 `.js` 或 `.mjs` 文件，并满足：

- `fc` 明确绑定自 `fast-check`；
- `test` 明确绑定自 `node:test`；
- 至少一个具名 `test("...", ...)`；
- 调用 `fc.assert(fc.property(...))`；
- 不导入 `fs`、`child_process`、网络、worker、cluster 等模块；
- 不使用动态 `import()`、`eval`、`new Function`、`fetch` 或 `process.*`。

这是保守的文本级静态门禁，不是 JavaScript 沙箱或完整语法证明。候选仍是可执行代码，必须先人工 preview；高风险来源应在独立虚拟机中审核。

## 2. 依赖策略

执行模式要求：

1. `node` 已在 PATH；
2. 项目 `package.json` 的 `dependencies` 或 `devDependencies` 显式声明 `fast-check`；
3. `node_modules/fast-check` 已存在，且依赖闭包的 package metadata 有效；
4. 依赖闭包不包含符号链接，总计不超过 10,000 个文件和 64 MiB。

AutoFix 按 Node 常见解析顺序，从请求包自身的 `node_modules` 开始，再检查项目顶层提升目录；因此支持 npm 常见的 hoisted 与 nested 布局。普通 `dependencies` 必须存在，已安装的 `optionalDependencies` 会纳入证据，缺失 optional 包不会阻断。每个包复制时保留相对安装路径，并排除未声明的嵌套 `node_modules`。

它不复制整个依赖树，不运行 npm/yarn/pnpm，不读取远端 registry，也不临时安装缺失包。执行报告会记录每个实际复制包的名称、版本、相对路径、文件数、字节数和 `package.json` SHA-256，以区分同名包的不同嵌套版本并支持结果审计。

## 3. 使用方式

先预览：

```powershell
autofix property-js-check .\candidate_property.mjs `
  --project .\my-js-project `
  --plan .\my-js-project\.autofix\plans\PLAN.json
```

审核候选后执行：

```powershell
autofix property-js-check .\candidate_property.mjs `
  --project .\my-js-project `
  --plan .\my-js-project\.autofix\plans\PLAN.json `
  --run
```

执行使用 `node --test --test-isolation=none`，避免测试 runner 再派生子进程。验收顺序与 Python 协议一致：baseline 候选连续两次失败且规范化签名相同；应用 reviewed plan 的 fixed 副本连续两次通过；最后 plan 原验证矩阵全部通过。原项目不会写入候选文件。

## 4. 输出与限制

JSON 遵循 [`autofix-javascript-property-check` v2](../schemas/autofix-javascript-property-check-v2.schema.json)，新增 `dependency_packages` 执行闭包证据；preview 时该数组为空。历史 [v1 schema](../schemas/autofix-javascript-property-check-v1.schema.json) 保留用于读取已保存报告。报告默认写入 `.autofix/javascript-property-checks/`，`--no-save` 可关闭。

- preview 或 `counterexample-confirmed`：退出 0；
- 差分契约未确认：退出 1；
- 候选、依赖、Node、plan 或策略错误：退出 2。

## 5. 将确认候选晋升为审核 patch

只有保存为 JSON、版本为 v2 且 verdict 为 `counterexample-confirmed` 的报告可以晋升：

```powershell
autofix property-js-promote `
  .\my-js-project\.autofix\javascript-property-checks\REPORT.json `
  --project .\my-js-project
```

晋升会重新检查：报告路径与 bytes SHA、两次稳定 baseline 失败、两次 fixed 通过、plan 原矩阵通过、候选当前源码与 SHA、plan diff，以及当前 fast-check 依赖闭包的版本/路径/规模/manifest SHA。任一证据漂移都会拒绝。

默认目标为 `tests/autofix_property_<sha>.test.mjs`。`--target` 只接受尚不存在、项目内且能识别为测试的 `.js`/`.mjs` 路径。输出目录 `.autofix/javascript-property-patches/` 包含 Git-compatible new-file patch 与 `applied: false` JSON 元数据：

```powershell
git apply --check ".\.autofix\javascript-property-patches\PATCH.patch"
```

命令不创建目标测试、不调用 `git apply`、不 commit/push。当前仍不支持 JavaScript 候选的模型生成或 property-apply；人工审核 patch 后如何安全写回属于下一权限阶段，不能复用 Python apply 而跳过 `.mjs` 与依赖证据校验。
