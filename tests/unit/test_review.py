"""代码审查的规则、解析与节点契约。

分两层测：

1. **规则**在宿主 Python 上直接跑 `script_body()`。检查逻辑全是 stdlib 文件操作，
   没有理由非要在 WSL 里才能验证 —— 所以这一半不需要 `-m wsl`，能进 CI。
2. **解析与短路**用桩件，同样不碰沙箱。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from coding_agent.config import Settings
from coding_agent.graph.nodes.review import make_review_node
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.review import (
    _MARKER,
    BLOCKING,
    WARNING,
    ReviewFinding,
    ReviewResult,
    parse_review,
    script_body,
)

SID_OLD = "20260101T000000000000000-aaaaaa"
SID_NEW = "20260102T000000000000000-bbbbbb"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """建一个最小的 `.agent/backups/<sid>/` 布局：只有 SID_NEW 之后的算"本次改动"。"""
    (tmp_path / ".agent/backups" / SID_OLD).mkdir(parents=True)
    (tmp_path / ".agent/backups" / SID_NEW).mkdir(parents=True)
    return tmp_path


def _before(workspace: Path, relpath: str, content: str) -> None:
    target = workspace / ".agent/backups" / SID_NEW / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _absent(workspace: Path, relpath: str) -> None:
    """留底记的是「当时这个文件不存在」。"""
    _before(workspace, f"{relpath}.absent", "")


def _now(workspace: Path, relpath: str, content: str) -> None:
    target = workspace / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _run(workspace: Path, *, watermark: str = SID_OLD) -> dict:
    """用宿主 Python 跑一遍审查脚本，返回它的 JSON 输出。"""
    script = workspace / "_review_body.py"
    script.write_text(script_body(str(workspace), watermark=watermark), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(_MARKER))
    return json.loads(line[len(_MARKER) :])


def _rules(payload: dict, severity: str | None = None) -> list[str]:
    return [
        f["rule"]
        for f in payload["findings"]
        if severity is None or f["severity"] == severity
    ]


# ---------------- 规则：结构性缺陷（blocking） ----------------

def test_syntax_error_blocks(workspace: Path) -> None:
    _before(workspace, "broken.py", "def ok():\n    return 1\n")
    _now(workspace, "broken.py", "def (\n")

    payload = _run(workspace)
    assert "syntax-error" in _rules(payload, BLOCKING)
    assert payload["checked_files"] == 1


def test_debug_leftovers_block(workspace: Path) -> None:
    _before(workspace, "a.py", "def f():\n    return 1\n")
    _now(workspace, "a.py", "def f():\n    breakpoint()\n    return 1\n")

    payload = _run(workspace)
    finding = next(f for f in payload["findings"] if f["rule"] == "debug-breakpoint")
    assert finding["severity"] == BLOCKING
    assert finding["location"] == "a.py:2"  # 行号指向**新增**的那一行


def test_weakened_tests_block(workspace: Path) -> None:
    """把测试改弱以迎合实现 —— 这是"测试通过"最廉价的伪造方式。"""
    _before(workspace, "tests/test_a.py", "class T:\n    def test_x(self):\n        pass\n")
    _now(
        workspace,
        "tests/test_a.py",
        "import unittest\n"
        "\n"
        "\n"
        "@unittest.skip\n"
        "class T:\n"
        "    def test_x(self):\n"
        "        assert True\n",
    )

    payload = _run(workspace)
    rules = _rules(payload, BLOCKING)
    assert "test-skipped" in rules
    assert "test-weakened" in rules


def test_assert_true_outside_tests_is_not_blocking(workspace: Path) -> None:
    """`assert True` 出现在普通代码里不是"改弱测试"，只当告警。

    规则只对测试文件生效 —— 误伤会把无关代码判成缺陷，让阻断失去分量。
    """
    _before(workspace, "a.py", "def f():\n    return 1\n")
    _now(workspace, "a.py", "def f():\n    assert True\n    return 1\n")

    payload = _run(workspace)
    assert "test-weakened" not in _rules(payload)


def test_writing_into_agent_state_blocks(workspace: Path) -> None:
    """`.agent/` 是 agent 自己的备份与审计目录，模型不该动它。"""
    _before(workspace, ".agent/notes.md", "old\n")
    _now(workspace, ".agent/notes.md", "new\n")

    payload = _run(workspace)
    assert "agent-state" in _rules(payload, BLOCKING)


# ---------------- 规则：只对新增行生效，且只对告警级 ----------------

def test_preexisting_problems_are_not_reported(workspace: Path) -> None:
    """**只审新增行**。种子文件里本来就有的 `print(` 不算这次改动引入的问题。

    否则每个任务都会带一堆与本次改动无关的告警，真正的发现被淹没。
    """
    _before(workspace, "a.py", "print('本来就是它')\ndef f():\n    return 1\n")
    _now(workspace, "a.py", "print('本来就是它')\ndef f():\n    print('新加的')\n    return 1\n")

    payload = _run(workspace)
    prints = [f for f in payload["findings"] if f["rule"] == "debug-print"]
    assert len(prints) == 1
    assert prints[0]["location"] == "a.py:3"


def test_todo_and_print_are_warnings(workspace: Path) -> None:
    _before(workspace, "a.py", "def f():\n    return 1\n")
    _now(workspace, "a.py", "def f():\n    print('debug')\n    return 1  # TODO 收拾\n")

    payload = _run(workspace)
    rules = _rules(payload, WARNING)
    assert "debug-print" in rules
    assert "todo-marker" in rules


def test_a_new_file_counts_as_all_added(workspace: Path) -> None:
    """新建文件走 `.absent` 标记：整份内容都算新增。"""
    _absent(workspace, "new.py")
    _now(workspace, "new.py", "print('hello')\n")

    payload = _run(workspace)
    finding = next(f for f in payload["findings"] if f["rule"] == "debug-print")
    assert finding["location"] == "new.py:1"


def test_unchanged_file_is_not_reviewed(workspace: Path) -> None:
    _before(workspace, "a.py", "print('x')\n")
    _now(workspace, "a.py", "print('x')\n")

    payload = _run(workspace)
    assert payload["checked_files"] == 0
    assert payload["findings"] == []


# ---------------- 水位线 ----------------

def test_snapshots_at_or_below_the_watermark_are_ignored(workspace: Path) -> None:
    """水位线是防"跨步重复告警"的关键。

    留底记的是**写前内容**，所以上一步改过的文件会永远与自己的留底不同。
    没有水位线的话，每一步都会把前面所有步骤的改动重审一遍。
    """
    _before(workspace, "a.py", "def f():\n    return 1\n")
    _now(workspace, "a.py", "def f():\n    breakpoint()\n")

    payload = _run(workspace, watermark=SID_NEW)  # 已经审到最新了
    assert payload["findings"] == []
    assert payload["checked_files"] == 0


def test_watermark_advances_to_the_newest_snapshot(workspace: Path) -> None:
    _before(workspace, "a.py", "def f():\n    return 1\n")
    _now(workspace, "a.py", "def f():\n    return 2\n")

    assert _run(workspace)["watermark"] == SID_NEW
    # 没有新留底时保持原值，不能倒退
    assert _run(workspace, watermark=SID_NEW)["watermark"] == SID_NEW


def test_earliest_snapshot_in_range_is_the_baseline(workspace: Path) -> None:
    """一批留底里取**最早**那份当"改动前"。

    取最新的话，同一步内的多次修改只会看到最后一笔，前面引入的问题就漏了。
    """
    mid = "20260101T120000000000000-midmid"
    (workspace / ".agent/backups" / mid).mkdir(parents=True)
    (workspace / ".agent/backups" / mid / "a.py").write_text("v1\n", encoding="utf-8")
    _before(workspace, "a.py", "v2\n")  # SID_NEW 更晚
    _now(workspace, "a.py", "v1\nprint('x')\n")

    payload = _run(workspace, watermark=SID_OLD)
    # 与最早的 v1 比 → 只有 print 那一行是新增；若拿 v2 比，整份都算"新增"
    finding = next(f for f in payload["findings"] if f["rule"] == "debug-print")
    assert finding["location"] == "a.py:2"


# ---------------- 解析与短路 ----------------

def test_result_renders_blocking_and_warning_separately() -> None:
    result = ReviewResult(
        status="blocked",
        checked_files=3,
        findings=[
            ReviewFinding(severity=BLOCKING, rule="x", location="a.py:1", message="坏了"),
            ReviewFinding(severity=WARNING, rule="y", location="b.py:2", message="脏"),
        ],
    )
    assert result.blocked
    assert len(result.blocking_findings) == 1
    assert len(result.warnings) == 1
    assert "3 个改动文件" in result.render()
    assert "a.py:1 坏了" in result.render()


def test_a_crashed_review_is_not_reported_as_clean() -> None:
    """审查没跑起来 ≠ 没问题。

    这是本项目反复踩过的失败形态（verify 认不出测试命令就静默当通过），
    所以这里显式记一条可见的告警。
    """
    result = parse_review(
        None,  # type: ignore[arg-type]
        "/ws",
        ExecResult(command="x", exit_code=1, stdout="", stderr="boom", duration_ms=1),
        enable_linters=False,
    )
    assert result.status == "warned"
    assert not result.blocked
    assert result.findings[0].rule == "review-unavailable"


def test_noop_job_can_be_parsed_from_surrounding_output() -> None:
    """输出里混了别的行也不该崩 —— 崩了就会被当成"审过了没问题"。"""
    stdout = 'noise\n{"@type": "x"}\n' + _MARKER + json.dumps(
        {"findings": [], "checked_files": 0, "watermark": "s1"}
    )
    result = parse_review(
        None,  # type: ignore[arg-type]
        "/ws",
        ExecResult(command="x", exit_code=0, stdout=stdout, stderr="", duration_ms=1),
        enable_linters=False,
    )
    assert result.status == "clean"
    assert result.watermark == "s1"


# ---------------- 节点短路 ----------------

class _FakeSandbox:
    def __init__(self) -> None:
        self.settings = Settings(_env_file=None, wsl_workspace="/ws")
        self.runs = 0

    def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None):
        self.runs += 1
        return ExecResult(command=command, exit_code=0, stdout="", stderr="", duration_ms=1)

    def cached_probe(self, key: str, producer):
        return producer()

    def forget_probe(self, key: str) -> None:
        return None


def test_clean_step_skips_review() -> None:
    """没改动就没什么可审的 —— 与 verify 的短路条件一致。"""
    sandbox = _FakeSandbox()
    out = make_review_node(sandbox, "/ws")({"dirty": False}, None)  # type: ignore[arg-type]
    assert out == {"review": {}}
    assert sandbox.runs == 0


def test_review_disabled_short_circuits() -> None:
    sandbox = _FakeSandbox()
    sandbox.settings = Settings(_env_file=None, wsl_workspace="/ws", review_enabled=False)
    out = make_review_node(sandbox, "/ws")({"dirty": True}, None)  # type: ignore[arg-type]
    assert out == {"review": {}}
    assert sandbox.runs == 0


def test_node_passes_the_watermark_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """水位线从 state 读、推进后写回 —— 这是跨步骤不重复告警的传递链。"""
    captured: dict[str, object] = {}

    def _fake_run(_sandbox, _root, *, watermark="", enable_linters=True):
        captured.update(watermark=watermark, enable_linters=enable_linters)
        return ReviewResult(status="clean", watermark=SID_NEW, checked_files=1)

    monkeypatch.setattr("coding_agent.graph.nodes.review.run_review", _fake_run)

    sandbox = _FakeSandbox()
    out = make_review_node(sandbox, "/ws")(
        {"dirty": True, "review_watermark": SID_OLD}, None  # type: ignore[arg-type]
    )
    assert captured["watermark"] == SID_OLD
    assert out["review_watermark"] == SID_NEW
    assert out["review"]["status"] == "clean"


# ---------------- 图级：审查阻断环的收敛 ----------------
#
# 两道质量关**共用**一份修复预算（`retry`）。共用是否安全，只有把环跑起来才知道：
# 少了任何一条边都会变成"空转到额度耗尽"或"直接 GraphRecursionError"。

def _blocking_review_graph(limit: int):
    """验证永远通过、审查永远阻断的迷你图，拓扑与真实图一致。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    from coding_agent.graph.nodes.repair import repair
    from coding_agent.graph.routing import (
        ADVANCE,
        REPAIR,
        REPLAN,
        RESPOND,
        REVIEW,
        VERIFY,
        make_route_after_review,
        make_route_after_verify,
        route_after_act,
    )
    from coding_agent.graph.state import AgentState

    calls = {"act": 0, "review": 0, "replan": 0}

    def act(state, config):
        calls["act"] += 1
        return {"messages": [AIMessage(content=f"attempt {calls['act']}")]}

    def verify_node(state, config):
        return {"verification": {"status": "ok"}}

    def review_node(state, config):
        calls["review"] += 1
        return {
            "review": {
                "status": "blocked",
                "summary": "1 个阻断问题",
                "findings": [
                    {"severity": "blocking", "location": "a.py:1", "message": "还有 breakpoint()"}
                ],
            }
        }

    def replan_node(state, config):
        calls["replan"] += 1
        # 放弃：砍掉剩余步骤，让任务如实收尾
        return {"plan": []}

    graph = StateGraph(AgentState)
    graph.add_node("act", act)
    graph.add_node(VERIFY, verify_node)
    graph.add_node(REVIEW, review_node)
    graph.add_node(REPAIR, repair)
    graph.add_node(REPLAN, replan_node)
    # 条件边的映射里出现的目标都必须存在，所以 ADVANCE 要有节点 ——
    # 审查永远阻断，这条边实际走不到，但它必须在。
    graph.add_node(ADVANCE, lambda state, config: {})
    graph.add_node(RESPOND, lambda state, config: {})
    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {VERIFY: VERIFY})
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, REVIEW: REVIEW, RESPOND: RESPOND},
    )
    graph.add_conditional_edges(
        REVIEW,
        make_route_after_review(limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, ADVANCE: ADVANCE, RESPOND: RESPOND},
    )
    graph.add_edge(REPAIR, "act")
    graph.add_edge(REPLAN, RESPOND)
    graph.add_edge(ADVANCE, RESPOND)
    graph.add_edge(RESPOND, END)
    return graph.compile(checkpointer=MemorySaver()), calls


