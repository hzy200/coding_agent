from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.graph.routing import (
    ADVANCE,
    APPROVE,
    NUDGE,
    REPAIR,
    REPLAN,
    RESPOND,
    REVIEW,
    VERIFY,
    has_more_steps,
    make_route_after_review,
    make_route_after_verify,
    route_after_act,
)

MAX_REPAIRS = 3
route_after_verify = make_route_after_verify(MAX_REPAIRS)
route_after_review = make_route_after_review(MAX_REPAIRS)
# 关掉「空步骤重做」的那一版 —— 用来验证这个开关确实能回到旧行为
route_after_review_no_nudge = make_route_after_review(MAX_REPAIRS, nudge_empty_steps=False)


def _state(messages, plan=None, step_idx=0, *, dirty=True):
    """默认 `dirty=True`。

    这一步的改动是**有意义的**：`dirty=False` 且没被 nudge 过时会走重做那条路
    （见 `nodes/nudge.py`），而本文件大半用例考的是"改动之后往哪去"。
    """
    return {"messages": messages, "plan": plan or [], "step_idx": step_idx, "dirty": dirty}


# ---------------- act 之后 ----------------

def test_step_done_goes_to_verify() -> None:
    """一步做完先验证，再决定步进还是收尾。"""
    state = _state([HumanMessage(content="hi"), AIMessage(content="done")], ["only"], 0)
    assert route_after_act(state) == VERIFY


def test_tool_calls_go_through_approval_gate_first() -> None:
    """只要 act 产出了 tool_calls，就必须先过审批关卡，不能直达 tools。"""
    call = AIMessage(content="", tool_calls=[{"name": "shell_exec", "args": {}, "id": "1"}])
    state = _state([call], ["a", "b"], 0)
    assert route_after_act(state) == APPROVE


def test_tool_message_does_not_look_like_tool_call() -> None:
    state = _state([ToolMessage(content="out", tool_call_id="1")], ["a"], 0)
    assert route_after_act(state) == VERIFY


# ---------------- verify 之后 ----------------

def test_verify_passing_goes_to_review_not_straight_to_advance() -> None:
    """测试通过 ≠ 代码正确 —— 通过之后还有第二道关。"""
    state = _state([AIMessage(content="step1 done")], ["a", "b"], 0)
    assert route_after_verify(state) == REVIEW


def test_review_then_advance_when_steps_remain() -> None:
    state = _state([AIMessage(content="step1 done")], ["a", "b"], 0)
    assert has_more_steps(state)
    assert route_after_review(state) == ADVANCE


def test_review_then_respond_on_last_step() -> None:
    state = _state([AIMessage(content="step2 done")], ["a", "b"], 1)
    assert not has_more_steps(state)
    assert route_after_review(state) == RESPOND


def test_review_then_respond_when_plan_empty() -> None:
    assert route_after_review(_state([AIMessage(content="done")], [], 0)) == RESPOND


# ---------------- 失败驱动的修复循环 ----------------

def test_failed_verification_enters_repair_when_budget_remains() -> None:
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed", "summary": "1 failed"}
    state["retry"] = 0
    assert route_after_verify(state) == REPAIR


def test_failed_verification_hands_over_to_replan() -> None:
    """修复额度用尽不是直接收尾 —— 先交给 replan 判断「换个做法还能不能成」。

    上限仍然是硬性的：没有它，自动修复就是个烧 token 的无限循环，而且用户
    永远拿不到「我试过了但没修好」这个结论。replan 同样有硬性上限，
    它也没辙时会把剩余步骤砍掉，再由 route_after_replan 送去收尾。
    """
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == REPLAN


def test_repair_budget_counts_up_to_the_limit() -> None:
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    for attempt in range(MAX_REPAIRS):
        state["retry"] = attempt
        assert route_after_verify(state) == REPAIR, attempt
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == REPLAN


