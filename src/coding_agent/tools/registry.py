"""工具注册表。

`allow_write=False`（默认）时只有只读能力：shell 限 L0，文件工具只有 file_read。
`allow_write=True` 时放开 L1 与 file_write / file_edit。

文件修改一律走 file_* 工具，不走 shell —— 这样才有精确替换、diff 与备份。

**放行与否不在这里决定**，而是由 approval_gate + tools 节点依据 SessionPolicy
逐次判定；这里只决定「暴露哪些工具」。新工具记得在
`graph/nodes/approve.py::tool_level` 登记等级，否则按最危险的 L3 处理。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from coding_agent.config import Settings, get_settings
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.deps import build_deps_tools
from coding_agent.tools.files import build_file_tools
from coding_agent.tools.git import build_git_tools
from coding_agent.tools.search import build_search_tools
from coding_agent.tools.shell import build_shell_tool
from coding_agent.tools.testrun import build_test_tool


def build_tools(
    settings: Settings | None = None,
    *,
    allow_write: bool = False,
    sandbox: WslSandbox | None = None,
) -> list[BaseTool]:
    """构造本次会话的全部工具。

    注意：放行与否**不在这里决定**，而是由 approval_gate + tools 节点依据
    SessionPolicy 逐次判定。这里只决定「暴露哪些工具」。

    `sandbox` 由调用方传入以便与 verify、runtime 共用一份实例：环境探测的结果
    缓存在实例上，每多建一份就多探一遍。
    """
    settings = settings or get_settings()
    sandbox = sandbox or WslSandbox(settings)

    tools: list[BaseTool] = [build_shell_tool(settings, sandbox)]
    # 其余工具与 shell 共用同一个 sandbox 实例，工作区边界一致
    tools += build_file_tools(settings, sandbox, allow_write=allow_write)
    tools += build_git_tools(settings, sandbox)
    tools += build_deps_tools(settings, sandbox)
    tools += build_search_tools(settings, sandbox)
    tools.append(build_test_tool(settings, sandbox))
    return tools
