"""review 节点：一步做完、验证通过后，对本次改动做一遍代码审查。

为什么在 verify 之后
--------------------

`verify` 跑的是项目自己的测试，它回答的是「行为对不对」。但**测试通过 ≠ 代码正确**
（`docs/IMPROVEMENT_PLAN.md` P1-6）：调试残留、被改弱的断言、语法坏掉的分支、
往 `.agent/` 里写东西，这些都可以让测试全绿。所以质量需要第二道关 —— 这就是它。

与 verify 的关系是**串行**，不是替代：verify 失败先走 repair/replan，通过了才轮到
审查。两者共用同一份修复预算（`retry`），额度耗尽一律去 replan「换一种做法」。
共用而不是各给一份，是因为独立计数器会让 `estimate_recursion_limit` 多一层乘积 ——
而那个公式历史上已经算歪过三次（见 `graph/build.py`）。代价写在下面的取舍里。

取舍：共用预算意味着 review 会与 verify 争额度
----------------------------------------------

最坏情形是 verify 连失败 3 次吃光预算，随后通过、却又被 review 拦下 —— 这时本步
已经没有修复额度，直接进 replan。这不是漏网：`replan` 认识 review 的阻断
（见 `nodes/replan.py::_blocking_failure`），会带着审查发现去换做法，而它自己也有
硬性上限，所以环仍然收敛。反过来「review 占满额度后 verify 再失败」同理。

代价是明确的：**一个步骤里，验证与审查共享同一份修复机会**。要各给一份，
就得重推 `estimate_recursion_limit` 并同步它那条独立复算的测试。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.graph.state import AgentState
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.review import run_review


def make_review_node(
    sandbox: WslSandbox,
    root: str | None = None,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """root 的传法与 verify 完全一致：工作区由**本次运行的 settings** 决定，
    沙箱自己那份未必带 `--workspace` 覆盖，让它自己推会退回 $HOME。
    """
    root = root or resolve_workspace(sandbox.settings, sandbox)

    def review(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        # 没改动就没什么可审的。与 verify 的短路条件一致，但多一层开关：
        # 审查是"第二道关"，关掉它不影响主流程的正确性。
        if not sandbox.settings.review_enabled or not state.get("dirty"):
            return {"review": {}}

        result = run_review(
            sandbox,
            root,
            watermark=str(state.get("review_watermark") or ""),
        )
        return {"review": result.model_dump(), "review_watermark": result.watermark}

    return review
