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
from coding_agent.llm.context import ContextBudget, trim_messages
from coding_agent.llm.prompts import RESPOND_INSTRUCTION, RESPOND_PROMPT

VERIFICATION_FAILED_NOTE = (
    "注意：本次运行的验证（{command}）**失败了**，发现以下问题：\n{issues}\n"
    "{attempts}"
    "请在答复中如实说明任务未通过验证，不要声称已完成。"
)

REPAIRS_EXHAUSTED = (
    "已经连续尝试修复 {attempt} 次仍未通过，请停止再试，"
    "明确指出你认为问题出在哪里、以及需要什么信息或授权才能继续。\n"
)


def make_respond_node(
    llm: BaseChatModel,
    *,
    max_repair_rounds: int = 0,
    budget: ContextBudget | None = None,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    budget = budget or ContextBudget()

    def respond(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        system = RESPOND_PROMPT
        verification = state.get("verification") or {}
        if verification.get("status") == "failed":
            issues = verification.get("issues") or []
            listed = "\n".join(
                f"- {i.get('location', '')} {i.get('message', '')}".strip() for i in issues[:10]
            )
            attempt = state.get("retry", 0)
            exhausted = attempt > 0 and attempt >= max_repair_rounds
            system += "\n\n" + VERIFICATION_FAILED_NOTE.format(
                command=verification.get("command", ""),
                issues=listed or "（无结构化信息）",
                attempts=REPAIRS_EXHAUSTED.format(attempt=attempt) if exhausted else "",
            )

        history, _ = trim_messages(state["messages"], budget)
        messages = [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=RESPOND_INSTRUCTION),
        ]
        return {"messages": [llm.invoke(messages, config)]}

    return respond
