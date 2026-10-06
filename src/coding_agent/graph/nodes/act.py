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
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.llm.context import ContextBudget, trim_messages
from coding_agent.llm.prompts import (
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
                verification, attempt=attempt, limit=max_repair_rounds or attempt
            )

        system = compose_system_prompt(
            plan=state.get("plan"),
            step_idx=state.get("step_idx", 0),
            allow_write=allow_write,
            memories=state.get("memories"),
            feedback=feedback,
        )
        history, _ = trim_messages(state["messages"], trim_budget)
        messages = [SystemMessage(content=system), *history]

        response = await llm.ainvoke(messages, config)
        return {"messages": [response], "tool_rounds": rounds + 1}

    return act
