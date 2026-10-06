"""架构约束：UI 层只能通过 AgentRuntime 触达能力层。

若 TUI / Web 直接导入 tools 或 sandbox，就会长出第二条执行路径，
最危险的后果是「Web 端绕过命令分级审批」。这条约束用一个静态扫描守住。

**这条约束是安全属性，所以扫描本身必须堵死所有写法。** 早先的实现只处理
`import a.b` 与 `from a.b import c`（且要求 `node.level == 0`），于是三种写法
都能溜过去：

    from ..sandbox import policy        # 相对导入：level != 0，被整条跳过
    from coding_agent import sandbox    # 只记下 `coding_agent`，不匹配任何禁忌前缀
    from ..tools import files           # 同上

下面把「提取」与「判定」分开，并**为每种写法各留一条防绕过用例** ——
安全测试自己也要有测试。
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


def _package_of(path: Path, src: Path = SRC) -> str:
    """模块所属的包名：`.../coding_agent/tui/app.py` → `coding_agent.tui`。"""
    rel = path.resolve().relative_to(src.resolve())
    parts = rel.parts[:-1]
    return ".".join(["coding_agent", *parts]) if parts else "coding_agent"


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """把（可能是相对的）ImportFrom 解析成绝对模块名。

    `level=1` 指向当前包，`level=2` 指向上一级，依此类推。
    越出顶层的 level 返回 None（那种写法 Python 自己也会导入失败）。
    """
    if node.level == 0:
        return node.module

    parts = package.split(".")
    drop = node.level - 1
    if drop >= len(parts):
        return None
    base = parts[: len(parts) - drop]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _imports(source: str, package: str) -> set[str]:
    """抽出源码里被导入的所有绝对模块名。

    `from a.b import c` 同时记下 `a.b` 与 `a.b.c`：后者让
    `from coding_agent import sandbox` 这种「导入子模块」的写法也能被认出。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(node, package)
            if base is None:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _violations(imported: set[str]) -> list[str]:
    """命中了禁忌前缀的导入（判定与提取分开，好单独测）。"""
    return sorted(
        name
        for name in imported
        if any(name == bad or name.startswith(f"{bad}.") for bad in FORBIDDEN)
    )


def _imported_modules(path: Path) -> set[str]:
    return _imports(path.read_text(encoding="utf-8"), _package_of(path))


@pytest.mark.parametrize("package", UI_PACKAGES)
def test_ui_does_not_import_capability_or_sandbox(package: str) -> None:
    for path in _modules(package):
        offenders = _violations(_imported_modules(path))
        assert not offenders, (
            f"{path.relative_to(SRC)} 直接导入了 {offenders}；"
            f"UI 层只能通过 AgentRuntime 触达能力层，否则会出现绕过审批的旁路。"
        )


# ---------------- 扫描自身的防绕过 ----------------

# 每一种都必须被认出来。漏掉任何一种，这条安全约束就只剩虚假保证。
# 基准是 `coding_agent/tui/app.py`，其**所属包**是 `coding_agent.tui`。
_BYPASS_SPELLINGS = [
    "import coding_agent.sandbox.policy",
    "from coding_agent.sandbox import policy",
    "from coding_agent.sandbox.policy import classify",
    "from coding_agent import sandbox",
    "from coding_agent import tools",
    "from ..sandbox import policy",
    "from .. import sandbox",
    "from ..tools import files",
]

_LEGITIMATE = [
    "import coding_agent.runtime",
    "from coding_agent.runtime import AgentRuntime",
    "from coding_agent.events import RunFinished",
    "from coding_agent import runtime",
    "from . import events",
    "from .policy import decide",
    "from ..memory import sessions",
]


@pytest.mark.parametrize("source", _BYPASS_SPELLINGS)
def test_every_spelling_of_a_forbidden_import_is_caught(source: str) -> None:
    found = _imports(source, "coding_agent.tui")
    assert _violations(found), f"逃过扫描：{source!r} → 只抽出 {sorted(found)}"


@pytest.mark.parametrize("source", _LEGITIMATE)
def test_legitimate_imports_are_not_flagged(source: str) -> None:
    """反向对照：约束必须是因为「不该导入的没导入」而成立，不是因为扫描太宽。

    注意 `from . import events` 在 `tui/` 里指的是 `coding_agent.tui.events`
    （同包兄弟模块），并不是禁忌层 —— 判定要能区分这两者。
    """
    found = _imports(source, "coding_agent.tui")
    assert not _violations(found), f"误报：{source!r} → {sorted(found)}"


def test_tui_reaches_capabilities_only_through_runtime() -> None:
    """防止约束测试变成空转：满足约束必须是因为走了 runtime，而不是因为没写 UI。"""
    modules = _modules("tui")
    assert modules, "tui 包不存在，架构约束测试失去意义"
    assert any(
        "coding_agent.runtime" in _imported_modules(path) for path in modules
    ), "tui 没有通过 AgentRuntime 触达能力层"
