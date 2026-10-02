"""架构约束：UI 层只能通过 AgentRuntime 触达能力层。

若 TUI / Web 直接导入 tools 或 sandbox，就会长出第二条执行路径，
最危险的后果是「Web 端绕过命令分级审批」。这条约束用一个静态扫描守住。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "coding_agent"

# 前端包：只允许依赖 runtime / events / config 这些中立层
UI_PACKAGES = ("tui", "web")
FORBIDDEN = ("coding_agent.tools", "coding_agent.sandbox")


def _modules(package: str) -> list[Path]:
    directory = SRC / package
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package", UI_PACKAGES)
def test_ui_does_not_import_capability_or_sandbox(package: str) -> None:
    for path in _modules(package):
        imported = _imported_modules(path)
        for forbidden in FORBIDDEN:
            offenders = sorted(
                n for n in imported if n == forbidden or n.startswith(f"{forbidden}.")
            )
            assert not offenders, (
                f"{path.relative_to(SRC)} 直接导入了 {offenders}；"
                f"UI 层只能通过 AgentRuntime 触达能力层，否则会出现绕过审批的旁路。"
            )


def test_tui_reaches_capabilities_only_through_runtime() -> None:
    """防止约束测试变成空转：满足约束必须是因为走了 runtime，而不是因为没写 UI。"""
    modules = _modules("tui")
    assert modules, "tui 包不存在，架构约束测试失去意义"
    assert any(
        "coding_agent.runtime" in _imported_modules(path) for path in modules
    ), "tui 没有通过 AgentRuntime 触达能力层"
