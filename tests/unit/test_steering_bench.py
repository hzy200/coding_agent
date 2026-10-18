"""干预延迟基准本身要能跑、且口径正确（不等同于测试延迟阈值）。"""

from __future__ import annotations

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from steering_bench import run_benchmark  # noqa: E402


def test_benchmark_reports_pause_resume_and_cancel() -> None:
    report = run_benchmark(repeat=1, delay_ms=10)

    assert report["delay_ms"] == 10
    assert report["repeat"] == 1
    for scenario in ("pause_resume", "cancel"):
        stats = report[scenario]
        assert {"wait_ms", "apply_ms", "total_ms"} <= set(stats)
        for values in stats.values():
            assert values["min"] >= 0
            assert values["min"] <= values["median"] <= values["max"]
    # pause 场景额外测了恢复耗时。这里只断言“可测且非负”——
    # 10ms 级的墙钟在并行测试负载下会被调度延迟放大/缩短，不能当阈值断言。
    assert "resume_ms" in report["pause_resume"]
