"""图节点 ↔ 事件翻译的覆盖约束。

runtime 把图节点的更新翻译成领域事件，用的是 `_stream` 里一长串 `if/elif`。
漏翻译既不会报错、也不会有测试失败 —— 只是前端时间线上少一行。这个约束
用 AST 从 `_stream` 源码里抽出它**真正比较过**的节点名，与 `build_graph`
编译出的真实节点集比对，把"静默漏翻译"变成一次断言失败。

为什么抽源码而不是维护一份常量：常量会与 `if/elif` 漂移，而 AST 读的是
实现本身。反方向（有分支但节点已改名）由第二个用例守着。
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from langgraph.checkpoint.memory import MemorySaver

from coding_agent.config import Settings
from coding_agent.graph.build import build_graph
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.wsl_exec import ExecResult, WslSandbox

# 建图时检索工具要探测 rg。给固定应答，让本用例不依赖真实 WSL
# （它是一条约 200ms 的结构约束，不该被沙箱拖成集成测试）。
_FAKE_PROBE = ExecResult(
    command="command -v rg", exit_code=0, stdout="/usr/bin/rg", stderr="", duration_ms=0
)

# 刻意不产事件的节点。**有意的沉默必须显式登记并写明理由**，
# 否则新增节点被漏翻译时，这个测试就不会响。
SILENT_NODES = {
    # 审批决定本身不是给用户看的结果：需要确认时走 __interrupt__ → ApprovalRequested，
    # 放行/拒绝则由 tools 节点执行后产出 ToolCallFinished。它只往 state 写 approvals。
    "approval_gate",
    # survey 只把仓库画像写进 state；用户可见的选择结果由 select_workflow 发出。
    "survey",
}

_META_NODES = {"__start__", "__end__"}


@pytest.fixture
def real_graph(monkeypatch: pytest.MonkeyPatch):
    """真实编译的图，只有沙箱探测被替换掉。"""
    monkeypatch.setattr(
        WslSandbox, "_exec", lambda self, script, *, wall, command="": _FAKE_PROBE
    )
    settings = Settings(_env_file=None, deepseek_api_key="x", wsl_workspace="/tmp/agent-eval")
    return build_graph(settings, checkpointer=MemorySaver())


def _graph_nodes(graph) -> set[str]:
    return set(graph.get_graph().nodes) - _META_NODES


def _translated_nodes() -> tuple[set[str], set[str]]:
    """`_stream` 里与 `node` 比较过的字面量，以及 `node.startswith(...)` 前缀。

    Phase 3D 有四个 steering 节点共用一套翻译，用前缀分支避免复制四遍
    if/elif；测试必须能识别这种写法，否则会误报它们"没有翻译"。
    """
    source = textwrap.dedent(inspect.getsource(AgentRuntime._stream))
    names: set[str] = set()
    prefixes: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            if not (isinstance(node.left, ast.Name) and node.left.id == "node"):
                continue
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if (
                    isinstance(op, ast.Eq)
                    and isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                ):
                    names.add(comparator.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "node"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            prefixes.add(node.args[0].value)
    return names, prefixes


def _is_translated(node: str, names: set[str], prefixes: set[str]) -> bool:
    return node in names or any(node.startswith(prefix) for prefix in prefixes)


def test_every_graph_node_is_translated_or_declared_silent(real_graph) -> None:
    nodes = _graph_nodes(real_graph)
    names, prefixes = _translated_nodes()
    untranslated = {node for node in nodes if not _is_translated(node, names, prefixes)}
    assert untranslated == SILENT_NODES, (
        f"这些图节点没有对应的事件翻译：{sorted(untranslated - SILENT_NODES)}。"
        f"要么在 runtime._stream 里补上分支，要么在 SILENT_NODES 里登记并写明"
        f"为什么它对用户不可见。"
    )


def test_no_translation_branch_for_a_missing_node(real_graph) -> None:
    """反向约束：翻译分支必须对应真实节点，防止节点改名后留下死分支。"""
    nodes = _graph_nodes(real_graph)
    names, prefixes = _translated_nodes()
    dead = {name for name in names if name not in nodes}
    assert not dead, f"这些翻译分支对应的节点已不存在：{sorted(dead)}"
    for prefix in prefixes:
        assert any(node.startswith(prefix) for node in nodes), (
            f"前缀翻译分支 {prefix!r} 没有匹配任何真实节点"
        )


def test_the_constraint_is_not_vacuous(real_graph) -> None:
    """防止约束退化成空转：两边都必须非空且规模合理。"""
    assert len(_graph_nodes(real_graph)) >= 8, "图节点太少，约束失去意义"
    names, prefixes = _translated_nodes()
    assert len(names) >= 7, "没有抽到任何翻译分支，AST 提取可能失效"
    assert prefixes, "没有抽到 startswith 分支"


def _edges(real_graph) -> set[tuple[str, str]]:
    return {(edge.source, edge.target) for edge in real_graph.get_graph().edges}


def test_review_sits_between_verify_and_advance(real_graph) -> None:
    """两道质量关是**串联**的：验证通过才轮到审查，审查通过才步进。

    按拓扑断言而不是按行为断言：这里要挡的是「接线接错位置」，而节点各自的
    单测对此无能为力 —— 每个节点都是对的，错的只是它们之间的连线。
    """
    edges = _edges(real_graph)

    assert ("verify", "review") in edges
    assert ("review", "advance") in edges
    # 审查阻断与验证失败走**同一条**出口，不是各修各的
    assert ("review", "repair") in edges
    assert ("review", "replan") in edges
    # 反向：审查不能排到步进之后 —— 那会去审"下一步"的改动
    assert ("advance", "review") not in edges
    # 也不能绕开审查直接步进
    assert ("verify", "advance") not in edges
