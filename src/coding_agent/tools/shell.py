"""Shell 工具：把模型输出的"命令意图"交给 WSL2 沙箱执行。

工具的职责边界：**校验 + 执行 + 回报**。
是否放行由 `sandbox.policy` 决定，工具本身不做安全判断。

返回值是 `artifacts.pack(文本, 产物)` 封装：文本给模型看，
产物给事件层和审计层用，两边都不必互相将就格式。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import SandboxPathError, ensure_inside
from coding_agent.sandbox.policy import CommandLevel, classify
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, pack

SHELL_TOOL_NAME = "shell_exec"

SHELL_TOOL_DESCRIPTION = """\
在 WSL2 沙箱中执行一条 bash 命令并返回 stdout / stderr / exit_code。

何时使用：需要查看目录、检索代码、运行测试或构建、调用 git 等场景。
何时不用：修改文件内容请使用文件编辑工具，不要用重定向或 sed -i 改写源码。

注意：
- 命令必须是非交互式的，不能依赖用户输入（避免 git rebase -i、vim 等）。
- 当前会话允许的最高命令等级为 {max_level}，超出等级的命令会被宿主拒绝。
- 一次只跑一条命令，看到结果再决定下一步。
"""


class ShellInput(BaseModel):
    command: str = Field(description="要执行的 bash 命令，单条、非交互式")
    reason: str = Field(description="执行这条命令的意图，一句话说明")
    cwd: str | None = Field(
        default=None,
        description="工作目录（WSL 内的绝对路径）。省略则使用工作区根目录",
    )


def _refusal(
    verdict_level: CommandLevel, reason: str, max_level: CommandLevel, command: str
) -> str:
    text = (
        f"命令被安全策略拒绝。\n"
        f"判定等级：{verdict_level.label}（{reason}）\n"
        f"当前会话允许的最高等级：{max_level.label}\n"
        f"请改用更低风险的方式达成同样目的。"
    )
    artifact = ShellArtifact(
        command=command,
        ok=False,
        rejected=True,
        level=int(verdict_level),
        level_label=verdict_level.label,
    )
    return pack(text, artifact)


def build_shell_tool(
    settings: Settings,
    sandbox: WslSandbox,
    *,
    max_level: CommandLevel = CommandLevel.READ,
) -> BaseTool:
    workspace = resolve_workspace(settings, sandbox)

    def _run(command: str, reason: str, cwd: str | None = None) -> str:
        verdict = classify(command)
        if verdict.level > max_level:
            return _refusal(verdict.level, verdict.reason, max_level, command)

        try:
            workdir = ensure_inside(cwd, workspace) if cwd else workspace
        except SandboxPathError as exc:
            artifact = ShellArtifact(
                command=command,
                ok=False,
                rejected=True,
                level=int(verdict.level),
                level_label=verdict.level.label,
            )
            return pack(f"工作目录非法：{exc}", artifact)

        result = sandbox.run(command, cwd=workdir)
        text = f"[{verdict.level.label}] {reason}\n{result.render(settings.max_output_chars)}"
        artifact = ShellArtifact(
            command=command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=int(verdict.level),
            level_label=verdict.level.label,
        )
        return pack(text, artifact)

    return StructuredTool.from_function(
        func=_run,
        name=SHELL_TOOL_NAME,
        description=SHELL_TOOL_DESCRIPTION.format(max_level=max_level.label),
        args_schema=ShellInput,
    )