def test_review_blocking_loop_converges_and_reviews_every_round() -> None:
    """审查阻断 → repair → 改完再验再审，到额度用尽转 replan，环必须收敛。

    关键断言是 **review 每轮都重跑**：`repair` 刻意不清 `review`（act 要靠它
    知道该改什么），所以如果 `route_after_verify` 也去看 review，验证一通过就会
    被陈旧的阻断直接打回 repair —— 审查再也不跑，改没改好没人确认。
    """
    limit = 3
    app, calls = _blocking_review_graph(limit)

    asyncio.run(app.ainvoke(
        {"messages": [], "plan": ["做点什么"], "step_idx": 0, "dirty": True, "retry": 0},
        {"configurable": {"thread_id": "review-loop"}, "recursion_limit": 200},
    ))

    # 初次 + limit 次修复，每次都要重新过一遍审查
    assert calls["review"] == limit + 1
    assert calls["act"] == limit + 1
    assert calls["replan"] == 1  # 额度用尽后换做法（这里简化为放弃）


def test_review_blocking_loop_terminates_with_a_zero_budget() -> None:
    """修复预算为 0 时一次都不重试，直接去 replan —— 不能变成死循环。"""
    app, calls = _blocking_review_graph(0)

    asyncio.run(app.ainvoke(
        {"messages": [], "plan": ["做点什么"], "step_idx": 0, "dirty": True, "retry": 0},
        {"configurable": {"thread_id": "review-zero"}, "recursion_limit": 50},
    ))

    assert calls["review"] == 1
    assert calls["act"] == 1
    assert calls["replan"] == 1


