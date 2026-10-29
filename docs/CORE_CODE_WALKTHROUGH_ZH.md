# AutoFix 核心代码导读

本文按一次命令的调用路径解释代码。无需按文件名顺序阅读仓库。

## 1. 总览

```text
cli.py
 ├─ detectors/project.py      识别项目和 BuildStep
 ├─ config.py                 合并全局/项目配置
 ├─ builders/runner.py        本机执行
 ├─ builders/docker.py        Docker 执行
 ├─ repository.py             Git 身份、门禁和 worktree
 └─ agent/repair.py           修复状态机
     ├─ context.py            选择有限上下文
     ├─ ollama.py             模型协议
     ├─ patches.py            不可信提议→安全修改
     ├─ plans.py              plan v4 保存和重放
     ├─ artifacts.py          standalone patch
     └─ rollback.py           撤销
```

## 2. CLI：`src/autofix/cli.py`

CLI 负责参数、用户输出和退出码，不应承载核心修复算法。

重点辅助函数：

- `_selected_build_steps()`：把显式参数、配置和自动检测统一成 `BuildStep`；
- `_docker_config()`：形成受限 Docker 策略；
- `_commit_verified_result()`：把验证结果转换为精确本地 commit 请求；
- `run()`：只执行构建；
- `repair()`：调用模型修复，默认 preview；
- `apply_plan()`：重放已审核 plan，不再次调用模型。

阅读方法：先看命令参数和异常到退出码的映射，再跟进被调用的核心函数。

## 3. 检测与配置

`detectors/project.py` 输出 `ProjectInspection`。一个仓库可以发现多个构建系统，但单次执行只选择一个。`config.py` 把用户级默认值和项目 `autofix.yml` 合并，并对未知字段和非法值提前报错。

关键问题：最终选择了哪些 `BuildStep.argv`？如果答案不正确，应先修检测或配置，而不是修模型 prompt。

## 4. 执行器

`builders/runner.py` 的核心数据结构是：

- `StepResult`：单步 argv、stdout、stderr、退出码、耗时和超时；
- `BuildResult`：按顺序执行过的步骤及聚合成功状态。

本机执行使用 `subprocess.run(..., shell=False)`。临时 Python bytecode 和 Go build cache 防止旧产物影响验证。

`builders/docker.py` 负责构造只读 root filesystem、禁网络、资源限制和 disposable copy 等参数。Docker 降低误操作风险，但不是对恶意构建脚本的强安全沙箱。

## 5. Git 边界：`repository.py`

`inspect_git_workspace()` 是只读入口，返回：

- repository root、branch、HEAD；
- porcelain changes；
- repository fingerprint 和项目子路径；
- 是否允许 repair。

`create_detached_worktree()` 从已提交 HEAD 创建临时执行空间，并在成功、失败、异常或取消时定向清理。

`create_local_repair_commit()` 是第二道显式门禁：重新检查仓库身份、HEAD、目标文件 bytes、Git 变更集合和暂存区，只对精确路径执行本地 commit。

## 6. 修复状态机：`agent/repair.py`

`repair_project()` 是核心编排函数：

1. 校验 Git baseline；
2. 创建隔离工作区；
3. 运行初始构建；
4. 把错误和有限源码交给 client；
5. 校验 proposal；
6. 应用候选并重新构建；
7. 失败时记录证据并恢复同一基线；
8. 成功时合并为最终 `ValidatedChange`；
9. 可选写回真实项目；
10. 保存 report 和 patch，清理隔离空间。

`RepairResult` 是 CLI、报告和后续 commit 之间的数据契约。修改它时必须同步检查序列化、CLI 和历史报告兼容性。

## 7. 模型边界：`context.py` 与 `ollama.py`

`context.py` 从构建输出提取直接相关文件，解析其保守的本地 import/include 边，并在数量和大小限制内生成 `SourceContext`。每个 `ContextSelection` 解释文件来自错误输出、某个本地依赖还是回退扫描，并记录截断状态。兼容入口仍返回普通文件字典。

`repair.py` 把选择原因传给 `ollama.py`，同时按 attempt 写入审计结果。`ollama.py` 只把原因作为相关性元数据；动态路径枚举仍单独决定哪些文件可编辑，因此“被选中”不会绕过测试保护。

模型响应永远按不可信输入处理。JSON Schema 只是第一层格式约束，不代表路径和代码安全。

## 8. 修改安全：`patches.py`

`validate_changes()` 把 proposal 转换成 `ValidatedChange`，主要拒绝：

- 绝对路径和 `..`；
- 项目外文件、符号链接逃逸和不支持的文件类型；
- 默认受保护的测试；
- 旧文本或行号不匹配；
- 重叠、无变化和过大编辑；
- 删除大量代码或 Python 顶层定义。

应用时使用完整 replacement，但 replacement 只由经过验证的局部编辑与原文件未修改部分组合而成。换行风格以真实目标文件为准。

## 9. Plan 与 artifact

`plans.py` 保存和加载 plan v4。加载顺序很重要：先验证 schema/provenance，再验证路径和原文件 hash，最后重建 diff digest。

`artifacts.py` 只负责确定性渲染与独占写入 patch；它不判断候选是否正确，输入必须已经是 `ValidatedChange`。

## 10. 从测试学习约束

优先阅读：

- `test_repair.py`：状态机和恢复；
- `test_git_worktree.py`：真实 Git 隔离和换行差异；
- `test_plans.py`：plan 兼容与 provenance；
- `test_repository.py`：脏工作区、commit 精确范围；
- `test_artifacts.py`：patch 确定性和不可覆盖。

看到一个安全判断时，寻找对应负向测试。若没有测试，后续修改应优先补上。
