"""符号检索工具：按名字定位 Python 类/函数/方法（LSP-lite）。"""

from __future__ import annotations

import threading
import time

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.codeintel.lsp import (
    LspClient,
    command_from_settings,
)
from coding_agent.codeintel.python_index import collect_python_sources
from coding_agent.codeintel.symbols import (
    Symbol,
    extract_symbols,
    render_symbols,
)
from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import CallArtifact, pack

SYMBOL_SEARCH = "symbol_search"

SYMBOL_SEARCH_DESCRIPTION = """\
按名字搜索 Python 类/函数/方法，返回定义位置（文件:行号）。

何时使用：知道符号名，想快速定位定义；比全文 grep 更精确。
限制：只覆盖 Python AST 可见的类/函数/方法，不做跨语言或类型推断。
"""


class SymbolSearchInput(BaseModel):
    query: str = Field(description="符号名或名字片段，例如 DiscountService")
    limit: int = Field(default=20, ge=1, le=50)
    reason: str = Field(description="搜索这个符号的意图，一句话说明")


def build_symbol_tool(settings: Settings, sandbox: WslSandbox) -> BaseTool:
    root = resolve_workspace(settings, sandbox)
    lsp_lock = threading.Lock()
    lsp_client: LspClient | None = None
    lsp_disabled = False

    def _lsp_symbols(query: str, limit: int) -> list[Symbol] | None:
        nonlocal lsp_client, lsp_disabled
        if not settings.lsp_command or lsp_disabled:
            return None
        with lsp_lock:
            try:
                if lsp_client is None or not lsp_client.alive():
                    lsp_client = LspClient(
                        command_from_settings(settings.lsp_command),
                        root=root,
                        timeout=settings.lsp_timeout,
                    )
                    lsp_client.initialize()
                return lsp_client.workspace_symbol(query)[:limit]
            except Exception:  # noqa: BLE001 - LSP 失败回退 AST
                lsp_disabled = True
                if lsp_client is not None:
                    lsp_client.close()
                    lsp_client = None
                return None

    cached_symbols: list[Symbol] | None = None
    cached_at = 0.0

    def _ast_symbols(query: str, limit: int) -> list[Symbol]:
        nonlocal cached_symbols, cached_at
        ttl = float(getattr(settings, "symbol_index_ttl_s", 10.0) or 0.0)
        now = time.monotonic()
        if (
            cached_symbols is None
            or ttl <= 0
            or now - cached_at > ttl
        ):
            cached_symbols = extract_symbols(
                collect_python_sources(sandbox, root)
            )
            cached_at = now
        needle = str(query or "").strip().lower()
        if not needle:
            return []
        matches = [
            symbol
            for symbol in cached_symbols
            if needle in symbol.name.lower()
            or needle in symbol.qualified.lower()
        ]
        matches.sort(key=lambda item: (item.path, item.line, item.qualified))
        return matches[: max(1, int(limit))]

    def _run(query: str, limit: int = 20, reason: str = "") -> str:
        symbols = _lsp_symbols(query, limit)
        if not symbols:
            symbols = _ast_symbols(query, limit)
        text = f"[符号检索] {reason}\n{render_symbols(symbols)}"
        if len(text) > settings.max_output_chars:
            text = text[: settings.max_output_chars] + "\n…（输出已截断）"
        artifact = CallArtifact(tool=SYMBOL_SEARCH, ok=True)
        return pack(text, artifact)

    return StructuredTool.from_function(
        func=_run,
        name=SYMBOL_SEARCH,
        description=SYMBOL_SEARCH_DESCRIPTION,
        args_schema=SymbolSearchInput,
    )
