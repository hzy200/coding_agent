"""工具注册表。

`allow_write=False`（默认）时只有只读能力：shell 限 L0，文件工具只有 file_read。
`allow_write=True` 时放开 L1 只读命令与 file_write / file_edit。

文件修改一律走 file_* 工具，不走 shell —— 这样才有精确替换、diff 与备份。

后续按计划继续放开：
  W5 → 变更类命令经审批中断放行
  W6 → git / 依赖管理 / 测试工具
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from coding_agent.config import Settings, get_settings
from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.files import build_file_tools
from coding_agent.tools.shell import build_shell_tool


def build_tools(settings: Settings | None = None, *, allow_write: bool = False) -> list[BaseTool]:
    settings = settings or get_settings()
    sandbox = WslSandbox(settings)
    max_level = CommandLevel.LOW_WRITE if allow_write else CommandLevel.READ

    tools: list[BaseTool] = [build_shell_tool(settings, sandbox, max_level=max_level)]
    # 文件工具与 shell 共用同一个 sandbox 实例，工作区边界一致
    tools += build_file_tools(settings, sandbox, allow_write=allow_write)
    return tools
