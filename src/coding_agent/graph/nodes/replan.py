"""replan 节点：一步做完后，重新审视剩下的计划。

`planner` 只在开局跑一次。跟着一个过时的计划走到黑，是「自主」二字的直接否定：
第 2 步的发现可能让第 3、4 步变得没必要，也可能暴露出计划缺了一步。
所以 `advance` 之后加一道 replan —— 把「刚做完什么」和「还剩什么」交给模型，
由它决定要不要重建剩下的步骤。

两个入口
--------
- **步进后**（`advance → replan`）：上一步做完了，看看后面还需不需要原计划。
- **修复用尽后**（`verify` 失败 / `review` 阻断 → replan）：这一步反复修都没做成，
  先别放弃 —— 换一种做法也许能成。这是重规划最该发挥作用的地方：`repair` 修不动
  的东西，有时换个拆法就能做。

  注意这里认**两种**阻断（见 `blocking_failure`）：验证失败与代码审查阻断。只认
  验证会让审查阻断落进步进入口，而那个入口允许返回"不用改" —— 路由于是把控制流
  送回 `act` 重试一个已经确定不合格的步骤，空转到额度耗尽。

三条硬约束
----------
1. **次数上限**（`max_replans`），且**两个入口各算各的**。没有上限的「边做边改」
   没有收敛点，而且每次重规划都要多一次模型调用。到达上限后不再问模型。

   分开计数的原因是踩过的坑：两者曾经共用 `replan_count`，于是「前两步各做了
   一次无害的微调」就会把额度用光，等到第 3 步真做不成时，失败入口因为额度耗尽
   而**不再问模型，直接把剩余步骤全部砍掉** —— 第 4、5 步与那次失败毫无关系，
   却被一起放弃。共用一个额度，等于让锦上添花的微调抢走救命稻草的机会。
2. **只能改剩下的步骤**。已完成的部分是既成事实：删掉它们会让 `step_idx`
   与计划的对应关系断掉。因此新计划 = `plan[:step_idx] + 修订后的剩余步骤`。
   注意 `step_idx` 指的是**当前这一步**（步进入口下是"下一步"，失败入口下是
   "没做成的那一步"），所以两种入口用的是同一个切分点。
3. **失败入口必须给出一个改过的 plan**（哪怕是砍掉剩余步骤）。返回"无变化"会被
   路由送回 `act` 重试一个已经确定做不成的步骤 —— 那是死循环。步进入口则可以
   沿用原计划：计划不完美也比任务中断强。

本节点**不写消息历史**：诊断信息走 state，和 repair 的做法一致。往对话里塞
一条"助手口吻"的计划消息会污染后续每一轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
from coding_agent.llm.prompts import REPLAN_AFTER_FAILURE_PROMPT, REPLAN_PROMPT
from coding_agent.messages import last_human_text, text_of


class ReplanDecision(BaseModel):
    """重规划决定。"""

    revise: bool = Field(description="剩下的步骤是否需要调整")
    steps: list[str] = Field(
        default_factory=list,
        description="调整之后的剩余步骤（revise 为真时给出；剩余工作已做完就给空列表）",
    )


def _plan_view(plan: list[str], step_idx: int, *, failed: bool) -> str:
    """把计划画给模型看。

    `step_idx` 指向的是**当前要处理的那一步**：从 advance 进来时它是下一步，
    从「修复用尽」进来时它是那步没做成的。两种情形的标注不同，混淆了会让模型
    以为某步已经做完。
    """
    lines = []
    for index, step in enumerate(plan):
        if index < step_idx:
            mark = "（已完成）"
        elif index == step_idx:
            mark = "（没做成）" if failed else "（下一步）"
        else:
            mark = "（待办）"
        lines.append(f"{index + 1}. {step}{mark}")
    return "\n".join(lines)


def blocking_failure(state: AgentState) -> tuple[str, dict] | None:
    """当前这一步有没有被质量关拦下；返回 (来源, 详情)。

    **两种阻断必须一起认**：`verify` 失败（行为不对）与 `review` 阻断（代码不干净）。
    只认前者会让 review 阻断落进"步进微调"入口 —— 那个入口允许返回"不用改"，
    路由于是把控制流送回 act 重试一个已经确定不合格的步骤，空转到额度耗尽。

    判定口径与 `routing.is_blocked` 同源，两处都从这里出发。
    """
    verification = state.get("verification") or {}
    if verification.get("status") == "failed":
        return "verify", verification
    review = state.get("review") or {}
    if review.get("status") == "blocked":
        return "review", review
    return None


def _failure_note(failure: dict, attempts: int) -> str:
    """把「试了几次、怎么失败的」整理成给模型看的可执行信息。

    要同时认两种形状：验证结果的 `issues` 与审查结果的 `findings`
    （`location` + `message` 的字段名一致，所以只差一层列表名）。
    """
    lines = [f"已尝试修复 {attempts} 次，仍未通过。"]
    summary = failure.get("summary", "")
    if summary:
        lines.append(f"结果：{summary}")
    issues = list(failure.get("issues") or []) + list(failure.get("findings") or [])
    for issue in issues[:5]:
        where = issue.get("location", "")
        message = issue.get("message", "")
        lines.append(f"- {where} {message}".rstrip())
    if not issues and failure.get("output_tail"):
        lines.append(f"原始输出（末尾）：\n{failure['output_tail'][-800:]}")
    return "\n".join(lines)


def _last_summary(messages: list[AnyMessage]) -> str:
    """刚做完那步的小结：最后一条不带 tool_calls 的助手消息。

    只给小结而不是整段历史 —— replan 只需要知道"实际发生了什么"，
    把工具结果全塞进来会让每次步进都多付一大笔上下文。
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return text_of(message).strip()
    return ""