# ---------------- 图级：空步骤只重做一次 ----------------

def _nudge_graph(*, nudge_enabled: bool, limit: int = 3):
    """迷你图：模型每次都不改任何东西（`dirty` 恒为 False）。

    这复现的是实测里那种"把计划读完就算完成"—— `verify` 与 `review` 都因
    `dirty=False` 而短路，控制流一路 advance。nudge 是这条路上的补救。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    from coding_agent.graph.nodes.nudge import nudge as nudge_node
    from coding_agent.graph.nodes.repair import repair
    from coding_agent.graph.routing import (
        ADVANCE,
        NUDGE,
        REPAIR,
        REPLAN,
        RESPOND,
        REVIEW,
        VERIFY,
        make_route_after_review,
        make_route_after_verify,
        route_after_act,
    )
    from coding_agent.graph.state import AgentState

    calls = {"act": 0, "nudge": 0, "advance": 0, "respond": 0}

    def act(state, config):
        calls["act"] += 1
        return {"messages": [AIMessage(content="我读完了")]}

    def verify_node(state, config):
        # `dirty=False` 时真节点就是这条短路
        return {"verification": {}}

    def review_node(state, config):
        return {"review": {}}

    def counting(name, inner):
        # nudge 是**纯函数**（只收 state）—— LangGraph 靠签名决定传几个参数，
        # 包一层之后要自己按同样口径调用，否则会多传一个 config。
        def wrapper(state, config):
            calls[name] += 1
            return inner(state)
        return wrapper

    graph = StateGraph(AgentState)
    graph.add_node("act", act)
    graph.add_node(VERIFY, verify_node)
    graph.add_node(REVIEW, review_node)
    graph.add_node(NUDGE, counting("nudge", nudge_node))
    graph.add_node(REPAIR, repair)
    graph.add_node(REPLAN, lambda state, config: {"plan": []})
    graph.add_node(ADVANCE, counting("advance", lambda state: {}))
    graph.add_node(RESPOND, counting("respond", lambda state: {}))
    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {VERIFY: VERIFY})
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, REVIEW: REVIEW, RESPOND: RESPOND},
    )
    graph.add_conditional_edges(
        REVIEW,
        make_route_after_review(limit, nudge_empty_steps=nudge_enabled),
        {NUDGE: NUDGE, REPAIR: REPAIR, REPLAN: REPLAN, ADVANCE: ADVANCE, RESPOND: RESPOND},
    )
    graph.add_edge(NUDGE, "act")
    graph.add_edge(REPAIR, "act")
    graph.add_edge(REPLAN, RESPOND)
    graph.add_edge(ADVANCE, RESPOND)
    graph.add_edge(RESPOND, END)
    return graph.compile(checkpointer=MemorySaver()), calls


def _run_nudge_graph(enabled: bool) -> dict:
    app, calls = _nudge_graph(nudge_enabled=enabled)
    asyncio.run(app.ainvoke(
        # 两步计划：单步计划下 `has_more_steps` 为假会直接去 RESPOND，
        # 那样就验不到 advance 这一跳
        {"messages": [], "plan": ["实现折扣", "跑测试"], "step_idx": 0,
         "dirty": False, "retry": 0},
        {"configurable": {"thread_id": f"nudge-{enabled}"}, "recursion_limit": 200},
    ))
    return calls


def test_a_step_that_changes_nothing_is_retried_once_then_accepted() -> None:
    """只重做一次，然后放行 —— 不能变成死循环，也不能无限要求"必须有改动"。"""
    calls = _run_nudge_graph(enabled=True)

    assert calls["nudge"] == 1
    assert calls["act"] == 2  # 初次 + 重做一次
    assert calls["advance"] == 1


def test_without_the_nudge_the_empty_step_passes_straight_through() -> None:
    """关掉开关时回到旧行为（现有基线要有一条可比的路）。"""
    calls = _run_nudge_graph(enabled=False)

    assert calls["nudge"] == 0
    assert calls["act"] == 1
    assert calls["advance"] == 1
