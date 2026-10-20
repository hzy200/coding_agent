"""上下文/记忆专项基准的回归护栏。

基准本身是 `tests/eval/context_memory_bench.py`（确定性、无需 WSL/模型）。
这里只钉住几条不能退化的指标，避免标尺自己坏掉。
"""

from __future__ import annotations

from tests.eval.context_memory_bench import memory_benchmark, trim_benchmark


def test_long_session_compaction_keeps_task_facts() -> None:
    result = trim_benchmark()
    assert result["retention"] == 1.0
    assert result["missing"] == []
    # 压缩必须真的发生，否则「保持率 100%」没有意义
    assert result["compression"] > 0.5
    assert result["under_token_budget"]


def test_memory_recall_and_safety_behaviours() -> None:
    result = memory_benchmark(limit=5)
    # 纯词法检索，允许个别语义改写查询未命中
    assert result["recall_at_k"] >= 0.9
    assert result["fallback_on_no_match"] is True
    assert result["conflict_replaced"] is True
    assert result["expiry_filtered"] is True
