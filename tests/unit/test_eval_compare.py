"""baseline 对比工具的噪声守卫。

它存在的唯一理由：单次运行的差值不能当结论。所以这里守三件事——
差值小于 spread 时明确拒绝下结论、臂内翻过的任务不能归因于改动、
重复次数不足时不给 verdict。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from compare import compare  # noqa: E402


def _baseline(
    *,
    suite: str = "default",
    model: str = "deepseek-chat",
    rates: tuple[float, ...] = (0.5, 0.6),
    verdicts: dict[str, str] | None = None,
    flipped: list[str] | None = None,
    mechanism: dict[str, Any] | None = None,
    comparable: bool = True,
) -> dict[str, Any]:
    verdicts = verdicts or {"t1": "passed", "t2": "failed"}
    spread = max(rates) - min(rates) if len(rates) > 1 else 0.0
    return {
        "suite": suite,
        "model": model,
        "passed": sum(value == "passed" for value in verdicts.values()),
        "total": len(verdicts),
        "pass_rate": sum(rates) / len(rates),
        "tasks": [{"id": key, "verdict": value} for key, value in verdicts.items()],
        "noise": {
            "pass_rate_per_run": list(rates),
            "spread": spread,
            "flipped": flipped or [],
        },
        "mechanism": mechanism or {},
        "comparable": comparable,
    }


def test_delta_within_spread_refuses_conclusions() -> None:
    left = _baseline(rates=(0.5, 0.6))
    right = _baseline(rates=(0.55, 0.65))

    report = compare(left, right)

    assert report["pass_rate"]["delta"] == 0.05
    assert report["pass_rate"]["noise_band"] == 0.1
    assert report["pass_rate"]["verdict"] == "within_noise"
    assert "不能" in report["pass_rate"]["note"]


def test_delta_beyond_spread_is_reported_as_outside_noise() -> None:
    left = _baseline(rates=(0.5, 0.6))
    right = _baseline(rates=(0.7, 0.8))

    report = compare(left, right)

    assert report["pass_rate"]["delta"] == 0.2
    assert report["pass_rate"]["verdict"] == "outside_noise"


def test_single_run_cannot_report_a_verdict() -> None:
    report = compare(_baseline(rates=(0.5,)), _baseline(rates=(0.8,)))

    assert report["pass_rate"]["verdict"] == "insufficient_repeats"
    assert report["pass_rate"]["noise_band"] is None


def test_different_task_sets_are_incomparable() -> None:
    left = _baseline(verdicts={"t1": "passed"})
    right = _baseline(verdicts={"t2": "passed"})

    report = compare(left, right)

    assert report["pass_rate"]["verdict"] == "incomparable"
    assert any("任务集合不同" in w for w in report["warnings"])


def test_different_suites_are_incomparable() -> None:
    left = _baseline(suite="default")
    right = _baseline(suite="long")

    report = compare(left, right)

    assert report["pass_rate"]["verdict"] == "incomparable"
    assert any("套件不同" in w for w in report["warnings"])


def test_flip_that_also_happened_inside_an_arm_is_noise_suspect() -> None:
    left = _baseline(
        verdicts={"t1": "passed", "t2": "failed"}, flipped=["t1"]
    )
    right = _baseline(verdicts={"t1": "failed", "t2": "failed"})

    report = compare(left, right)

    assert report["tasks"]["flipped"] == ["t1"]
    assert report["tasks"]["noise_suspect"] == ["t1"]


def test_clean_flip_is_not_marked_as_noise() -> None:
    left = _baseline(verdicts={"t1": "passed", "t2": "failed"}, flipped=[])
    right = _baseline(verdicts={"t1": "failed", "t2": "passed"})

    report = compare(left, right)

    assert report["tasks"]["flipped"] == ["t1", "t2"]
    assert report["tasks"]["noise_suspect"] == []


def test_mechanism_deltas_include_worker_cost_and_parallelism() -> None:
    left = _baseline(
        mechanism={
            "worker_input_tokens": 100,
            "total_input_tokens": 400,
            "worker_seconds": 2.0,
            "worker_parallelism": 0.5,
            "worker_conflicts": 0,
        }
    )
    right = _baseline(
        mechanism={
            "worker_input_tokens": 300,
            "total_input_tokens": 800,
            "worker_seconds": 6.0,
            "worker_parallelism": 1.5,
            "worker_conflicts": 1,
        }
    )

    metrics = compare(left, right)["metrics"]

    assert metrics["worker_input_tokens"]["delta"] == 200
    assert metrics["worker_input_tokens"]["delta_percent"] == 200.0
    assert metrics["total_input_tokens"]["delta"] == 400
    assert metrics["worker_parallelism"]["delta"] == 1.0
    assert metrics["worker_conflicts"]["delta"] == 1.0


def test_non_comparable_side_is_surfaced_as_a_warning() -> None:
    left = _baseline(comparable=False)
    right = _baseline()

    report = compare(left, right)

    assert any("comparable=false" in warning for warning in report["warnings"])
