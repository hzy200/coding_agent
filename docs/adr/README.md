---
layer: L2
doc_id: adr-index
title: 架构决策记录索引
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 每次新增决策时
---

# 架构决策记录（ADR）

**AI 唯一读不到的东西。** 代码告诉你"现在是什么样"，ADR 告诉你"当时为什么这么选、
放弃了什么、代价是什么"。

## 规矩

1. **只增不改**。决策被推翻时新增一条，把旧条目标为
   `superseded by ADR-NNNN`。改写历史会让"为什么"彻底丢失。
2. **必须写代价**。只写好处的 ADR 等于没写，它会让后人以为这是个零成本的决定。
3. **不要让 AI 把代码现状概括成"我们采用 X"**。那样会丢掉备选方案与代价，
   只剩下结果——而 ADR 的价值恰恰在被放弃的那些选项里。
4. 数量控制在几十条量级。**超过五十条说明该重构了**，不是该继续加。
5. 模板：[`TEMPLATE.md`](TEMPLATE.md)。

## 索引

| # | 标题 | Status | 领域 |
|---|---|---|---|
| [0001](0001-model-outputs-intent-only.md) | 模型只输出意图，宿主代执行 | accepted | 安全 · 架构 |
| [0002](0002-bash-stdin-not-argv.md) | 命令经 stdin 传给 `bash -l -s`，不用 `-lc` + argv | accepted | 沙箱 · 偏离需求 |
| [0003](0003-dotenv-over-env.md) | 配置优先级刻意让 `.env` 高于环境变量 | accepted | 配置 · 偏离惯例 |
| [0004](0004-approval-decision-execution-split.md) | 审批决定与执行分离，执行集中在唯一咽喉 | accepted | 安全 · 架构 |
| [0005](0005-auto-verify-no-manifest.md) | 自动验证不执行 manifest 派生的命令 | accepted | 安全 · 验证 |
| [0006](0006-eval-judge-stdlib-unittest.md) | 评测判定固定用 stdlib unittest，不用 pytest | accepted | 评测 · 事故驱动 |
| [0007](0007-eval-fixture-deterministic-generator.md) | 长程评测仓库用确定性生成器，不用真实仓库 | accepted | 评测 · 偏离计划 |
| [0008](0008-memory-no-auto-distillation.md) | 长期记忆只做显式增删，不做 LLM 自动提炼 | accepted | 记忆 · 可审计性 |

## 待补录候选

下面这些在现有文档里已有素材，但还没整理成 ADR。按价值排序：

- 编排采用「独立子图 + 事件驱动 DAG」而非「在主图里塞 orchestrator 逻辑」（`HARNESS.md §15`）
- 并发写采用「worktree 隔离 + 顺序 patch 合并」而非「文件锁 + 就地写」（`HARNESS.md §8`）
- 上下文裁剪用「字符 + 估算 token 双预算」而不引入 tokenizer（`WORKFLOW.md §2.9 W4`）
- 内存上限（`ulimit -v`）默认关闭（`ACCEPTANCE.md §4`）
- 命令分级用「词法 best-effort + 可选 bwrap」而非「默认内核级隔离」（`THREAT_MODEL.md §5`）
- 审计与 checkpoint 落宿主侧而非工作区（`THREAT_MODEL.md §1`）
- 干预只在节点边界生效、cancel 是协作式的（`HARNESS.md §10.3`）

## 相关

- [`../glossary.md`](../glossary.md) —— 统一语言词汇表（术语歧义是 AI 出错的头号来源）
- [`../THREAT_MODEL.md`](../THREAT_MODEL.md) —— 安全边界与防线分级
- [`../tech-debt.yaml`](../tech-debt.yaml) —— 已知缺陷与临时方案
- [`../postmortem/`](../postmortem/) —— 事故复盘（反例教材）
