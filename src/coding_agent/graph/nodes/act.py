"""act 节点：调用模型，产出「工具意图」或「当前步骤的小结」。

关键不变量
----------
**act 只在还有轮次预算时才产出 tool_calls。**

这条不变量让路由可以无条件地把带 tool_calls 的消息送去 tools 执行，
从而绝不会留下「有 tool_calls 却没人应答」的消息 —— 那会让下一轮
OpenAI 兼容接口直接报错。超预算时 act 自己产出一条无 tool_calls 的收尾消息。

配置透传
--------
config 原样传给模型，LangGraph 因此能挂上 token 回调，
CLI 侧用 stream_mode="messages" 实现逐字流式输出。

**这里是 async 节点，走 `llm.ainvoke`。**
同步的 `invoke` 会被 LangGraph 丢进线程池执行：既占满一个线程，又让 Ctrl-C
在模型生成期间完全失效（取消信号传不进线程池里的阻塞 HTTP 调用）。
改用 `ainvoke` 之后取消与超时都能顺着事件循环传下去。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.graph.plan import EXPLORE, IMPLEMENT, current_step_kind
from coding_agent.graph.state import AgentState
from coding_agent.llm.context import (
    ContextBudget,
    is_context_length_error,
    summarize_messages,
    tighter_budget,
    trim_messages,
)
from coding_agent.llm.prompts import (
    DELEGATION_NOTE,
    DELEGATION_NOTE_DISABLED,
    DELEGATION_NOTE_NOT_EXPLORE,
    compose_system_prompt,
    format_review_feedback,
    format_verification_feedback,
)

BUDGET_EXHAUSTED_TEMPLATE = "（本步骤已达到工具调用上限 {limit} 轮，停止继续尝试。）"


def make_act_node(
    llm: BaseChatModel,
    *,
    max_tool_rounds: int,
    allow_write: bool = False,
    max_repair_rounds: int = 0,
    budget: ContextBudget | None = None,
    delegate_enabled: bool = False,
    forced_llm: BaseChatModel | None = None,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    # 裁剪只影响发给模型的内容，不改动写回 state 的历史
    trim_budget = budget or ContextBudget()

    async def act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        rounds = state.get("tool_rounds", 0)

        if rounds >= max_tool_rounds:
            return {
                "messages": [
                    AIMessage(content=BUDGET_EXHAUSTED_TEMPLATE.format(limit=max_tool_rounds))
                ],
                "budget_exhausted": True,
            }

        # 上一步被质量关拦下时，把结构化问题合成进系统提示 ——
        # 不写回消息历史，否则每轮重试都在往上下文里塞一条伪造口吻的消息。
        #
        # **审查优先**：它总是发生在验证之后，所以它的结论更新。两者也不会同时
        # 成立（审查只在验证通过后才跑），只是优先级把这件事写明确。
        feedback = ""
        verification = state.get("verification") or {}
        review = state.get("review") or {}
        attempt = state.get("retry", 0)
        if review.get("status") == "blocked":
            feedback = format_review_feedback(
                review, attempt=attempt, limit=max_repair_rounds or attempt
            )
        elif verification.get("status") == "failed" and attempt > 0:
            feedback = format_verification_feedback(
                verification,
                attempt=attempt,
                limit=max_repair_rounds or attempt,
                same_failure_count=int(state.get("same_failure_count", 0)),
            )

        delegation_note = ""
        if delegate_enabled:
            if not state.get("delegation_allowed", True):
                delegation_note = DELEGATION_NOTE_DISABLED
            elif current_step_kind(state) != EXPLORE:
                delegation_note = DELEGATION_NOTE_NOT_EXPLORE
            else:
                budget = max(1, int(state.get("delegation_budget", 1)))
                delegation_note = DELEGATION_NOTE.format(max_calls=budget)

        system = compose_system_prompt(
            plan=state.get("plan"),
            plan_kinds=state.get("plan_kinds"),
            step_idx=state.get("step_idx", 0),
            step_kind=current_step_kind(state),
            allow_write=allow_write,
            memories=state.get("memories"),
            project_instructions=str(
                state.get("project_instructions") or ""
            ),
            stale_paths=state.get("stale_paths"),
            feedback=feedback,
            # 上一步没产生任何改动、被 nudge 打回来重做 —— 把它明说给模型听，
            # 否则它只会把同样的事再做一遍
            empty_step=bool(state.get("empty_step_nudged")) and not state.get("dirty"),
            # 只有 delegate 工具真的绑定时才提；否则会教模型调用一个不存在的工具
            delegation_note=delegation_note,
            worker_context=str(state.get("worker_context") or ""),
            context_summary=summarize_messages(state["messages"]),
        )
        history, trim_report = trim_messages(state["messages"], trim_budget)
        messages = [SystemMessage(content=system), *history]

        # 实测 deepseek-chat 在 implement 步偶尔会连续返回「无 tool_calls」的
        # 空动作（读完文件但不动手）。被 nudge 之后的第一次调用强制 required，
        # 至少让它先读目标文件；后续调用恢复自动选择，留出编辑机会。
        force_tool = (
            forced_llm is not None
            and current_step_kind(state) == IMPLEMENT
            and bool(state.get("empty_step_nudged"))
            and not state.get("dirty")
            and int(state.get("tool_rounds", 0)) == 0
        )
        model = forced_llm if force_tool else llm
        async def _retry_tighter(
            _exc: BaseException, current: dict[str, Any]
        ):
            tighter = tighter_budget(trim_budget)
            tighter_history, tighter_report = trim_messages(
                current["messages"], tighter
            )
            tighter_messages = [
                SystemMessage(
                    content=compose_system_prompt(
                        plan=current.get("plan"),
                        plan_kinds=current.get("plan_kinds"),
                        step_idx=current.get("step_idx", 0),
                        step_kind=current_step_kind(current),
                        allow_write=allow_write,
                        memories=current.get("memories"),
                        project_instructions=str(
                            current.get("project_instructions") or ""
                        ),
                        stale_paths=current.get("stale_paths"),
                        feedback=feedback,
                        empty_step=bool(current.get("empty_step_nudged"))
                        and not current.get("dirty"),
                        delegation_note=delegation_note,
                        worker_context=str(
                            current.get("worker_context") or ""
                        ),
                        context_summary=summarize_messages(
                            current["messages"]
                        ),
                    )
                ),
                *tighter_history,
            ]
            return await llm.ainvoke(tighter_messages, config), tighter_report

        try:
            response = await model.ainvoke(messages, config)
        except Exception as exc:  # noqa: BLE001
            if force_tool:
                # 强制 tool_choice 在个别 provider/模式下可能被拒；退回普通调用，
                # 不要让一次强制失败把整个运行打断。
                try:
                    response = await llm.ainvoke(messages, config)
                except Exception as fallback_exc:  # noqa: BLE001
                    if not is_context_length_error(fallback_exc):
                        raise
                    response, trim_report = await _retry_tighter(
                        fallback_exc, state
                    )
            elif is_context_length_error(exc):
                response, trim_report = await _retry_tighter(exc, state)
            else:
                raise
        return {
            "messages": [response],
            "tool_rounds": rounds + 1,
            "context_trim": {
                **asdict(trim_report),
                "node": "act",
            },
        }

    return act
