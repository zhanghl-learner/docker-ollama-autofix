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

AutoFix 从已安装包的 `dependencies` 递归解析闭包，只把该闭包复制到 baseline/fixed 临时副本。它不复制整个 `node_modules`，不运行 npm/yarn/pnpm，不读取远端 registry，也不临时安装缺失包。

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

JSON 遵循 [`autofix-javascript-property-check` v1](../schemas/autofix-javascript-property-check-v1.schema.json)。报告默认写入 `.autofix/javascript-property-checks/`，`--no-save` 可关闭。

- preview 或 `counterexample-confirmed`：退出 0；
- 差分契约未确认：退出 1；
- 候选、依赖、Node、plan 或策略错误：退出 2。

当前切片只负责候选 preview 和差分确认，尚不支持 JavaScript 候选的模型生成、promotion 或 property-apply。Python promotion 的 schema 和写回流程不会自动接受 JavaScript 报告；扩展前需要单独设计 `.mjs` 目标、patch 和依赖可重现性契约。
