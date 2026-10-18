"""噪声感知的 baseline 对比。

用法：

    python tests/eval/compare.py a.json b.json [--json]

为什么不是「打印两个通过率」：
单次运行的差值说明不了任何事 —— 同一份代码曾跑出 52% 与 83%。本工具把
两臂各自观测到的 `spread` 取大者作为噪声带，**差值落在带内就明确拒绝下结论**。
`spread` 不是置信区间，只是这把尺子实际能分辨的量级；重复次数不足 2 时
连这个量级都没有，直接报 `insufficient_repeats`。

逐任务翻转也一样：一个任务若在**任一臂内部**就跑出过不同结果，那它两臂之间的
差异同样不能归因于改动（标为 `noise_suspect`）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

# 需要对比的机制指标。None 表示该版本没有记录这个字段（老 baseline）。
_METRICS = (
    "agent_seconds",
    "input_tokens",
    "output_tokens",
    "worker_input_tokens",
    "worker_output_tokens",
    "total_input_tokens",
    "total_output_tokens",
    "workers",
    "max_worker_batch",
    "worker_seconds",
    "worker_parallelism",
    "worker_merged",
    "worker_merge_attempts",
    "worker_conflicts",
    "merge_success_rate",
    "conflict_rate",
    "repairs",
    "replans",
)


def load_baseline(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 baseline JSON 对象")
    return data


def _pass_rates(data: dict[str, Any]) -> list[float]:
    noise = data.get("noise") or {}
    rates = noise.get("pass_rate_per_run")
    if rates:
        return [float(rate) for rate in rates]
    if data.get("pass_rate") is not None:
        return [float(data["pass_rate"])]
    total = int(data.get("total") or 0)
    return [int(data.get("passed") or 0) / total] if total else []


def _spread(data: dict[str, Any], rates: list[float]) -> float | None:
    noise = data.get("noise") or {}
    if noise.get("spread") is not None:
        return float(noise["spread"])
    if len(rates) >= 2:
        return max(rates) - min(rates)
    return None


def _task_verdicts(data: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("id", "")): str(row.get("verdict", ""))
        for row in data.get("tasks") or []
    }


def _internal_flips(data: dict[str, Any]) -> set[str]:
    return {str(task_id) for task_id in (data.get("noise") or {}).get("flipped") or []}


def _metric(data: dict[str, Any], key: str) -> float | None:
    mechanism = data.get("mechanism") or {}
    value = mechanism.get(key)
    if value is None and key == "agent_seconds":
        value = sum(
            float(row.get("agent_seconds") or 0) for row in data.get("tasks") or []
        )
    if value is None:
        return None
    return float(value)


def compare(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    """对比两个 baseline；`right - left` 为差值方向。"""
    warnings: list[str] = []
    rates_left = _pass_rates(left)
    rates_right = _pass_rates(right)
    spread_left = _spread(left, rates_left)
    spread_right = _spread(right, rates_right)

    if left.get("suite") != right.get("suite"):
        warnings.append(
            f"套件不同：{left.get('suite')} vs {right.get('suite')}，通过率不可直接比较"
        )
    if left.get("model") != right.get("model"):
        warnings.append(
            f"模型不同：{left.get('model')} vs {right.get('model')}"
        )
    for label, data in (("左", left), ("右", right)):
        if data.get("comparable") is False:
            warnings.append(
                f"{label}侧标记 comparable=false：{data.get('comparable_reason') or '原因未记录'}"
            )

    verdicts_left = _task_verdicts(left)
    verdicts_right = _task_verdicts(right)
    same_tasks = set(verdicts_left) == set(verdicts_right)
    if not same_tasks:
        warnings.append("任务集合不同（或有一侧缺少逐任务行），只能比较聚合值")

    mean_left = round(statistics.fmean(rates_left), 4) if rates_left else None
    mean_right = round(statistics.fmean(rates_right), 4) if rates_right else None
    delta = (
        round(mean_right - mean_left, 4)
        if mean_left is not None and mean_right is not None
        else None
    )

    # 重复次数不足 2 时没有"运行间波动"这个量级，直接拒绝下结论。
    if len(rates_left) < 2 or len(rates_right) < 2:
        noise_verdict = "insufficient_repeats"
        noise_band = None
    elif not same_tasks or left.get("suite") != right.get("suite"):
        noise_verdict = "incomparable"
        noise_band = None
    else:
        noise_band = round(max(spread_left or 0.0, spread_right or 0.0), 4)
        noise_verdict = (
            "within_noise"
            if delta is not None and abs(delta) <= noise_band
            else "outside_noise"
        )

    flips = sorted(
        task_id
        for task_id in set(verdicts_left) & set(verdicts_right)
        if verdicts_left[task_id] != verdicts_right[task_id]
    )
    internally_noisy = _internal_flips(left) | _internal_flips(right)
    noise_suspect = [task_id for task_id in flips if task_id in internally_noisy]

    metrics: dict[str, dict[str, float | None]] = {}
    for key in _METRICS:
        value_left = _metric(left, key)
        value_right = _metric(right, key)
        if value_left is None and value_right is None:
            continue
        delta_value = (
            value_right - value_left
            if value_left is not None and value_right is not None
            else None
        )
        percent = (
            round(delta_value / value_left * 100, 1)
            if delta_value is not None and value_left
            else None
        )
        metrics[key] = {
            "left": value_left,
            "right": value_right,
            "delta": round(delta_value, 4) if delta_value is not None else None,
            "delta_percent": percent,
        }

    return {
        "left": {
            "path": left.get("_path", ""),
            "suite": left.get("suite"),
            "model": left.get("model"),
            "pass_rate_mean": mean_left,
            "spread": spread_left,
            "repeats": len(rates_left),
            "comparable": left.get("comparable"),
        },
        "right": {
            "path": right.get("_path", ""),
            "suite": right.get("suite"),
            "model": right.get("model"),
            "pass_rate_mean": mean_right,
            "spread": spread_right,
            "repeats": len(rates_right),
            "comparable": right.get("comparable"),
        },
        "pass_rate": {
            "delta": delta,
            "noise_band": noise_band,
            "verdict": noise_verdict,
            "note": (
                "差值落在两臂观测到的最大 spread 之内：不能把差异归因于改动。"
                if noise_verdict == "within_noise"
                else "差值大于观测到的 spread；仍需检查逐任务翻转与机制指标。"
                if noise_verdict == "outside_noise"
                else "重复次数不足或口径不可比，无法判断差异是否超过噪声。"
            ),
        },
        "tasks": {
            "flipped": flips,
            "noise_suspect": noise_suspect,
        },
        "metrics": metrics,
        "warnings": warnings,
    }


def _fmt_percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _fmt_number(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _print_report(report: dict[str, Any]) -> None:
    left = report["left"]
    right = report["right"]
    print(f"套件：{left['suite']} vs {right['suite']}")
    print(f"模型：{left['model']} vs {right['model']}")
    print(
        f"通过率：{_fmt_percent(left['pass_rate_mean'])}"
        f"（spread {_fmt_percent(left['spread'])}，{left['repeats']} 轮）"
        f" vs {_fmt_percent(right['pass_rate_mean'])}"
        f"（spread {_fmt_percent(right['spread'])}，{right['repeats']} 轮）"
    )
    pass_rate = report["pass_rate"]
    delta = pass_rate["delta"]
    if delta is not None:
        print(f"Δ：{delta:+.1%}    噪声带：{_fmt_percent(pass_rate['noise_band'])}")
    print(f"结论：{pass_rate['note']}")
    if report["warnings"]:
        print("警告：")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    if report["tasks"]["flipped"]:
        print("逐任务翻转：" + "、".join(report["tasks"]["flipped"]))
    if report["tasks"]["noise_suspect"]:
        print(
            "噪声可疑翻转（臂内也翻过，不能归因于改动）："
            + "、".join(report["tasks"]["noise_suspect"])
        )
    if report["metrics"]:
        print("\n机制指标：")
        print(f"  {'指标':<24} {'左':>12} {'右':>12} {'Δ':>12}")
        for key, values in report["metrics"].items():
            print(
                f"  {key:<24} {_fmt_number(values['left']):>12}"
                f" {_fmt_number(values['right']):>12}"
                f" {_fmt_number(values['delta']):>12}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="带噪声守卫的 baseline 对比")
    parser.add_argument("left", type=Path, help="左/基线 baseline JSON")
    parser.add_argument("right", type=Path, help="右/对照 baseline JSON")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args(argv)

    try:
        left = load_baseline(args.left)
        right = load_baseline(args.right)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法读取 baseline：{exc}", file=sys.stderr)
        return 2
    left["_path"] = str(args.left)
    right["_path"] = str(args.right)

    report = compare(left, right)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0 if report["pass_rate"]["verdict"] != "incomparable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
