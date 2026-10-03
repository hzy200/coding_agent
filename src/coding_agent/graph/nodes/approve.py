"""approval_gate 节点：在执行工具之前决定「放行 / 询问 / 拒绝」。

它是唯一会产生 `interrupt()` 的地方 —— 图在这里挂起，把待批请求交给前端，
用户答复后由 `Command(resume=...)` 带着结果重新进入本节点。

注意：**节点被中断后恢复时会从头重跑**，所以这里除了 `interrupt()` 之外
必须保持纯函数（同样的 state 得到同样的结论），否则审批结果会对不上号。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from coding_agent.graph.state import AgentState
from coding_agent.sandbox.policy import APPROVED, ASK, DENIED, SessionPolicy
from coding_agent.sandbox.policy import CommandLevel as _Level
from coding_agent.sandbox.policy import classify as _classify
from coding_agent.tools.deps import DEPS_INSTALL, DEPS_LIST
from coding_agent.tools.files import (
    EDIT_TOOL_NAME,
    READ_TOOL_NAME,
    RESTORE_TOOL_NAME,
    WRITE_TOOL_NAME,
)
from coding_agent.tools.git import GIT_ADD, GIT_COMMIT, GIT_DIFF, GIT_LOG, GIT_STATUS
from coding_agent.tools.search import FIND_FILES, SEARCH_CODE
from coding_agent.tools.shell import SHELL_TOOL_NAME
from coding_agent.tools.testrun import RUN_TESTS_TOOL

# 只读工具：无条件放行
_READ_TOOLS = frozenset(
    {READ_TOOL_NAME, GIT_STATUS, GIT_DIFF, GIT_LOG, DEPS_LIST, SEARCH_CODE, FIND_FILES}
)

# 低风险写：由 --write 一次性授权，不逐条询问。
# 文件工具是受控路径（有 diff、有备份）；git add 只动索引，可 restore 撤销。
_WRITE_TOOLS = frozenset({WRITE_TOOL_NAME, EDIT_TOOL_NAME, RESTORE_TOOL_NAME, GIT_ADD})

# 变更性：产生提交、改动依赖环境、执行项目代码，一律逐条询问。
# 注意自动 verify 不走这里 —— 它跑的是宿主推导出的固定命令，模型影响不了跑什么。
_MUTATE_TOOLS = frozenset({GIT_COMMIT, DEPS_INSTALL, RUN_TESTS_TOOL})


def tool_level(name: str, args: dict[str, Any]) -> _Level:
    """各工具对应的风险等级；新工具必须在这里登记，否则 fail-closed 按最危险处理。"""
    if name == SHELL_TOOL_NAME:
        return _classify(str(args.get("command", ""))).level
    if name in _READ_TOOLS:
        return _Level.READ
    if name in _WRITE_TOOLS:
        return _Level.LOW_WRITE
    if name in _MUTATE_TOOLS:
        return _Level.MUTATE
    return _Level.DANGER  # 未登记的工具按最危险处理


def _describe(name: str, args: dict[str, Any]) -> str:
    """给审批弹窗/提示用的一行摘要。"""
    if name == SHELL_TOOL_NAME:
        return str(args.get("command", ""))
    if name == GIT_COMMIT:
        return f"git commit -m {args.get('message', '')!r}"
    if name == DEPS_INSTALL:
        return f"安装依赖：{', '.join(args.get('packages') or [])}"
    if name == GIT_ADD:
        return f"git add {' '.join(args.get('paths') or [])}"
    if name == RUN_TESTS_TOOL:
        return "运行本项目的测试"
    return str(args.get("path") or args.get("command") or name)


def make_approval_gate_node(
    policy: SessionPolicy,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def approval_gate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []

        approvals: dict[str, str] = {}
        pending: list[dict[str, Any]] = []

        for call in calls:
            name = str(call.get("name", ""))
            args = dict(call.get("args") or {})
            level = tool_level(name, args)
            decision = policy.decide(level)

            if decision == ASK:
                pending.append(
                    {
                        "call_id": str(call.get("id", "")),
                        "tool": name,
                        "command": _describe(name, args),
                        "reason": str(args.get("reason", "")),
                        "level": level.label,
                        "level_int": int(level),
                    }
                )
            else:
                approvals[str(call.get("id", ""))] = decision

        if not pending:
            return {"approvals": approvals}

        answers = interrupt({"requests": pending})
        if not isinstance(answers, dict):
            # 前端给了无法理解的答复 → 一律拒绝（fail closed）
            answers = {}

        for request in pending:
            granted = bool(answers.get(request["call_id"]))
            approvals[request["call_id"]] = APPROVED if granted else DENIED

        return {"approvals": approvals}

    return approval_gate
