"""verify 节点：一步做完后跑一遍项目的测试/构建，把结果结构化。

两个刻意的取舍：

1. **只在改过东西时才验证**（`dirty`）。只读探索的步骤跑测试纯属浪费 ——
   一次 pytest 可能几十秒。
2. **自动验证不走审批**，因此它只能跑**命令文本完全由宿主确定**的命令。
   判据是「谁决定执行什么」：`cargo test` / `go test` / `pytest` 跑什么由宿主
   写死；而 `make test` 跑什么写在 Makefile 里、`npm test` 写在 package.json 里，
   这两个文件模型写得到（`--write` 下是自动放行的 L1）—— 那样就出现了一条
   「写个恶意 Makefile → 下一次脏写自动执行」的无审批执行路径，绕过命令分级。
   所以这里传 `allow_manifest=False`；模型主动调 `run_tests` 才走审批，
   那条路径才允许 manifest 派生的命令。

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
    root: str | None = None,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """sandbox 是执行侧的配置源（超时、verify_enabled、verify_command 都在它的 settings 里）。

    `root` 由调用方显式给：verify 与工具共用同一个沙箱实例，而决定工作区的是
    **本次运行的 settings**（可能带 `--workspace` 覆盖），不是沙箱自己那份。
    不传则由沙箱的 settings 推 —— 那会退回 $HOME，与工具的工作区不是同一个。
    """
    root = root or resolve_workspace(sandbox.settings, sandbox)

    def verify(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        if not sandbox.settings.verify_enabled or not state.get("dirty"):
            return {"verification": {}}

        result = run_verification(sandbox, root, allow_manifest=False)
        return {"verification": result.model_dump()}

    return verify
