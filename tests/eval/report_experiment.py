"""从 LangSmith 实验生成量化报告（JSON + Markdown）。"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.config import get_settings


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return float(ordered[index])


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "sum": round(sum(values), 2),
        "avg": round(statistics.mean(values), 2) if values else 0.0,
        "p50": round(_pct(values, 0.5), 2),
        "p95": round(_pct(values, 0.95), 2),
        "max": round(max(values), 2) if values else 0.0,
    }


def _bucket_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    def vals(path: str) -> list[float]:
        return [_num(r["metrics"].get(path)) for r in records]

    return {
        "total": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "pass_rate": round(
            sum(1 for r in records if r["passed"]) / len(records), 4
        )
        if records
        else 0.0,
        "main_tokens": round(
            sum(vals("input_tokens")) + sum(vals("output_tokens")), 2
        ),
        "worker_tokens": round(
            sum(vals("worker_input_tokens"))
            + sum(vals("worker_output_tokens")),
            2,
        ),
        "agent_seconds": round(sum(vals("agent_seconds")), 2),
        "avg_agent_seconds": round(
            statistics.mean(vals("agent_seconds")), 2
        )
        if records
        else 0.0,
    }


def collect(project: str) -> dict[str, Any]:
    from langsmith import Client

    settings = get_settings()
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    client = Client()
    runs = list(client.list_runs(project_name=project, is_root=True))
    records: list[dict[str, Any]] = []
    for run in runs:
        inputs = run.inputs or {}
        outputs = run.outputs or {}
        metrics = outputs.get("metrics") or {}
        records.append(
            {
                "case_id": str(inputs.get("case_id", run.name)),
                "category": str(inputs.get("category", "")),
                "passed": bool(outputs.get("passed")),
                "verdict": str(outputs.get("verdict", "error")),
                "detail": str(outputs.get("detail", ""))[:1_500],
                "error": str(run.error or ""),
                "metrics": metrics,
                "start_time": run.start_time.isoformat()
                if run.start_time
                else "",
                "end_time": run.end_time.isoformat() if run.end_time else "",
            }
        )

    verdicts = Counter(record["verdict"] for record in records)
    categories = {
        category: _bucket_stats(
            [r for r in records if r["category"] == category]
        )
        for category in sorted({record["category"] for record in records})
    }
    tiers = {
        tier: _bucket_stats(
            [
                r
                for r in records
                if str(r["metrics"].get("tier") or "unknown") == tier
            ]
        )
        for tier in sorted(
            {str(r["metrics"].get("tier") or "unknown") for r in records}
        )
    }
    workflows = Counter()
    failures: list[dict[str, Any]] = []
    for record in records:
        workflows[str(record["metrics"].get("workflow") or "direct")] += 1
        if not record["passed"]:
            failures.append(record)

    def values(path: str) -> list[float]:
        return [_num(r["metrics"].get(path)) for r in records]

    token_in = values("input_tokens")
    token_out = values("output_tokens")
    worker_in = values("worker_input_tokens")
    worker_out = values("worker_output_tokens")
    sub_in = values("subagent_input_tokens")
    sub_out = values("subagent_output_tokens")
    agent_seconds = values("agent_seconds")
    judge_seconds = values("judge_seconds")
    tool_calls = values("tool_calls")
    writes = values("writes")
    steps = values("steps")
    replans = values("replans")
    workers = values("workers")
    merge_conflicts = sum(
        1 for r in records if _num(r["metrics"].get("worker_conflicts")) > 0
    )
    retries = sum(
        _num(r["metrics"].get("worker_retries")) for r in records
    )
    verifications = Counter()
    worker_statuses = Counter()
    for record in records:
        verifications.update(record["metrics"].get("verifications") or {})
        worker_statuses.update(
            record["metrics"].get("worker_statuses") or {}
        )

    start_times = [r["start_time"] for r in records if r["start_time"]]
    end_times = [r["end_time"] for r in records if r["end_time"]]
    wall_seconds = 0.0
    if start_times and end_times:
        wall_seconds = (
            datetime.fromisoformat(max(end_times))
            - datetime.fromisoformat(min(start_times))
        ).total_seconds()

    def total_tokens(record: dict[str, Any]) -> float:
        metrics = record["metrics"]
        return sum(
            _num(metrics.get(key))
            for key in (
                "input_tokens",
                "output_tokens",
                "worker_input_tokens",
                "worker_output_tokens",
                "subagent_input_tokens",
                "subagent_output_tokens",
            )
        )

    top_token = sorted(records, key=total_tokens, reverse=True)[:10]
    slowest = sorted(
        records,
        key=lambda record: _num(record["metrics"].get("agent_seconds")),
        reverse=True,
    )[:10]
    return {
        "project": project,
        "total": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "pass_rate": round(
            sum(1 for r in records if r["passed"]) / len(records), 4
        )
        if records
        else 0.0,
        "verdicts": dict(verdicts),
        "categories": categories,
        "tiers": dict(tiers),
        "workflows": dict(workflows),
        "tokens": {
            "main_input": _summary(token_in),
            "main_output": _summary(token_out),
            "worker_input": _summary(worker_in),
            "worker_output": _summary(worker_out),
            "subagent_input": _summary(sub_in),
            "subagent_output": _summary(sub_out),
            "total_tokens": round(
                sum(token_in)
                + sum(token_out)
                + sum(worker_in)
                + sum(worker_out)
                + sum(sub_in)
                + sum(sub_out),
                2,
            ),
            "missing_main_usage": sum(
                1 for record in records
                if record["metrics"].get("input_tokens") is None
            ),
        },
        "runtime_seconds": {
            "agent": _summary(agent_seconds),
            "judge": _summary(judge_seconds),
            "wall_seconds": round(wall_seconds, 2),
            "wall_start": min(start_times) if start_times else "",
            "wall_end": max(end_times) if end_times else "",
        },
        "top_token_tasks": [
            {
                "case_id": record["case_id"],
                "tokens": round(total_tokens(record), 2),
                "agent_seconds": round(
                    _num(record["metrics"].get("agent_seconds")), 2
                ),
            }
            for record in top_token
        ],
        "slowest_tasks": [
            {
                "case_id": record["case_id"],
                "agent_seconds": round(
                    _num(record["metrics"].get("agent_seconds")), 2
                ),
                "tokens": round(total_tokens(record), 2),
            }
            for record in slowest
        ],
        "mechanism": {
            "tool_calls": _summary(tool_calls),
            "writes": _summary(writes),
            "steps": _summary(steps),
            "replans_total": sum(replans),
            "workers_total": sum(workers),
            "worker_retries_total": round(retries, 2),
            "worker_merge_conflicts_tasks": merge_conflicts,
            "verifications": dict(verifications),
            "worker_statuses": dict(worker_statuses),
        },
        "failures": failures,
    }


def render_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# LangSmith 量化报告：`{data['project']}`",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 任务数 | {data['total']} |",
        f"| 通过 | {data['passed']} |",
        f"| 通过率 | {data['pass_rate']:.1%} |",
        f"| verdict | {data['verdicts']} |",
        "",
        "## 分类",
        "",
        "| 类别 | 通过 / 总数 | 通过率 | 主 token | worker token | 平均耗时 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for name, bucket in data["categories"].items():
        lines.append(
            f"| {name} | {bucket['passed']} / {bucket['total']} | "
            f"{bucket['pass_rate']:.1%} | {bucket['main_tokens']:.0f} | "
            f"{bucket['worker_tokens']:.0f} | "
            f"{bucket['avg_agent_seconds']:.1f}s |"
        )
    lines += [
        "",
        "## 档位",
        "",
        "| 档位 | 通过 / 总数 | 通过率 |",
        "|---|---|---|",
    ]
    for name, bucket in data["tiers"].items():
        lines.append(
            f"| {name} | {bucket['passed']} / {bucket['total']} | "
            f"{bucket['pass_rate']:.1%} |"
        )

    tokens = data["tokens"]
    runtime = data["runtime_seconds"]
    mech = data["mechanism"]
    lines += [
        "",
        "## Token",
        "",
        "| 项 | sum | avg | p50 | p95 | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in (
        "main_input",
        "main_output",
        "worker_input",
        "worker_output",
        "subagent_input",
        "subagent_output",
    ):
        item = tokens[name]
        lines.append(
            f"| {name} | {item['sum']:.0f} | {item['avg']:.1f} | "
            f"{item['p50']:.0f} | {item['p95']:.0f} | {item['max']:.0f} |"
        )
    lines += [
        f"| **total** | **{tokens['total_tokens']:.0f}** | | | | |",
        "",
        f"缺失主 agent usage：{tokens['missing_main_usage']} 个任务",
        "",
        "> 未配置 provider 单价（`input_cost_per_1m` / `output_cost_per_1m`），",
        "> 因此本报告只统计 token，不计算美元成本；配置账单 API 后可补充金额对账。",
        "",
        "## 运行时间（秒）",
        "",
        "| 项 | sum | avg | p50 | p95 | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("agent", "judge"):
        item = runtime[name]
        lines.append(
            f"| {name} | {item['sum']:.1f} | {item['avg']:.1f} | "
            f"{item['p50']:.1f} | {item['p95']:.1f} | {item['max']:.1f} |"
        )
    lines += [
        "",
        f"实验墙钟：{runtime['wall_seconds']:.1f}s"
        f"（{runtime['wall_start']} .. {runtime['wall_end']}）",
        "",
        "## Token / 耗时 Top 10",
        "",
        "| 任务 | tokens | agent 秒 |",
        "|---|---:|---:|",
    ]
    for item in data["top_token_tasks"]:
        lines.append(
            f"| {item['case_id']} | {item['tokens']:.0f} | "
            f"{item['agent_seconds']:.1f} |"
        )
    lines += [
        "",
        "| 最慢任务 | agent 秒 | tokens |",
        "|---|---:|---:|",
    ]
    for item in data["slowest_tasks"]:
        lines.append(
            f"| {item['case_id']} | {item['agent_seconds']:.1f} | "
            f"{item['tokens']:.0f} |"
        )
    lines += [
        "",
        "## 机制指标",
        "",
        f"- workflow：{data['workflows']}",
        f"- tool_calls：{mech['tool_calls']}",
        f"- writes：{mech['writes']}",
        f"- steps：{mech['steps']}",
        f"- replans 总数：{mech['replans_total']:.0f}",
        f"- worker 总数：{mech['workers_total']:.0f}",
        f"- worker 重试总数：{mech['worker_retries_total']:.0f}",
        f"- 发生合并冲突的任务：{mech['worker_merge_conflicts_tasks']}",
        f"- verifications：{mech['verifications']}",
        f"- worker_statuses：{mech['worker_statuses']}",
        "",
        "## 覆盖说明",
        "",
        "- 本次 workflow 分布是 `direct` + `parallel_readonly`；没有进入 "
        "`orchestrated_implement`，因为评测默认 `orchestrator_write_enabled=false`。"
        "并发 implement/worktree/合并路径由本机生产试跑单独验证。",
        "- 评测默认 `explore_enabled=false`，因此 subagent 计数为 0；"
        "explore/review 子智能体由单测与生产试跑覆盖。",
        "- 要覆盖这两条路径，可显式开启对应配置后跑 `--smoke` 或指定长程任务。",
        "",
        "## 失败任务",
        "",
    ]
    if not data["failures"]:
        lines.append("无。")
    else:
        for item in data["failures"]:
            lines.append(
                f"- **{item['case_id']}**（{item['verdict']}）："
                f"{item['error'] or item['detail'] or '无 detail'}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--json-out", default="tests/eval/reports/langsmith_report.json")
    parser.add_argument("--md-out", default="docs/LANGSMITH_REPORT.md")
    args = parser.parse_args()

    data = collect(args.project)
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(data), encoding="utf-8")
    print(f"写入 {json_path} 和 {md_path}")
    print(
        f"{data['passed']}/{data['total']} passed "
        f"({data['pass_rate']:.1%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
