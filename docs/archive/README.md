---
layer: L2
doc_id: archive-index
title: 归档索引
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 引用变动时
---

# 归档索引

**为什么有这个目录**：`IMPROVEMENT_PLAN.md` 与 `PLAN.md` 曾被多处引用
（`HARNESS.md §18`、`ACCEPTANCE.md §3`、`tests/eval/README.md`），但**文件本身不存在**。
断链比"没有文档"更糟——它让人以为有一份说明在，点进去才发现是空的。

处理办法不是凭空补两份文件，而是**把引用指向这里，并说明内容现在住在哪**。

## 原 `PLAN.md`（早期开发方案）

早期开发方案，已被实现覆盖。它的内容现在分散在：

- 架构与分层 → [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- 编排设计 → [`../HARNESS.md`](../HARNESS.md)
- 工作流与状态生命周期 → [`../WORKFLOW.md`](../WORKFLOW.md)

## 原 `IMPROVEMENT_PLAN.md`（缺陷与阶段计划）

按原始条目去向：

| 原条目 | 现在在哪 |
|---|---|
| Q1「CI 无 WSL 的沙箱层缺口」 | [`../ACCEPTANCE.md`](../ACCEPTANCE.md) 第 3 节的口径提醒；CI 已用 `::warning::` 显式化 |
| §3.2 W2「长程任务要提一个量级」 | [`../tests/eval/README.md`](../../tests/eval/README.md) 的「long 套件」章节（38 文件 / 约 970 行） |
| 第四部分「任务源自真实仓库而非自造」 | 已**决定不照做**，理由见 [`../adr/0007-eval-fixture-deterministic-generator.md`](../adr/0007-eval-fixture-deterministic-generator.md) |
| 各类缺陷与待办 | [`../tech-debt.yaml`](../tech-debt.yaml)（带 owner 与到期日） |
| 已修的 P0 | [`../postmortem/`](../postmortem/) |

## 一次性报告

| 文件 | 说明 |
|---|---|
| [`TEST_REPORT_2026-10-21.md`](TEST_REPORT_2026-10-21.md) | 2026-10-21 全量测试与上机运行快照；第 5 节的两个 P0 已抽成 [`../postmortem/2026-10-21-orchestrated-write.md`](../postmortem/2026-10-21-orchestrated-write.md)，本文只作历史数字 |

快照只增不改。新结果另起一份，不要覆盖历史基线。

## 规矩

- **不要新建指向不存在文件的链接**。CI 的 `scripts/check_docs.py` 会拦断链。
- 历史阶段（Phase 3A–4D）的坐标意义保留在 [`../HARNESS.md`](../HARNESS.md) §14，
  不单独搬出来——它们已全部落地，只作历史参考，不代表当前状态。
