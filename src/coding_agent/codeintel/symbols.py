"""AST 符号检索（LSP-lite）。

完整 LSP 需要语言服务器、文档同步与增量诊断；这里先提供**静态符号搜索**：
按名字找类/函数/方法及定义位置。对 Python 项目足够定位入口，且不引入
外部进程。真实 LSP 适配仍是后续工作。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    parent: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


def extract_symbols(sources: dict[str, str]) -> list[Symbol]:
    symbols: list[Symbol] = []
    for path, source in sources.items():
        normalized = str(path).replace("\\", "/").lstrip("./")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    Symbol(node.name, "class", normalized, node.lineno)
                )
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = (
                            "async method"
                            if isinstance(child, ast.AsyncFunctionDef)
                            else "method"
                        )
                        symbols.append(
                            Symbol(
                                child.name,
                                kind,
                                normalized,
                                child.lineno,
                                parent=node.name,
                            )
                        )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = (
                    "async function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                )
                symbols.append(
                    Symbol(node.name, kind, normalized, node.lineno)
                )
    return symbols


def search_symbols(
    sources: dict[str, str], query: str, *, limit: int = 20
) -> list[Symbol]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    matches = [
        symbol
        for symbol in extract_symbols(sources)
        if needle in symbol.name.lower()
        or needle in symbol.qualified.lower()
    ]
    matches.sort(key=lambda item: (item.path, item.line, item.qualified))
    return matches[: max(1, int(limit))]


def render_symbols(symbols: list[Symbol]) -> str:
    if not symbols:
        return "未找到匹配的符号。"
    return "\n".join(
        f"{item.path}:{item.line} [{item.kind}] {item.qualified}"
        for item in symbols
    )
