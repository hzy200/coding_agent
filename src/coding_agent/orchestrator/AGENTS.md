<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# orchestrator/ · harness 子图

独立子图：`survey → select_and_plan → write_gate? → scheduler ⇄ steering_dispatch → fan_in`，
结束后把结果交回主 agent 做最终实现与答复。小仓库由 runtime 选 `direct`，不进这里。

## 硬约束（改之前先看 `graph.py::validate_tasks`）

- 任务 `id` 唯一、`goal` 非空；`depends_on` 必须存在、无自依赖、无环。
- implement 必须有非空 `write_scope`，**implement 之间不能互相依赖**，
  并发 implement 的 `write_scope` 必须两两不重叠。
- 只读角色不能声明 `write_scope`。implement 不允许 `depends_mode=any`。
- 运行中只允许追加**只读**任务（explore/test/review）；worker 提议也只能是只读，
  且每个 worker 最多 2 个，经 host 校验（角色/去重/依赖/总量/无环）才入队。
- 校验失败 → **整图降级**（写模式退化为单个保守 implement，只读模式退化为单 explore），
  并显式标记 `degraded`。**不要静默截断任务数**。

## 写路径（风险最高）

implement 在独立 `git worktree` 中修改，完成后导出 patch，宿主在主工作区
**顺序** `git apply`：先全部 `--check` → 解析路径 → 全部留快照 → 顺序 apply
→ 任一失败则反向撤销/回滚。同一批补丁是**一个事务**，不留半合并状态。

前置条件：`AGENT_ORCHESTRATOR_WRITE_ENABLED=true` + 会话 `--write` +
工作区是 git 仓库根且干净（`.agent/`、`__pycache__/` 不计入脏改动）。

## 已知易错点（都出过 P0）

- worktree/patch 落在主工作区 `.agent/` 下，**脏检查必须排除 `.agent/` 与
  `__pycache__/`**，否则第二个 worker 会把它们误判成用户改动。
- implement 结果进入合并队列时**立即置 `merging` 并离开 pending**，
  否则 `refresh_ready` 会把它重新排进 ready，同一补丁被重复合并。
- 合并错误要写进审计 detail（`merge_error`），否则事后无法定位。
- 并发区**不弹审批、不调用 `interrupt()`**；审批属于主图或编排边界。

## 调度

ready 最小堆 + `asyncio.wait(FIRST_COMPLETED)`，完成即解锁依赖，不等整批。
`checkpoint_every` 只决定何时返回父图，**不再限制每边界能启动几个**。
token 预留用同角色历史用量 EWMA，预算不足时 next-fit 选放得下的任务，
不是把所有 pending 一起标 blocked。
