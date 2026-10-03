"""Shell 工具：把模型输出的"命令意图"交给 WSL2 沙箱执行。

职责边界：**执行 + 路径守卫 + 结果回报**。

这里**不做**安全分级判定 —— 放行与否由 `graph.nodes.approve`（approval_gate）
依据 `sandbox.policy.SessionPolicy` 决定，并在 `graph.nodes.tools` 强制执行。
把判定放在节点而不是工具里，是因为审批结果属于图状态（一次调用一个结果），
工具是无状态的；节点是所有工具执行的唯一咽喉，检查放在那里不会漏。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import SandboxPathError, ensure_inside
from coding_agent.sandbox.policy import classify
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, pack

SHELL_TOOL_NAME = "shell_exec"

SHELL_TOOL_DESCRIPTION = """\
在 WSL2 沙箱中执行一条 bash 命令并返回 stdout / stderr / exit_code。

何时使用：需要查看目录、检索代码、运行测试或构建、调用 git 等场景。
何时不用：修改文件内容请使用文件编辑工具，不要用重定向或 sed -i 改写源码。

注意：
- 命令必须是非交互式的，不能依赖用户输入（避免 git rebase -i、vim 等）。
- 变更类命令会先请求用户确认；被拒绝时你会收到一条说明，请换更低风险的做法。
- 一次只跑一条命令，看到结果再决定下一步。
"""


class ShellInput(BaseModel):
    command: str = Field(description="要执行的 bash 命令，单条、非交互式")
    reason: str = Field(description="执行这条命令的意图，一句话说明")
    cwd: str | None = Field(
        default=None,
        description="工作目录（WSL 内的绝对路径）。省略则使用工作区根目录",
    )


def build_shell_tool(settings: Settings, sandbox: WslSandbox) -> BaseTool:
    workspace = resolve_workspace(settings, sandbox)

    def _run(command: str, reason: str, cwd: str | None = None) -> str:
        # 等级只用于回报（前端着色、审计记账），不在这里拦
        level = classify(command).level

        try:
            workdir = ensure_inside(cwd, workspace) if cwd else workspace
        except SandboxPathError as exc:
            artifact = ShellArtifact(
                command=command,
                ok=False,
                rejected=True,
                level=int(level),
                level_label=level.label,
                decision="rejected",
            )
            return pack(f"工作目录非法：{exc}", artifact)

        result = sandbox.run(command, cwd=workdir)
        text = f"[{level.label}] {reason}\n{result.render(settings.max_output_chars)}"
        artifact = ShellArtifact(
            command=command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=int(level),
            level_label=level.label,
        )
        return pack(text, artifact)

    return StructuredTool.from_function(
        func=_run,
        name=SHELL_TOOL_NAME,
        description=SHELL_TOOL_DESCRIPTION,
        args_schema=ShellInput,
    )
