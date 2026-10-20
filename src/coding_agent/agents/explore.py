"""只读 Explore 子智能体（Phase 1）。

主智能体通过 `delegate_explore` 把一次探索委派出去。子智能体有**独立的消息历史**：
入口只有系统提示 + 本次任务，出口只有结构化报告。它的工具输出不会进入主 agent
的对话历史 —— 这正是「子智能体」与「换一段提示词」的区别。

为什么不是 LangGraph 子图
-------------------------

Phase 1 只需要一个受控的「模型 ⇄ 只读工具」循环。把它做成独立子图会引入嵌套
checkpoint、interrupt 恢复与并行分支语义，而这些在 Phase 2 才真正需要。现在用
宿主内的 async 循环，边界清楚、单测简单；Phase 2 可以把它原样包成 worker。

安全边界
--------

- 只绑定 `EXPLORE_TOOL_NAMES`（file_read / search_code / find_files）；
- 宿主在构造时断言工具集合是白名单的子集，运行时不接受白名单外的工具名；
- 工具执行走 `asyncio.to_thread`，因为现有沙箱/工具是同步的；Phase 2 前应改成
  async subprocess，否则同步工具仍然无法被取消。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.llm.deepseek import with_json_output
from coding_agent.llm.prompts import (
    EXPLORE_PROMPT,
    EXPLORE_REPORT_PROMPT,
    REVIEW_PROMPT,
    REVIEW_REPORT_PROMPT,
)
from coding_agent.messages import text_of
from coding_agent.tools.artifacts import SubAgentArtifact, unpack
from coding_agent.tools.delegate import EXPLORE_TOOL_NAMES

# 传给报告模型的对话转写上限。工具输出本身已被 max_output_chars 截断，
# 这里是兜底：6 轮 × 20k 仍然可能超过上下文预算。
_MAX_TRANSCRIPT_CHARS = 60_000
_MAX_ITEM_CHARS = 600
_MAX_REPORT_ITEMS = 20


class TaskProposal(BaseModel):
    """worker 建议追加的只读任务。

    写任务不能由 worker 提议：implement 需要明确的 write_scope，而自由文本
    推断出的范围不安全。`extend_tasks` 会再次校验角色、依赖、重复与总量。
    """

    role: Literal["explore", "test", "review"] = "explore"
    goal: str = Field(description="要回答/检查什么")
    inputs: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ExploreReport(BaseModel):
    """子智能体返回给主智能体的结构化探索报告。"""

    summary: str = Field(description="一段话回答探索目标")
    relevant_files: list[str] = Field(default_factory=list, description="相关文件路径")
    findings: list[str] = Field(default_factory=list, description="关键发现")
    open_questions: list[str] = Field(default_factory=list, description="尚未确认的问题")
    recommended_next_steps: list[str] = Field(
        default_factory=list, description="建议主智能体接下来做什么"
    )
    # 只有确实出现「需要新的只读调查才能继续」时才填；宿主会做上限与去重。
    proposed_tasks: list[TaskProposal] = Field(
        default_factory=list, description="建议追加的只读任务（最多 2 个）"
    )


DelegateRunner = Callable[..., Awaitable[SubAgentArtifact]]


def _usage(message: Any) -> tuple[int, int]:
    """从模型消息里取 token 用量；provider 没给就是 0。"""
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        int(meta.get("input_tokens") or 0),
        int(meta.get("output_tokens") or 0),
    )


def _child_config(config: RunnableConfig | None, call_id: str) -> dict[str, Any]:
    """给子智能体的 LLM/工具调用挂上可区分的 trace 信息。

    刻意**不改** `configurable.thread_id`：子智能体不是独立会话，它只是父 run
    内部的一次委派。tracing 侧靠 run_name / tags / agent_id 区分。
    """
    base = dict(config or {})
    metadata = dict(base.get("metadata") or {})
    metadata.update({"agent_id": call_id, "role": "explore"})
    tags = [*(base.get("tags") or []), "subagent", "explore"]
    return {
        **base,
        "run_name": f"explore:{call_id}",
        "tags": tags,
        "metadata": metadata,
    }


def _task_text(parent_request: str, goal: str, paths: list[str]) -> str:
    path_text = "\n".join(f"- {path}" for path in paths) if paths else "（未指定，自行定位）"
    return (
        f"用户请求：{parent_request or '（未提供）'}\n\n"
        f"探索目标：{goal or '（未提供）'}\n\n"
        f"优先查看的路径：\n{path_text}"
    )


def _transcript(messages: list[Any], max_chars: int) -> str:
    """把子对话压成纯文本，供报告模型使用。

    不直接把带 tool_calls 的消息丢给 json_mode 的结构化调用：那条路径对消息形状
    更敏感，而报告只需要「发生了什么」的文本。
    """
    lines: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            content = text_of(message).strip()
            if content:
                lines.append(f"助手：{content[:_MAX_ITEM_CHARS]}")
            calls = getattr(message, "tool_calls", None) or []
            if calls:
                names = ", ".join(str(call.get("name", "")) for call in calls)
                lines.append(f"（调用工具：{names}）")
        elif isinstance(message, ToolMessage):
            name = str(getattr(message, "name", "") or "")
            content = text_of(message).strip()
            if content:
                lines.append(f"工具[{name}]：{content[:_MAX_ITEM_CHARS]}")
    transcript = "\n".join(lines)
    if len(transcript) <= max_chars:
        return transcript
    return "…（前文已截断）\n" + transcript[-max_chars:]


def _parse_report_text(text: str) -> ExploreReport | None:
    """从收尾消息里解析结构化报告；失败返回 None，走第二次报告调用。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
        return ExploreReport.model_validate(payload)
    except (ValueError, TypeError):
        return None


