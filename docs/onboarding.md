---
layer: L3
doc_id: onboarding
title: 新人上手路径
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 环境或命令变动时
---

# Onboarding · 新人上手路径

> 目标：**30 分钟跑起来，2 小时看懂边界。** 也是人类保持系统理解力的防线——
> 不要只靠 AI 读代码来"理解"这个系统。

## 第 0 步：环境（10 分钟）

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
agent doctor                  # 期望「全部通过」，不需要 API Key
agent sandbox-init            # 创建沙箱工作区
```

前置条件：Anaconda/Miniconda、WSL2 + Ubuntu（**非 root 用户**）、DeepSeek API Key。
可选：`wsl -d Ubuntu -- sudo apt-get install -y bubblewrap`（内核级隔离）。

详细版本快照与复现/导出见 [`ENVIRONMENT.md`](ENVIRONMENT.md)。

## 第 1 步：跑通一次（5 分钟）

```bash
agent run -C <任意目录> "这个项目是做什么的？只做分析，不要改任何文件。"
```

观察点：工具行的等级标签 `→ [L0 只读] shell_exec: ...`。
**这个等级是宿主判定的，不是模型自报的。**

## 第 2 步：读懂一条原则（10 分钟）

> **宿主内置工具，模型只输出意图。**

LLM 只产出 `{tool, args}`，宿主校验后在 WSL2 沙箱执行；文件修改走专用工具，
不经 shell。由此推出四件事：分级不看自报、危险命令挂起审批、改动可回滚、一切可追溯。

读 [`ARCHITECTURE.md`](ARCHITECTURE.md) 第 1–2 节，最多 15 分钟。

## 第 3 步：知道哪里不许碰（15 分钟，**最重要**）

按顺序读三份：

1. [`../AGENTS.md`](../AGENTS.md) 第 3 节 —— 禁区清单（承重墙在哪）
2. [`THREAT_MODEL.md`](THREAT_MODEL.md) 第 4 节 —— 防线分级（哪些手段只能算 T1）
3. [`glossary.md`](glossary.md) —— 术语表（**本项目三套数字编号系统并存**，
   不查表一定会理解错）

## 第 4 步：跑测试，建立信心（10 分钟）

```bash
make check                    # ruff + 快反馈，约 1 分钟
pytest -m "not llm"           # 全量（含真实 WSL）
```

> ⚠️ **永远带 `-m`**。裸 `pytest` 会把 3 条 `llm` 标记用例一起跑、真的调用模型、真的计费。

## 第 5 步：按角色深入

| 你要动什么 | 读什么 |
|---|---|
| 主图 / 状态字段 | [`WORKFLOW.md`](WORKFLOW.md) §2.2 状态生命周期表 + `src/coding_agent/graph/AGENTS.md` |
| 编排 / 并发 / 写隔离 | [`HARNESS.md`](HARNESS.md) + `src/coding_agent/orchestrator/AGENTS.md` |
| 沙箱 / 安全 | [`THREAT_MODEL.md`](THREAT_MODEL.md) + `src/coding_agent/sandbox/AGENTS.md` |
| 工具 | `src/coding_agent/tools/AGENTS.md` + [`contracts/tools.yaml`](contracts/tools.yaml) |
| 前端 | [`WORKFLOW.md`](WORKFLOW.md) §4（**不得 import tools/ 或 sandbox/**） |
| 配置 | [`contracts/config.md`](contracts/config.md)（全量，代码生成） |
| 评测 | [`../tests/eval/README.md`](../tests/eval/README.md)（噪声口径必读） |

## 第 6 步：验收与决策

- 某条需求实现到哪：[`acceptance/acceptance-criteria.yaml`](acceptance/acceptance-criteria.yaml)
  （Given-When-Then + 可执行测试 ID）
- 为什么这么设计：[`adr/`](adr/README.md)（决策记录，只增不改）
- 还没还的债：[`tech-debt.yaml`](tech-debt.yaml)
- 踩过的坑：[`postmortem/`](postmortem/README.md)

## 常见误解（先打预防针）

| 误解 | 事实 |
|---|---|
| "命令等级是模型自己报的" | 不是。宿主看命令文本判定，模型自报不算数 |
| "`-y` 就全自动了" | 只跳过 L2，**L3 始终要人确认** |
| "经 shell 改的文件也能回滚" | 不能。经 shell 的修改不产生快照 |
| "TUI/Web 可以自己放行审批" | 不能。审批强制在 `tools` 节点，前端绕不过 |
| "default 评测套件 100% 说明智能体很强" | 说明**这把尺子已饱和、没有区分度**，不是智能体完美 |
| "评测涨了 10 个点" | 先看 `spread`。同份代码曾跑出 52% 与 83%（spread 31pp） |
| "词法守卫能挡住逃逸" | 不能。它是 best-effort，T2 必须靠 bwrap 兜底 |
