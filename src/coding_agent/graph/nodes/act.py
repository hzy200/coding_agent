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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.llm.prompts import compose_system_prompt

BUDGET_EXHAUSTED_TEMPLATE = "（本步骤已达到工具调用上限 {limit} 轮，停止继续尝试。）"


def make_act_node(
    llm: BaseChatModel,
    *,
    max_tool_rounds: int,
    allow_write: bool = False,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        rounds = state.get("tool_rounds", 0)

        if rounds >= max_tool_rounds:
            return {
                "messages": [
                    AIMessage(content=BUDGET_EXHAUSTED_TEMPLATE.format(limit=max_tool_rounds))
                ],
                "budget_exhausted": True,
            }

        system = compose_system_prompt(
            plan=state.get("plan"),
            step_idx=state.get("step_idx", 0),
            allow_write=allow_write,
        )
        messages = [SystemMessage(content=system), *state["messages"]]

        response = llm.invoke(messages, config)
        return {"messages": [response], "tool_rounds": rounds + 1}

    return act
