"""计划 kind 的对齐与兼容。

`plan` 是文本列表、`plan_kinds` 是并行列表；两者一旦错位，路由就会用错类型
（例如把 explore 当成 implement，或反过来）。这里钉住补位、截断、归一化与
旧格式（纯字符串）的兼容。
"""

from __future__ import annotations

from coding_agent.graph.plan import (
    IMPLEMENT,
    UNKNOWN,
    PlanStepModel,
    align_kinds,
    current_step_kind,
)


def test_align_kinds_pads_missing_values_with_unknown() -> None:
    assert align_kinds(["甲", "乙", "丙"], ["explore"]) == [
        "explore",
        UNKNOWN,
        UNKNOWN,
    ]


def test_align_kinds_truncates_extra_values() -> None:
    assert align_kinds(["甲"], ["explore", "implement"]) == ["explore"]


def test_align_kinds_normalizes_invalid_values() -> None:
    assert align_kinds(["甲"], ["Implement", "nonsense"]) == [IMPLEMENT]


def test_current_step_kind_falls_back_to_unknown() -> None:
    assert current_step_kind({"plan": ["甲", "乙"], "step_idx": 1}) == UNKNOWN
    assert current_step_kind({"plan": ["甲"], "step_idx": 9}) == UNKNOWN
    assert current_step_kind({}) == UNKNOWN


def test_plan_step_model_coerces_legacy_strings() -> None:
    step = PlanStepModel.model_validate("读一下测试")
    assert step.text == "读一下测试"
    assert step.kind == UNKNOWN


def test_plan_step_model_normalizes_kind_case() -> None:
    step = PlanStepModel.model_validate({"text": "改文件", "kind": "IMPLEMENT"})
    assert step.kind == IMPLEMENT
