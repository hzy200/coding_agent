"""respond 节点：汇总执行过程，产出面向用户的最终答复。

用不带工具的模型实例，避免它在收尾阶段又发起新的动作。

与 act 一样是 **async 节点**：收尾这次调用往往最长，若走同步 `invoke`，
Ctrl-C 在它生成期间不生效（见 graph/nodes/act.py 的说明）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.llm.context import ContextBudget, trim_messages
from coding_agent.llm.prompts import RESPOND_INSTRUCTION, RESPOND_PROMPT
from coding_agent.messages import text_of

VERIFICATION_FAILED_NOTE = (
    "注意：本次运行的验证（{command}）**失败了**。\n"
    "结果：{summary}\n"
    "发现以下问题：\n{issues}\n"
    "{attempts}"
    "请在答复中如实说明任务未通过验证，不要声称已完成。"
)

REPAIRS_EXHAUSTED = (
    "已经连续尝试修复 {attempt} 次仍未通过，请停止再试，"
    "明确指出你认为问题出在哪里、以及需要什么信息或授权才能继续。\n"
)

BUDGET_EXHAUSTED_NOTE = (
    "注意：有一步在达到工具调用上限时停下，且没有产生任何文件改动，"
    "该步骤很可能并没有完成。请在答复中如实说明未完成的部分，不要声称任务已完成。"
)

NO_CHANGES_NOTE = (
    "注意：本次运行**没有产生任何文件改动**。如果任务要求实现或修改代码，"
    "不要声称已经完成；请如实说明未完成的部分和你尝试过的方向。"
)

CANCELLED_NOTE = (
    "注意：用户已**取消**本次运行。请简要说明已经完成的部分、"
    "哪些工作没有执行，以及恢复运行需要什么信息；不要声称任务已全部完成。"
)


def make_respond_node(
    llm: BaseChatModel,
    *,
    max_repair_rounds: int = 0,
    budget: ContextBudget | None = None,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    budget = budget or ContextBudget()

    async def respond(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        system = RESPOND_PROMPT
        # replan 会清空 verification（旧结论针对旧做法）；收尾要看运行级快照。
        verification = (
            state.get("last_verification")
            or state.get("verification")
            or {}
        )
        if verification.get("status") == "failed":
            issues = verification.get("issues") or []
            listed = "\n".join(
                f"- {i.get('location', '')} {i.get('message', '')}".strip() for i in issues[:10]
            )
            attempt = state.get("retry", 0)
            exhausted = attempt > 0 and attempt >= max_repair_rounds
            system += "\n\n" + VERIFICATION_FAILED_NOTE.format(
                command=verification.get("command", ""),
                summary=str(verification.get("summary", "")).strip() or "（无摘要）",
                issues=listed or "（无结构化信息）",
                attempts=REPAIRS_EXHAUSTED.format(attempt=attempt) if exhausted else "",
            )

        # 与 routing 的判定对齐：预算耗尽且没改动，说明这一步没做成
        if state.get("budget_exhausted") and not state.get("dirty"):
            system += "\n\n" + BUDGET_EXHAUSTED_NOTE

        # 运行级护栏：多步任务一次写操作都没有，收尾时必须提醒模型别编造完成。
        # 实测踩过：委派探索把「读」做完了，主 agent 一步没写，却在最终答复里
        # 声称「功能已实现、测试全过」。判定层会判负，但用户看到的是假成功。
        if not state.get("run_dirty") and len(state.get("plan") or []) > 1:
            system += "\n\n" + NO_CHANGES_NOTE

        if state.get("steering_cancelled"):
            system += "\n\n" + CANCELLED_NOTE

        history, _ = trim_messages(state["messages"], budget)
        messages = [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=RESPOND_INSTRUCTION),
        ]
        answer = await llm.ainvoke(messages, config)

        # 确定性护栏：模型可能忽略提示，仍然声称成功。验证失败是事实，
        # 直接追加到用户可见的答复里，不依赖模型自觉。
        if verification.get("status") == "failed":
            summary = str(verification.get("summary", "")).strip()
            note = (
                "\n\n（系统提示：本次运行最后一次自动验证未通过"
                + (f"：{summary}" if summary else "")
                + "。以上答复不能视为任务已完成。）"
            )
            answer = answer.model_copy(
                update={"content": text_of(answer).rstrip() + note}
            )

        # 取消是用户可见的事实，同样不能只靠模型自觉。
        if state.get("steering_cancelled"):
            answer = answer.model_copy(
                update={
                    "content": text_of(answer).rstrip()
                    + "\n\n（系统提示：本次运行已按用户要求取消。）"
                }
            )
        return {"messages": [answer]}

    return respond
