"""计划步骤的 kind 契约。

`plan` 仍然是纯文本列表（事件、前端、评测都依赖它），`plan_kinds` 是与之
**逐项对齐**的并行列表。这比把 plan 改成 dict 列表的改动面小得多，也保留了
`PlanCreated.steps: list[str]` 这条已经稳定的前端契约。

kind 的语义：

- `explore`：只读定位/理解，**不要求**产生改动；
- `implement`：必须产生实际改动（文件/依赖/git 索引），只读不算完成；
- `verify`：跑测试/构建/检查，不要求改动；
- `unknown`：规划降级或旧 checkpoint 的缺省值，按原有宽松行为处理。

`implement + dirty=False` 是这次要修的核心缺陷：委派探索可以完成「读」，
但主 agent 不能因此把实现步当成做完了。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EXPLORE = "explore"
IMPLEMENT = "implement"
VERIFY = "verify"
UNKNOWN = "unknown"

VALID_KINDS = frozenset({EXPLORE, IMPLEMENT, VERIFY, UNKNOWN})


class PlanStepModel(BaseModel):
    """规划/重规划共用的步骤 schema。

    兼容旧输出：模型只给字符串时按 `kind=unknown` 处理（宽松路由），
    而不是让整次结构化调用失败并触发规划降级。
    """

    text: str = Field(description="一句话，描述做什么以及怎样算完成")
    kind: Literal["explore", "implement", "verify", "unknown"] = Field(
        default=UNKNOWN,
        description="explore=只读定位；implement=必须改文件；verify=跑测试/检查",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"text": value, "kind": UNKNOWN}
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        """大小写/未知值归一，别让一个拼错的 kind 触发整次规划降级。"""
        text = str(value).strip().lower()
        return text if text in VALID_KINDS else UNKNOWN


def align_kinds(plan: list[str], kinds: list[str] | None) -> list[str]:
    """把 kinds 补/截到与 plan 等长；非法值归为 unknown。"""
    values = []
    for kind in kinds or []:
        text = str(kind).strip().lower()
        values.append(text if text in VALID_KINDS else UNKNOWN)
    if len(values) < len(plan):
        values.extend([UNKNOWN] * (len(plan) - len(values)))
    return values[: len(plan)]


def current_step_kind(state: Any) -> str:
    """当前 step_idx 对应的 kind；越界或缺失时为 unknown。"""
    plan = state.get("plan") or []
    kinds = align_kinds(plan, state.get("plan_kinds"))
    index = int(state.get("step_idx", 0))
    if 0 <= index < len(kinds):
        return kinds[index]
    return UNKNOWN