def make_replan_node(
    llm: BaseChatModel,
    *,
    max_replans: int,
    max_plan_steps: int,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, ReplanDecision).with_retry(stop_after_attempt=2)

    async def replan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        plan = [step for step in (state.get("plan") or []) if step]
        step_idx = state.get("step_idx", 0)

        # 两种入口：步进后（上一步做完了）与修复用尽后（这一步没做成）。
        # **审查阻断也算"没做成"** —— 只认 verification 会让它落进步进微调入口，
        # 那条路允许返回"不改"，路由就会回到 act 空转（见 blocking_failure）。
        blocked = blocking_failure(state)
        failed = blocked is not None
        failure = blocked[1] if blocked else {}
        failure_source = blocked[0] if blocked else ""

        # 两个入口各用各的额度，互不挤占（见模块文档第 1 条）
        counter = "replan_count" if failed else "tweak_count"
        used = state.get(counter, 0)
        remaining = plan[step_idx:]

        if not remaining:
            return {}

        # 从「修复用尽」进来时，额度用尽就等于没辙了 —— 砍掉剩余步骤，
        # 让任务如实收尾。**必须返回一个改过的 plan**：返回 {} 会被路由送回 act，
        # 而这一步已经确定做不成，那就成了死循环。
        if failed and used >= max_replans:
            return {"plan": plan[:step_idx], counter: used}

        # 步进入口下额度用尽 → 直接放行，不多花一次模型调用
        if used >= max_replans:
            return {}

        if failed:
            prompt = REPLAN_AFTER_FAILURE_PROMPT.format(
                max_steps=max(max_plan_steps - step_idx, 1)
            )
            context = _failure_note(failure, state.get("retry", 0))
        else:
            prompt = REPLAN_PROMPT.format(max_steps=max(max_plan_steps - step_idx, 1))
            context = _last_summary(state["messages"])

        request = last_human_text(state["messages"])
        if not failed:
            head = "刚做完这步"
        elif failure_source == "review":
            head = "这一步的改动没通过代码审查"
        else:
            head = "没能做成这一步"
        body = (
            f"用户请求：{request}\n\n"
            f"计划：\n{_plan_view(plan, step_idx, failed=failed)}\n\n"
            f"{head}：\n{context}"
        )

        try:
            decision = await structured.ainvoke(
                [SystemMessage(content=prompt), HumanMessage(content=body)], config
            )
        except Exception:  # noqa: BLE001 - 重规划失败不能拖垮整张图
            # 步进入口沿用原计划；失败入口没有退路，只能收尾
            return {"plan": plan[:step_idx], counter: used} if failed else {}

        if not decision.revise and not failed:
            # 步进入口说"不用改"就是真不改 —— 返回空更新，连事件都不该发。
            # （失败入口不能这么返回，见上文：那会被路由送回 act 变成死循环。）
            return {}

        revised = [step.strip() for step in (decision.steps or []) if step and step.strip()]
        budget = max(max_plan_steps - step_idx, 0)
        new_plan = plan[:step_idx] + revised[:budget]

        update: dict[str, Any] = {"plan": new_plan, counter: used + 1}
        if failed and revised:
            # 换了新做法，就该有新的修复预算与轮次 —— 否则 act 一看轮次用尽
            # 就直接放弃，新做法根本没机会被执行。
            # 两道质量关的旧结论一并作废：它们是针对**上一个做法**的。
            update.update(
                retry=0,
                tool_rounds=0,
                budget_exhausted=False,
                verification={},
                review={},
            )
        return update

    return replan
