"""Fan-in：把并发 worker 的结果收敛成主 agent 可消费的上下文。

确定性排序/预算/截断是基线；`orchestrator_fanin_reducer` 打开时，只在
汇总接近预算或存在失败/冲突时调用一次 LLM 做可选压缩，失败回退基线。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState
from coding_agent.messages import text_of
from coding_agent.orchestrator.artifacts import ArtifactStore, make_run_id
from coding_agent.orchestrator.graph import mark_blocked
from coding_agent.sandbox.wsl_exec import WslSandbox

_FANIN_STATUS_ORDER = {
    "failed": 0,
    "blocked": 0,
    "cancelled": 1,
    "partial": 2,
    "success": 3,
}

REDUCER_PROMPT = """\
你是编排器的汇总压缩器。把下面的 worker 汇总压缩成适合主 agent 阅读的简短文本：
- 必须保留失败/阻塞/取消、合并状态、冲突文件与关键文件/符号位置；
- 去掉重复解释，按重要性排序；
- 只输出压缩后的正文，不要 JSON，不要额外说明。
目标长度：不超过 {limit} 字。
"""


def _fanin_rank(result: dict[str, Any]) -> tuple[int, int, int, str]:
    """失败/阻塞优先，其次 partial，最后 success；同级按优先级/深度。"""
    status = _FANIN_STATUS_ORDER.get(str(result.get("status", "")), 4)
    return (
        status,
        -int(result.get("priority") or 0),
        -int(result.get("depth") or 0),
        str(result.get("task_id", "")),
    )


def make_synthesize_node(
    settings: Settings,
    *,
    sandbox: WslSandbox | None = None,
    root: str = "",
    artifacts: bool = False,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def synthesize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        tasks = state.get("tasks") or []
        status = mark_blocked(tasks, state.get("task_status") or {})
        results = state.get("worker_results") or []

        # 同一任务可能重试多次：fan-in 只看最后一次尝试。
        latest: dict[str, dict[str, Any]] = {}
        for result in results:
            task_id = str(result.get("task_id", ""))
            previous = latest.get(task_id)
            if previous is None or int(result.get("attempt", 1)) >= int(
                previous.get("attempt", 1)
            ):
                latest[task_id] = result

        merged_count = sum(
            1
            for result in latest.values()
            if result.get("role") == "implement" and result.get("merged")
        )
        file_tasks: dict[str, list[str]] = {}
        for result in latest.values():
            for path in result.get("files") or []:
                file_tasks.setdefault(str(path), []).append(
                    str(result.get("task_id", "?"))
                )
        overlaps = {
            path: task_ids
            for path, task_ids in file_tasks.items()
            if len(set(task_ids)) > 1
        }
        unfinished = [
            task["id"]
            for task in tasks
            if status.get(task["id"], "pending")
            in {"pending", "blocked", "cancelled"}
        ]

        limit = max(int(settings.orchestrator_context_chars), 500)
        ordered = sorted(latest.values(), key=_fanin_rank)
        per_task = max(200, limit // max(len(ordered), 1))
        blocks: list[str] = []
        for result in ordered:
            task_id = result.get("task_id", "?")
            role = result.get("role", "")
            state_text = result.get("status", "")
            attempt = int(result.get("attempt", 1))
            retry_note = f"（第 {attempt} 次尝试）" if attempt > 1 else ""
            summary = str(result.get("summary", "")).strip()
            merge_note = ""
            if role == "implement":
                if result.get("merged"):
                    merge_note = "，已合并"
                elif result.get("merge_error"):
                    merge_note = f"，合并失败：{result['merge_error'][:200]}"
                elif result.get("patch_path"):
                    merge_note = "，补丁未合并"
            block_lines = [
                f"- {task_id} ({role}/{state_text}){retry_note}"
                f"{merge_note}: {summary}"
            ]
            files = result.get("files") or []
            if files:
                block_lines.append(
                    "  相关文件：" + "、".join(str(f) for f in files[:8])
                )
            findings = result.get("findings") or []
            if findings:
                block_lines.append(
                    "  发现：" + "；".join(str(f) for f in findings[:5])
                )
            block = "\n".join(block_lines)
            if len(block) > per_task:
                block = block[:per_task] + "…（任务摘要已截断）"
            blocks.append(block)

        head = [
            "[编排器汇总]"
            + (
                "\n注意：以下实现任务已由 worker 完成并合并到主工作区；"
                "优先验证与最小修复，不要重做已完成部分。"
                if merged_count
                else ""
            )
        ]
        if overlaps:
            head.append("多 worker 关注同一文件：")
            for path, task_ids in list(overlaps.items())[:10]:
                head.append(
                    f"- {path}（{', '.join(sorted(set(task_ids)))}）"
                )
        if unfinished:
            head.append("未执行/阻塞的任务：" + "、".join(unfinished))

        context = "\n".join([*head, *blocks])
        if len(context) > limit:
            context = context[:limit] + "\n…（汇总已截断）"

        artifact_items: list[dict[str, Any]] = []
        if artifacts and sandbox is not None and root:
            thread_id = str(
                (((config or {}).get("configurable")) or {}).get(
                    "thread_id", ""
                )
            )
            run_id = make_run_id(thread_id, time.strftime("%Y%m%dT%H%M%S"))
            store = ArtifactStore(sandbox, root, run_id)
            for result in ordered:
                rel = store.save_result(result)
                if rel:
                    artifact_items.append(
                        {
                            "task_id": str(result.get("task_id", "")),
                            "attempt": int(result.get("attempt", 1)),
                            "status": str(result.get("status", "")),
                            "artifact_id": rel,
                        }
                    )

        completed = sum(1 for value in status.values() if value == "success")
        partial = sum(1 for value in status.values() if value == "partial")
        cancelled_count = sum(
            1 for value in status.values() if value == "cancelled"
        )
        failed = sum(
            1 for value in status.values()
            if value in {"failed", "blocked", "cancelled"}
        )
        budget_note = ""
        limit = int(state.get("token_budget_limit") or 0)
        if limit > 0:
            spent = int(state.get("token_budget_spent") or 0)
            budget_note = f"，token {spent}/{limit}"
            if state.get("token_budget_exhausted"):
                budget_note += "（预算耗尽）"
        return {
            "task_status": status,
            "worker_context": context,
            "worker_brief": context,
            "orchestration_artifacts": artifact_items,
            "orchestrator_summary": (
                f"成功 {completed}，部分 {partial}，失败/阻塞 {failed}"
                f"（其中取消 {cancelled_count}），"
                f"已合并 {merged_count}，重叠文件 {len(overlaps)}"
                f"{budget_note}"
            ),
            "orchestrator_completed": completed,
            "orchestrator_failed": failed,
        }

    return synthesize


def make_fan_in_node(
    settings: Settings,
    *,
    llm: BaseChatModel | None = None,
    sandbox: WslSandbox | None = None,
    root: str = "",
    artifacts: bool = False,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    """异步 fan-in：确定性汇总 + 可选的一次 LLM 压缩。"""
    synthesize = make_synthesize_node(
        settings, sandbox=sandbox, root=root, artifacts=artifacts
    )

    async def fan_in(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        result = synthesize(state, config)
        context = str(result.get("worker_context") or "")
        limit = max(int(settings.orchestrator_context_chars), 500)
        should_reduce = bool(
            settings.orchestrator_fanin_reducer
            and llm is not None
            and (
                int(result.get("orchestrator_failed") or 0) > 0
                or len(context) >= max(limit - 200, 300)
            )
        )
        if not should_reduce:
            return result

        target = max(200, limit // 2)
        try:
            response = await llm.ainvoke(
                [
                    SystemMessage(
                        content=REDUCER_PROMPT.format(limit=target)
                    ),
                    HumanMessage(content=context),
                ],
                config,
            )
            reduced = text_of(response).strip()
        except Exception:  # noqa: BLE001 - 压缩失败必须回退基线
            return result
        if not reduced:
            return result
        if len(reduced) > limit:
            reduced = reduced[:limit] + "\n…（汇总已截断）"
        updated = dict(result)
        updated["worker_context"] = reduced
        updated["worker_brief"] = reduced
        updated["orchestrator_summary"] = (
            str(result.get("orchestrator_summary", "")) + "，已压缩"
        )
        return updated

    return fan_in
