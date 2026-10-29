# AutoFix 动手实验

这些实验使用独立临时项目，不要求先理解全部源码。建议每完成一步就观察文件、Git 状态和 `.autofix` 报告。

## 1. 准备

```powershell
autofix doctor
ollama list
docker version
```

在任意临时目录创建一个 Git 项目，加入一个会失败的测试并提交 baseline。不要在有重要未提交工作的仓库中做实验。

## 2. 实验一：只复现失败

```powershell
autofix inspect .
autofix git-status . --require-clean
autofix run .
```

观察：检测到了什么构建系统、执行了什么 argv、失败退出码是多少、`.autofix/runs` 保存了什么。

完成标准：能够指出 `BuildStep` 和 `BuildResult` 分别对应哪部分输出。

## 3. 实验二：preview 不修改原项目

```powershell
autofix repair . --model qwen2.5-coder:7b --max-attempts 3
git status --short
```

观察 repair report、patch 和 plan。源码应该保持原始失败状态；Git 项目中的模型修改发生在 detached worktree。

完成标准：能够解释为什么 preview 已经验证通过但原源码仍未改变。

## 4. 实验三：确定性应用

```powershell
$plan = Get-ChildItem .\.autofix\plans\*.json |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

autofix apply-plan $plan.FullName --project . `
  --patch-output .\artifacts\verified.patch

autofix run .
git diff
git apply --reverse --check .\artifacts\verified.patch
```

完成标准：能够说明 `apply-plan` 为什么不再次调用 Ollama，以及反向 patch 检查证明了什么。

## 5. 实验四：显式本地 commit

重新准备失败 baseline 和 plan 后执行：

```powershell
autofix apply-plan $plan.FullName --project . `
  --commit `
  --commit-message "fix: apply verified repair"

git show --stat HEAD
git show -s --format=%B HEAD
```

检查 commit 只包含目标源码，并存在 `AutoFix-Diff-SHA256` trailer。AutoFix 不会 push 或 merge。

## 6. 实验五：观察安全拒绝

### 6.1 脏工作区

在 baseline 上创建一个未提交文件，再运行 repair。预期在模型调用和源码写入前停止。

### 6.2 过期 plan

生成 plan 后人工修改目标文件，再执行 `apply-plan`。预期原文件 hash 不匹配，停止写入。

### 6.3 HEAD 漂移

生成 plan 后创建一个新 commit，再应用旧 plan。预期 baseline commit 不匹配。

完成标准：能够区分策略拒绝、构建失败和环境错误。

## 7. 三个入门改动练习

1. 为 `git-status` 表格增加一个只读字段，并补 CLI 测试；
2. 为 patch 输出增加一个不改变默认行为的摘要字段，并补 artifact 测试；
3. 为新的高置信度环境错误增加正向、负向分类测试。

每个练习必须遵循：先写失败测试、最小实现、全量回归、更新文档。不要从修改 Ollama prompt 开始，因为 prompt 行为最难确定性验证。

## 8. 标准收尾命令

```powershell
python -m pytest -q
python -m compileall -q src tests
git diff --check
git status --short
```

涉及 Git worktree 时额外运行：

```powershell
git worktree list --porcelain
```
