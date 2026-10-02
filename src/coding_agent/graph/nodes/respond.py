"""respond 节点：汇总执行过程，产出面向用户的最终答复。

用不带工具的模型实例，避免它在收尾阶段又发起新的动作。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.llm.prompts import RESPOND_INSTRUCTION, RESPOND_PROMPT


def make_respond_node(llm: BaseChatModel) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def respond(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        messages = [
            SystemMessage(content=RESPOND_PROMPT),
            *state["messages"],
            HumanMessage(content=RESPOND_INSTRUCTION),
        ]
        return {"messages": [llm.invoke(messages, config)]}

    return respond
