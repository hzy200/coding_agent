# 事故复盘：并发写编排的两个 P0（2026-10-21）

- **Date**：2026-10-21
- **Severity**：P0（编排写路径实际不可用）
- **Owner**：项目作者
- **Status**：已修复并补回归
- **Tags**：orchestrator · 并发写 · git worktree

> 事故复盘是最好的"反例教材"。AI 最容易犯的错就是重复别人已经踩过的坑——
> 所以复盘要**可被检索**，不能埋在日期命名的一次性报告里。
> 原始数字见 `docs/archive/TEST_REPORT_2026-10-21.md`。

## 摘要

同一场景下，4 个 implement worker 全部失败、0 个 patch 合并、耗时 49.33s /
99,739 token，最终由主 agent 自己兜底修复。修复两个 P0 后：
**32.04s / 40.1k token / 2 个 patch 成功合并**，4 个任务全部 success。

两个 P0 都属于「只在生产写编排路径出现、单测没有覆盖」的问题。

## 时间线

| 阶段 | 现象 |
|---|---|
| 1 | 开启写编排跑 `orchestrated_implement`，worker 的 `file_edit` 成功、worktree 内容确实改变 |
| 2 | 但 `_changed_files` 返回空，worker 自报"没有产生任何文件改动" |
| 3 | 修好 P0-1 后可产出 patch，但合并批次里出现 `(t1,1),(t1,2),(t2,1),(t2,2),(t2,3)` —— 同一任务被重复启动 |
| 4 | 第二次 `git apply` 失败并**整批回滚**，0 个 patch 合并 |
| 5 | 修好 P0-2 后恢复正常并发写 |

## 根因

### P0-1：快照目录被 `.gitignore` 忽略，脏检查误判

- 快照写在 worktree 内的 `.agent/`，而 `.agent/` 在 `.gitignore` 里。
- `git add -A -- . :(exclude).agent ...` 仍以 **exit=1** 返回
  "paths are ignored"。
- 代码把 **exit=1 当成失败**直接返回空列表。

**结构性教训**：`git` 的退出码 1 在不同上下文下含义不同（真失败 vs "nothing to stage"
vs "paths are ignored"）。**只看退出码、不看 stderr 就是在赌。**

修复：仅在 stderr 不是 ignored-path 警告时才返回空。

### P0-2：已完成但未合并的 implement 仍在 pending

- 一个 implement 完成、另一个仍在跑时，已完成者仍在 `pending` 状态，
  `refresh_ready` 把它重新排进 ready，导致同一任务重复启动。
- 同一补丁被重复加入合并批次，第二次 apply 必然失败，触发整批回滚。

**结构性教训**：状态机里「已完成」和「已收尾」是两个不同的状态。
中间态（等待合并）必须有一个**显式的、不会再被调度器捡起来**的名字。

修复：implement 结果进入合并队列时立即置 `merging`，离开 pending；
由 `finalize` 在合并后写入最终状态。

## 防御缺口

| 缺口 | 现在怎么补 |
|---|---|
| 单测覆盖不到"并发 + 真实 git worktree"的组合 | 补真实 WSL 集成测试（见下） |
| `.agent/` 在 worktree 内导致脏检查误判 | `_git_ready` 用与补丁收集相同的 pathspec 排除 `.agent/` 与 `__pycache__/` |
| 合并错误不落审计，事后无法定位 | worker 审计 detail 现在包含 `merge_error` |
| 状态机缺少中间态 | 新增 `merging` |

## 行动项

- [x] `implement._changed_files` 区分 ignored-path 警告与真失败
- [x] 引入 `merging` 中间状态
- [x] worker 审计 detail 增加 `merge_error`
- [x] 回归测试：
  - `test_concurrent_implement_workers_ignore_agent_state`
  - `test_completed_implement_is_not_relaunched_while_waiting_for_merge`
  - `test_implement_worker_creates_a_clean_patch`（仓库加入 `.gitignore`）
- [ ] **把 `orchestrator_write_enabled=true` 的并发写场景加入固定烟雾测试**
      —— 这两个 P0 之所以能潜行到生产路径，正是因为写编排没有常态化验证

## 给 Agent 的提醒

改 `orchestrator/implement.py` 或 `merge.py` 之前，先确认这三件事：

1. 脏检查是否排除了 `.agent/` 与 `__pycache__/`（否则并发 worker 互相误判）；
2. 新增状态是否有"不会再被调度器捡起来"的中间态；
3. 失败路径是否写进了审计 detail（否则事后无法定位）。

详见 [`src/coding_agent/orchestrator/AGENTS.md`](../../src/coding_agent/orchestrator/AGENTS.md)。
