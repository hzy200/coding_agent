"""planner 节点：把用户请求拆成有序子任务。

结构化输出走 pydantic 校验 + 重试；即使模型反复给不出合法结果，也降级为
「整个请求当成一步」，保证图不会因为规划失败而中断。

**降级必须可见。** 早先的实现把降级伪装成一次正常规划，于是多步工作流悄悄
退化成单步，而 `PlanCreated` 看上去完全正常 —— 只有翻审计才发现每次的计划
都是请求原文（见 llm/deepseek.py 里关于 json_mode 的注释）。
因此降级时写 `plan_degraded=True`，由事件层与前端明确告知。

与 act / respond 一致，这里是 **async 节点**（`ainvoke`），理由同上：
同步调用在异步图里会被丢进线程池，取消信号传不进去。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from coding_agent.graph.plan import UNKNOWN, PlanStepModel, align_kinds
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
from coding_agent.llm.prompts import (
    PLANNER_DELEGATION_NOTE,
    PLANNER_PERMISSION_READONLY,
    PLANNER_PERMISSION_WRITE,
    PLANNER_PROMPT,
)
from coding_agent.messages import last_human_text
from coding_agent.recovery import retry_async


class TaskPlan(BaseModel):
    """规划结果。"""

    steps: list[PlanStepModel] = Field(
        default_factory=list,
        description="有序子任务列表，每项含 text 与 kind",
    )


def make_planner_node(
    llm: BaseChatModel,
    *,
    max_steps: int,
    allow_write: bool,
    delegate_enabled: bool = False,
    retry_attempts: int = 3,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 30.0,
    retry_max_total_wait: float = 60.0,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, TaskPlan).with_retry(stop_after_attempt=3)
    permission_note = (
        PLANNER_PERMISSION_WRITE if allow_write else PLANNER_PERMISSION_READONLY
    )

    async def plan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        request = last_human_text(state["messages"])
        worker_context = str(state.get("worker_context") or "").strip()
        if worker_context:
            request = f"{request}\n\n{worker_context}"
        degraded = False
        retry_events: list[dict[str, Any]] = []
        # 委派提示依赖本次运行的仓库规模策略，所以按调用时组装。
        delegation_note = (
            PLANNER_DELEGATION_NOTE
            if delegate_enabled and state.get("delegation_allowed", True)
            else ""
        )
        prompt = PLANNER_PROMPT.format(
            max_steps=max_steps,
            permission_note=permission_note,
            delegation_note=delegation_note,
        )
        recovery_notice = str(state.get("recovery_notice") or "").strip()
        if recovery_notice:
            prompt += f"\n\n上次运行中断的恢复提示：\n{recovery_notice}"
        try:
            result = await retry_async(
                lambda: structured.ainvoke(
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(content=request),
                    ],
                    config,
                ),
                attempts=retry_attempts,
                base_delay=retry_base_delay,
                max_delay=retry_max_delay,
                max_total_wait=retry_max_total_wait,
                on_retry=retry_events.append,
                label="planner",
            )
            parsed = [
                (step.text.strip(), step.kind)
                for step in (result.steps or [])
                if step.text and step.text.strip()
            ][:max_steps]
            steps = [text for text, _kind in parsed]
            kinds = [kind for _text, kind in parsed]
        except Exception:  # noqa: BLE001 - 规划失败不能拖垮整张图
            steps = []
            kinds = []
            degraded = True

        if not steps:
            steps = [request or "完成用户请求"]
            # 降级计划不知道任务类型：用 unknown 保留原有的宽松行为，
            # 避免把只读问答误判成「必须改文件」。
            kinds = [UNKNOWN]
            # 模型给了合法 JSON 但 steps 为空，同样是「没规划出来」，
            # 与抛异常走的是同一条降级路径，也要如实标记。
            degraded = True

        return {
            "plan": steps,
            "plan_kinds": align_kinds(steps, kinds),
            "plan_degraded": degraded,
            "transient_retries": len(retry_events),
            "step_idx": 0,
            # 两个重规划额度都是整个任务的，开局归零
            "replan_count": 0,
            "tweak_count": 0,
            # 子智能体委派次数同样是任务级的（不是每步）
            "delegate_calls": 0,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            # run_dirty 是**整轮**的：orchestrator 合并写 worker 的补丁后会置 True，
            # planner 不能把它清掉（新一轮由 runtime.run 的 inputs 重置）。
            "empty_step_nudged": False,
            "verification": {},
            "review": {},
            # 水位线是**整轮**的：新的一轮开始，之前审过的留底都不算数了。
            # 只有这里归零，advance / repair 都不许碰它（详见 graph/state.py）。
            "review_watermark": "",
            "retry": 0,
            "same_failure_count": 0,
            "last_failure_signature": "",
            "last_verification": {},
        }

    return plan