def _fallback_summary(messages: list[Any]) -> str:
    """报告调用失败时，用最后一条助手/工具文本兜底。"""
    for message in reversed(messages):
        if isinstance(message, (AIMessage, ToolMessage)):
            content = text_of(message).strip()
            if content:
                return content
    return "探索未完成。"


def _clean_items(items: list[str] | None, limit: int = _MAX_REPORT_ITEMS) -> list[str]:
    """去空、去重、限长、限量。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:_MAX_ITEM_CHARS])
        if len(out) >= limit:
            break
    return out


def render_subagent_result(artifact: SubAgentArtifact, *, limit: int = 6_000) -> str:
    """把结构化结果压成主模型可读的紧凑文本。"""
    lines = [
        f"[探索子智能体] 目标：{artifact.goal}",
        f"状态：{artifact.status}",
        f"结论：{artifact.summary}",
    ]
    if artifact.relevant_files:
        lines.append("相关文件：")
        lines.extend(f"- {path}" for path in artifact.relevant_files[:20])
    if artifact.findings:
        lines.append("发现：")
        lines.extend(f"- {item}" for item in artifact.findings[:10])
    if artifact.open_questions:
        lines.append("未决问题：")
        lines.extend(f"- {item}" for item in artifact.open_questions[:5])
    if artifact.recommended_next_steps:
        lines.append("建议下一步：")
        lines.extend(f"- {item}" for item in artifact.recommended_next_steps[:5])
    if artifact.error:
        lines.append(f"错误：{artifact.error}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def make_readonly_runner(
    *,
    llm: BaseChatModel,
    tools: list[BaseTool],
    settings: Settings,
    role: str = "explore",
) -> DelegateRunner:
    """构造一个只读 worker runner（explore / review）。

    两种角色共用同一套只读工具循环与报告 schema，只换系统提示与报告提示。
    参数在**建图时**校验一次：工具集合必须是只读白名单的子集。运行时再按名字
    二次校验，防止模型伪造一个白名单外的工具名。
    """
    if role not in {"explore", "review"}:
        raise ValueError(f"不支持的只读 worker 角色：{role}")
    system_prompt = EXPLORE_PROMPT if role == "explore" else REVIEW_PROMPT
    report_prompt = (
        EXPLORE_REPORT_PROMPT if role == "explore" else REVIEW_REPORT_PROMPT
    )
    tool_map = {tool.name: tool for tool in tools}
    unexpected = set(tool_map) - EXPLORE_TOOL_NAMES
    if unexpected:
        raise ValueError(
            f"子智能体工具超出只读白名单：{sorted(unexpected)}；"
            f"允许：{sorted(EXPLORE_TOOL_NAMES)}"
        )

    child_llm = llm.bind_tools(list(tool_map.values()))
    # include_raw=True：结构化调用也能拿到 AIMessage，从而累计这次报告的 token。
    report_llm = with_json_output(llm, ExploreReport, include_raw=True).with_retry(
        stop_after_attempt=2
    )
    max_rounds = max(int(settings.explore_max_rounds), 1)
    timeout = max(int(settings.explore_timeout), 1)

    async def run(
        *,
        args: dict[str, Any],
        call_id: str,
        parent_request: str,
        cwd: str = "",
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> SubAgentArtifact:
        started = time.monotonic()
        agent_id = f"{role}:{call_id}"
        goal = str(args.get("goal", "")).strip()
        paths = [str(path).strip() for path in (args.get("paths") or []) if str(path).strip()]
        child_config = _child_config(config, call_id)
        # 任务级预算优先于全局默认；None 继承全局。轮次/超时至少为 1，
        # token 预算 0 表示不限制。
        raw_rounds = args.get("max_rounds")
        raw_timeout = args.get("timeout")
        raw_tokens = args.get("max_tokens")
        task_rounds = max(
            int(raw_rounds if raw_rounds is not None else max_rounds), 1
        )
        task_timeout = max(
            int(raw_timeout if raw_timeout is not None else timeout), 1
        )
        task_tokens = max(
            int(
                raw_tokens
                if raw_tokens is not None
                else settings.orchestrator_task_tokens or 0
            ),
            0,
        )

        repo_map = str(args.get("repo_map") or "").strip()
        task_text = _task_text(parent_request, goal, paths)
        if repo_map:
            task_text += (
                "\n\n仓库结构摘要（AST 生成，供定位；不是完整源码）：\n"
                f"{repo_map}"
            )

        system_text = system_prompt.format(
            cwd=cwd or "（未指定）", max_rounds=task_rounds
        )
        # 编排器按任务目标筛过的项目约定；与主 agent 的注入同源
        memories = [
            str(fact).strip()
            for fact in (args.get("memories") or [])
            if str(fact).strip()
        ]
        if memories:
            system_text += (
                "\n\n关于这个项目的长期记忆（可信，必须遵守）：\n"
                + "\n".join(f"- {fact}" for fact in memories)
            )
        instructions = str(args.get("instructions") or "").strip()
        if instructions:
            system_text += f"\n\n{instructions}"
        messages: list[Any] = [
            SystemMessage(content=system_text),
            HumanMessage(content=task_text),
        ]
        records: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        llm_ms = 0
        tool_ms = 0
        rounds = 0
        error = ""
        truncated = False
        responded = False
        cancelled = False
        budget_exhausted = False
        timed_out = False
        final_text = ""

        async def _loop() -> None:
            nonlocal input_tokens, output_tokens, rounds, truncated, responded
            nonlocal cancelled, budget_exhausted, final_text
            nonlocal llm_ms, tool_ms
            for round_index in range(1, task_rounds + 1):
                if cancel is not None and cancel():
                    cancelled = True
                    return
                rounds = round_index
                llm_started = time.monotonic()
                response = await child_llm.ainvoke(messages, child_config)
                llm_ms += int((time.monotonic() - llm_started) * 1000)
                responded = True
                used_in, used_out = _usage(response)
                input_tokens += used_in
                output_tokens += used_out
                messages.append(response)
                if (
                    task_tokens > 0
                    and input_tokens + output_tokens >= task_tokens
                ):
                    budget_exhausted = True
                    truncated = True
                    final_text = text_of(response).strip()
                    return

                calls = getattr(response, "tool_calls", None) or []
                if not calls:
                    # 收尾消息若已经是约定 JSON，宿主直接采用，省掉第二次
                    # 报告 LLM 调用；解析失败才回退。
                    final_text = text_of(response).strip()
                    return

                for index, call in enumerate(calls):
                    if cancel is not None and cancel():
                        cancelled = True
                        return
                    name = str(call.get("name", ""))
                    tool_call_id = str(call.get("id", "")) or f"r{round_index}-{index}"
                    call_args = dict(call.get("args") or {})
                    tool = tool_map.get(name)
                    ok = False
                    duration_ms = 0
                    if tool is None:
                        content = (
                            f"错误：子智能体没有 {name} 工具。"
                            f"可用工具：{sorted(tool_map)}"
                        )
                    else:
                        tool_started = time.monotonic()
                        try:
                            raw = await asyncio.to_thread(
                                tool.invoke, call_args, child_config
                            )
                            content, artifact = unpack(raw)
                            ok = (
                                bool(artifact.get("ok", True))
                                if isinstance(artifact, dict)
                                else True
                            )
                        except Exception as exc:  # noqa: BLE001 - 子工具失败回灌给子模型
                            content = f"工具执行异常：{type(exc).__name__}: {exc}"
                        duration_ms = int((time.monotonic() - tool_started) * 1000)
                        tool_ms += duration_ms
                    records.append(
                        {
                            "call_id": tool_call_id,
                            "name": name,
                            "args": call_args,
                            "ok": ok,
                            "duration_ms": duration_ms,
                            "round": round_index,
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=tool_call_id,
                            name=name,
                        )
                    )
            # 跑满轮次时仍有工具调用：强制收尾，报告可能不完整
            truncated = True

        try:
            await asyncio.wait_for(_loop(), timeout=task_timeout)
        except TimeoutError:
            timed_out = True
            error = f"探索超时（>{task_timeout}s）"
        except Exception as exc:  # noqa: BLE001 - 子智能体失败不能拖垮主图
            error = f"{type(exc).__name__}: {exc}"

        report: ExploreReport | None = None
        if responded and not cancelled:
            report = _parse_report_text(final_text)
        if report is None and responded and not cancelled and not budget_exhausted:
            transcript = _transcript(messages, _MAX_TRANSCRIPT_CHARS)
            # 报告调用也受同一个墙钟预算约束：循环若已用满，只给它剩余时间。
            remaining = max(task_timeout - (time.monotonic() - started), 1.0)
            try:
                report_started = time.monotonic()
                payload = await asyncio.wait_for(
                    report_llm.ainvoke(
                        [
                            SystemMessage(content=report_prompt),
                            HumanMessage(
                                content=transcript or "（没有可用的探索记录）"
                            ),
                        ],
                        child_config,
                    ),
                    timeout=remaining,
                )
                llm_ms += int((time.monotonic() - report_started) * 1000)
                if isinstance(payload, dict):
                    raw = payload.get("raw")
                    used_in, used_out = _usage(raw)
                    input_tokens += used_in
                    output_tokens += used_out
                    parsed = payload.get("parsed")
                    report = parsed if isinstance(parsed, ExploreReport) else None
                elif isinstance(payload, ExploreReport):
                    report = payload
            except Exception as exc:  # noqa: BLE001 - 报告失败用摘要兜底
                error = error or f"报告生成失败：{type(exc).__name__}: {exc}"

        had_tool_failure = any(not record["ok"] for record in records)
        proposed_tasks: list[dict[str, Any]] = []
        if cancelled:
            status = "cancelled"
            summary = "已按用户要求取消。"
            relevant_files = []
            findings = []
            open_questions = []
            next_steps = []
        elif report is not None:
            status = (
                "partial" if (error or truncated or had_tool_failure) else "success"
            )
            summary = report.summary
            relevant_files = report.relevant_files
            findings = report.findings
            open_questions = report.open_questions
            next_steps = report.recommended_next_steps
            # 每个 worker 最多带 2 个提议；更大的量由 extend_tasks 再按总量截断。
            proposed_tasks = [
                item.model_dump() for item in report.proposed_tasks[:2]
            ]
        else:
            status = "partial" if responded else "failed"
            summary = _fallback_summary(messages) if responded else "子智能体未能启动。"
            relevant_files = []
            findings = []
            open_questions = []
            next_steps = []

        summary = summary.strip() or "探索未完成。"
        if len(summary) > settings.explore_max_report_chars:
            summary = summary[: settings.explore_max_report_chars] + "…"

        return SubAgentArtifact(
            agent_id=agent_id,
            parent_call_id=call_id,
            role=role,
            goal=goal,
            status=status,
            summary=summary,
            relevant_files=_clean_items(relevant_files),
            findings=_clean_items(findings),
            open_questions=_clean_items(open_questions, limit=10),
            recommended_next_steps=_clean_items(next_steps, limit=10),
            proposed_tasks=proposed_tasks,
            rounds=rounds,
            tool_calls=records,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            llm_ms=llm_ms,
            tool_ms=tool_ms,
            timed_out=timed_out,
            error=error,
        )

    return run


def make_explore_runner(
    *, llm: BaseChatModel, tools: list[BaseTool], settings: Settings
) -> DelegateRunner:
    """向后兼容：Explore 角色。"""
    return make_readonly_runner(
        llm=llm, tools=tools, settings=settings, role="explore"
    )


def make_review_runner(
    *, llm: BaseChatModel, tools: list[BaseTool], settings: Settings
) -> DelegateRunner:
    """Review 角色：同一套只读工具循环，换审查提示与报告提示。"""
    return make_readonly_runner(
        llm=llm, tools=tools, settings=settings, role="review"
    )
