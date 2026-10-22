---
layer: L0
doc_id: ci
title: 流水线（pipeline as code）
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 工作流文件变动时
---

# 流水线

事实源是 `.github/workflows/` 里的 YAML——**改流水线就是改那些文件**，
本文只说明每条流水线跑什么、以及为什么这么切。

## CI（`.github/workflows/ci.yml`）

触发：push / pull_request。Linux runner（**没有 WSL2**）。

| 步骤 | 命令 |
|---|---|
| 安装 | `pip install -e ".[dev,ui,web]"` |
| lint | `ruff check src tests` |
| 测试 + 覆盖率 | `pytest -m "not wsl and not llm" --cov --cov-report=term-missing` |
| 显式化沙箱缺口 | `::warning::` 提示 wsl 用例未执行 |

必须装齐 `ui` / `web` extras：TUI（textual）与 Web（fastapi）测试需要它们，
缺一个会导致 collection error 而非 skip。这两条都曾导致过 CI 失败。

**覆盖率门槛 `fail_under=76`** 按 CI 实际执行的子集定，不是贴着实测值
（Windows 实测 78.87%，CI 跑 Linux 会小幅漂移）。原理见 `pyproject.toml` 的注释。

### 已知缺口

CI 是 Linux runner、没有 WSL2，所以 `wsl` 标记的用例**一行都没跑**。
沙箱层与能力层的覆盖全靠本地 `-m wsl`。CI 会用 `::warning::` 显式标注这一点——
**"CI 全绿"绝不等于"沙箱层可用"**。

## Docs（`.github/workflows/docs.yml`）

触发：push / pull_request / workflow_dispatch。

| 步骤 | 命令 |
|---|---|
| 契约漂移 | `python scripts/gen_contracts.py --check` |
| 文档一致性 | `python scripts/check_docs.py --strict` |
| 文档—代码漂移 | `python scripts/check_drift.py` |

一致性检查包含七项：契约漂移、断链（含 anchor）、每份文档必须有 owner、
过期标记（超 90 天）、文档里不许出现裸 `pytest`、验收标准引用的测试文件与
用例必须存在、常驻上下文（`AGENTS.md` / `llms.txt` / 模块 AGENTS）不得超长。

`check_drift.py` 补的是链接检查覆盖不到的一类漂移：代码注释/docstring 里引用的
`docs/...`、以及文档里反引号标注的 `src/` `tests/` `scripts/` 路径必须存在。
它只做存在性检查，不判断内容是否过时。

每次运行还会生成 `docs-metrics.json` 并作为 artifact 上传（文档数、断链、
无 owner、过期、契约漂移、验收锚点、模块 AGENTS 覆盖率、最大文档）。
它是趋势指标，不是门禁；门禁仍是上一步的 `--strict`。

**为什么单独一个 workflow**：文档检查失败不该和代码测试混在一起判断，
但也不该被跳过——它是防腐化的唯一自动闸门。

## 本地等价命令

```bash
make check                        # ruff + 快反馈
pytest -m "not llm"               # 全量（含真实 WSL）
python scripts/gen_contracts.py   # 重生成契约
python scripts/check_docs.py      # 文档一致性（过期只警告）
python scripts/check_docs.py --strict   # 与 CI 同口径
```

## 规矩

- **文档与代码同 PR**。改了 `events.py` / `config.py` / `approve.py` /
  `audit/models.py` 必须重跑 `gen_contracts.py`，否则契约检查会红。
- 新增 CI 步骤时想清楚它能不能在无 WSL 的 runner 上跑；不能就标 `wsl`
  并接受它只在本地执行。
