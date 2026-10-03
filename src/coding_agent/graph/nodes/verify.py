"""verify 节点：一步做完后跑一遍项目的测试/构建，把结果结构化。

两个刻意的取舍：

1. **只在改过东西时才验证**（`dirty`）。只读探索的步骤跑测试纯属浪费 ——
   一次 pytest 可能几十秒。
2. **自动验证不走审批**。它跑的是宿主从项目清单推导出的固定命令，
   模型影响不了跑什么；模型主动调用 `run_tests` 才走审批。
   区别在于「谁决定执行什么」，而不在于命令本身。

验证失败**不会中断任务**：结果写进状态、事件与审计，由后续路由决定怎么办
（W10 会在这里接 repair 循环；当前是如实报告给 respond）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.testrun import run_verification


def make_verify_node(
    sandbox: WslSandbox,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """sandbox 是唯一的配置源（超时、verify_enabled、verify_command 都在它的 settings 里）。"""
    root = resolve_workspace(sandbox.settings, sandbox)

    def verify(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        if not sandbox.settings.verify_enabled or not state.get("dirty"):
            return {"verification": {}}

        result = run_verification(sandbox, root)
        return {"verification": result.model_dump()}

    return verify