def test_repair_budget_is_per_step_not_global() -> None:
    """还有子任务时，预算耗尽也只是结束本步，不是结束整个任务。"""
    state = _state([AIMessage(content="done")], ["a", "b"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == REPLAN  # 本步交给 replan，再决定去留


def test_passing_verification_ignores_retry_counter() -> None:
    state = _state([AIMessage(content="done")], ["a", "b"], 0)
    state["verification"] = {"status": "ok"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == REVIEW


# ---------------- 代码审查之后 ----------------

def test_review_blocking_enters_repair_when_budget_remains() -> None:
    """审查的阻断与验证的失败走同一条出口 —— 对这一步来说含义相同：做出来了但不合格。"""
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "ok"}
    state["review"] = {"status": "blocked", "summary": "1 个阻断问题"}
    state["retry"] = 0
    assert route_after_review(state) == REPAIR


def test_review_blocking_goes_to_replan_when_budget_exhausted() -> None:
    """额度用尽时换一种做法，而不是硬试 —— 与验证失败的处理一致。"""
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["review"] = {"status": "blocked"}
    state["retry"] = MAX_REPAIRS
    assert route_after_review(state) == REPLAN


@pytest.mark.parametrize("status", ["clean", "warned", "skipped"])
def test_review_warnings_do_not_block(status: str) -> None:
    """只有 blocking 才改控制流。告警是记录用的，不该把任务卡住。

    `skipped` 也在这里：审查没跑起来（脚本坏了、开关关了）不该等于任务失败。
    """
    state = _state([AIMessage(content="done")], ["a", "b"], 0)
    state["review"] = {"status": status}
    assert route_after_review(state) == ADVANCE


def test_verify_does_not_reroute_on_a_stale_review_block() -> None:
    """verify 的路由只看验证本身 —— 审查阻断由 review 那一跳负责。

    两边都判同一个字段会让"验证通过但审查阻断"被处理两次，修复预算被双倍消耗。
    """
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "ok"}
    state["review"] = {"status": "blocked"}
    assert route_after_verify(state) == REVIEW


def test_zero_budget_disables_the_repair_loop() -> None:
    """修复预算为 0 时一次都不重试，直接交给 replan。"""
    strict = make_route_after_verify(0)
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = 0
    assert strict(state) == REPLAN


# ---------------- 工具预算耗尽 ----------------

def test_budget_exhausted_without_changes_stops_to_report() -> None:
    """预算耗尽且没产生任何改动：不静默跳到下一步，停下来如实上报。"""
    state = _state([AIMessage(content="停下了")], ["a", "b"], 0)
    state["budget_exhausted"] = True
    state["dirty"] = False
    assert route_after_verify(state) == RESPOND


def test_budget_exhausted_with_changes_still_advances() -> None:
    """改过东西说明这一步有产出，不该被预算标记截断后续步骤。

    预算标记的例外只发生在 verify 那一跳；真正的步进判定在 review 之后。
    """
    state = _state([AIMessage(content="改完了")], ["a", "b"], 0)
    state["budget_exhausted"] = True
    state["dirty"] = True
    assert route_after_verify(state) == REVIEW
    assert route_after_review(state) == ADVANCE


def test_budget_exhausted_does_not_override_failed_verification() -> None:
    """验证失败仍走修复循环，预算标记不能盖过它。"""
    state = _state([AIMessage(content="x")], ["a"], 0)
    state["budget_exhausted"] = True
    state["dirty"] = False
    state["verification"] = {"status": "failed"}
    state["retry"] = 0
    assert route_after_verify(state) == REPAIR


# ---------------- 空步骤重做（nudge） ----------------
#
# `verify` 与 `review` 都只在 `dirty` 时跑。所以「这一步只读了文件」与
# 「这一步做完了」在图上长得一样 —— 实测里模型因此把整个计划"读"完就算完成
# （10 次运行里 5 次一次写都没尝试过）。这一组钉住那条补救路径。

def test_a_step_with_no_change_is_sent_back_to_act() -> None:
    state = _state([AIMessage(content="我读完了")], ["a", "b"], 0, dirty=False)
    assert route_after_review(state) == NUDGE


def test_the_nudge_happens_only_once_per_step() -> None:
    """只读步骤是合法的（计划第一步常常就是探索），所以只给一次机会。

    无限要求"必须有改动"会逼出无意义的改动。
    """
    state = _state([AIMessage(content="这一步只需只读")], ["a", "b"], 0, dirty=False)
    state["empty_step_nudged"] = True
    assert route_after_review(state) == ADVANCE


def test_nudged_last_step_still_responds() -> None:
    state = _state([AIMessage(content="只读")], ["a"], 0, dirty=False)
    state["empty_step_nudged"] = True
    assert route_after_review(state) == RESPOND


def test_the_nudge_can_be_turned_off() -> None:
    """关闭时必须回到旧行为 —— 现有基线要有一条可比的路。"""
    state = _state([AIMessage(content="只读")], ["a", "b"], 0, dirty=False)
    assert route_after_review_no_nudge(state) == ADVANCE
