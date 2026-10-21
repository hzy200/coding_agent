"""LSP 语义查询工具：定义、引用、诊断。

需要显式配置 `AGENT_LSP_COMMAND`；未配置时返回说明，不静默假装支持。
AST 的 `symbol_search` 仍然可用，不依赖语言服务器。
"""

from __future__ import annotations

import threading
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.codeintel.lsp import LspClient, command_from_settings
from coding_agent.config import Settings
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import CallArtifact, pack

CODE_INTEL = "code_intel"

CODE_INTEL_DESCRIPTION = """\
查询 Python 符号的定义、引用或诊断（需要配置 AGENT_LSP_COMMAND）。

- definition：给定文件/行/列，返回定义位置；
- references：返回引用位置；
- diagnostics：打开文件并返回语言服务器诊断。
"""

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class CodeIntelInput(BaseModel):
    action: Literal["definition", "references", "diagnostics"]
    path: str = Field(description="工作区内相对路径")
    line: int = Field(default=1, ge=1)
    column: int = Field(default=1, ge=1)
    reason: str = Field(description="查询意图，一句话说明")


def build_code_intel_tool(settings: Settings, sandbox: WslSandbox) -> BaseTool:
    root = resolve_workspace(settings, sandbox)
    lock = threading.Lock()
    client: LspClient | None = None
    disabled = False

    def _client() -> LspClient | None:
        nonlocal client, disabled
        if disabled:
            return None
        if client is None or not client.alive():
            client = LspClient(
                command_from_settings(settings.lsp_command),
                root=root,
                timeout=settings.lsp_timeout,
            )
            client.initialize()
        return client

    def _run(
        action: str,
        path: str,
        line: int = 1,
        column: int = 1,
        reason: str = "",
    ) -> str:
        nonlocal client, disabled
        if not settings.lsp_command:
            return pack(
                "未配置 AGENT_LSP_COMMAND；symbol_search 仍可用 AST 索引。",
                CallArtifact(tool=CODE_INTEL, ok=False, skipped=True),
            )
        fs = SandboxFs(sandbox, root)
        try:
            resolved, info = fs.probe(path)
            if not info.exists or not info.is_file:
                return pack(
                    f"不是普通文件：{path}",
                    CallArtifact(tool=CODE_INTEL, ok=False),
                )
            text = fs.read_text(
                resolved, max_bytes=settings.max_file_read_bytes
            )
        except (SandboxPathError, SandboxFsError) as exc:
            return pack(
                f"无法读取 {path}：{type(exc).__name__}: {exc}",
                CallArtifact(tool=CODE_INTEL, ok=False),
            )

        with lock:
            try:
                lsp = _client()
                if lsp is None:
                    raise RuntimeError("LSP 客户端不可用")
                lsp.did_open(resolved, text)
                if action == "diagnostics":
                    diagnostics = lsp.diagnostics(
                        resolved,
                        wait=settings.lsp_diagnostics_wait_s,
                    )
                    body = _render_diagnostics(diagnostics)
                else:
                    if action == "definition":
                        locations = lsp.definition(resolved, line, column)
                    else:
                        locations = lsp.references(resolved, line, column)
                    body = _render_locations(locations)
            except Exception as exc:  # noqa: BLE001 - 失败显式回报
                disabled = True
                if client is not None:
                    client.close()
                    client = None
                body = f"LSP 查询失败：{type(exc).__name__}: {exc}"
                return pack(
                    body,
                    CallArtifact(tool=CODE_INTEL, ok=False, skipped=True),
                )
        text_out = f"[{action}] {reason}\n{body}"
        if len(text_out) > settings.max_output_chars:
            text_out = text_out[: settings.max_output_chars] + "\n…（输出已截断）"
        return pack(text_out, CallArtifact(tool=CODE_INTEL, ok=True))

    return StructuredTool.from_function(
        func=_run,
        name=CODE_INTEL,
        description=CODE_INTEL_DESCRIPTION,
        args_schema=CodeIntelInput,
    )


def _render_locations(locations: list[tuple[str, int, int]]) -> str:
    if not locations:
        return "未找到位置。"
    return "\n".join(
        f"{path}:{line}:{column}" for path, line, column in locations
    )


def _render_diagnostics(diagnostics: list[dict]) -> str:
    if not diagnostics:
        return "没有诊断。"
    lines: list[str] = []
    for item in diagnostics:
        severity = _SEVERITY.get(int(item.get("severity", 0) or 0), "info")
        start = (item.get("range") or {}).get("start") or {}
        line = int(start.get("line", 0)) + 1
        source = str(item.get("source") or "")
        code = item.get("code")
        tag = source + (f"/{code}" if code not in (None, "") else "")
        prefix = f" [{tag}]" if tag else ""
        lines.append(
            f"{line}: [{severity}]{prefix} {item.get('message', '')}"
        )
    return "\n".join(lines)
