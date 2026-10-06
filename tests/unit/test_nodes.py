from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.graph.nodes.act import BUDGET_EXHAUSTED_TEMPLATE, make_act_node
from coding_agent.graph.nodes.advance import advance
from coding_agent.graph.nodes.tools import make_tools_node
from coding_agent.llm.prompts import compose_system_prompt
from coding_agent.tools.artifacts import ShellArtifact, pack


class _EchoLLM:
    """记录收到的消息，便于断言 act 往提示里塞了什么。"""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.seen: list = []

    def invoke(self, messages, config=None):  # noqa: ANN001
        self.seen = messages
        return AIMessage(content=self.reply)

    async def ainvoke(self, messages, config=None):  # noqa: ANN001
        return self.invoke(messages, config)


class _AsyncOnlyLLM:
    """只实现 `ainvoke`：一旦节点退回同步调用，这里立刻炸。"""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("不该走同步 invoke —— 那样 Ctrl-C 取消不了")

    async def ainvoke(self, messages, config=None):  # noqa: ANN001
        self.calls += 1
        return AIMessage(content="ok")


def _act(node, state, config=None):  # noqa: ANN001
    """act 是 async 节点（走 ainvoke 才能取消），单测里驱动一次。"""
    return asyncio.run(node(state, config))


# ---------------- advance ----------------

def test_advance_increments_step_and_resets_per_step_state() -> None:
    """每步的轮次预算、dirty、验证结果都要清零，否则会跨步误判。"""
    out = advance(
        {
            "messages": [],
            "plan": ["a", "b"],
            "step_idx": 0,
            "tool_rounds": 7,
            "dirty": True,
            "verification": {"status": "failed"},
            "retry": 2,
        }
    )
    assert out == {
        "step_idx": 1,
        "tool_rounds": 0,
        "budget_exhausted": False,
        "dirty": False,
        "verification": {},
        "review": {},
        "retry": 0,
    }
    # review_watermark **不在这里清**：它必须跨步骤保留，否则每一步都会重审
    # 前面所有步骤的改动（留底记的是写前内容，老文件永远与自己的留底不同）
    assert "review_watermark" not in out


# ---------------- act ----------------

def test_act_injects_plan_and_marks_current_step() -> None:
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5)
    _act(
        node,
        {
            "messages": [HumanMessage(content="do it")],
            "plan": ["first", "second"],
            "step_idx": 1,
            "tool_rounds": 0,
        },
    )

    assert isinstance(llm.seen[0], SystemMessage)
    system = llm.seen[0].content
    assert "first" in system and "（已完成）" in system
    assert "second" in system and "← 现在只做这一步" in system
    # 步骤上下文只进系统提示，不写回消息历史
    assert llm.seen[1:] == [HumanMessage(content="do it")]


def test_act_injects_long_term_memories() -> None:
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5)
    _act(
        node,
        {
            "messages": [HumanMessage(content="do it")],
            "memories": ["这个仓库用 pytest", "别动 legacy/"],
            "tool_rounds": 0,
        },
    )
    system = llm.seen[0].content
    assert "这个仓库用 pytest" in system
    assert "别动 legacy/" in system
    assert "长期记忆" in system


def test_act_without_memories_has_no_memory_section() -> None:
    llm = _EchoLLM()
    _act(
        make_act_node(llm, max_tool_rounds=5),
        {"messages": [HumanMessage(content="x")], "tool_rounds": 0},
    )
    assert "长期记忆" not in llm.seen[0].content


def test_act_injects_verification_feedback_on_retry() -> None:
    """修复轮次里，结构化错误必须进系统提示 —— 否则模型在盲改。"""
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3)
    _act(
        node,
        {
            "messages": [HumanMessage(content="修好它")],
            "tool_rounds": 0,
            "retry": 1,
            "verification": {
                "status": "failed",
                "command": "python3 -m unittest discover",
                "summary": "FAILED (failures=1)",
                "issues": [{"location": "calc.py:2", "message": "assertEqual"}],
            },
        },
    )
    system = llm.seen[0].content
    assert "calc.py:2" in system
    assert "第 1/3 次" in system


def test_act_has_no_feedback_on_the_first_attempt() -> None:
    llm = _EchoLLM()
    _act(
        make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3),
        {"messages": [HumanMessage(content="开始")], "tool_rounds": 0, "retry": 0},
    )
    assert "次修复" not in llm.seen[0].content


def test_act_ignores_stale_passing_verification() -> None:
    llm = _EchoLLM()
    _act(
        make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3),
        {
            "messages": [HumanMessage(content="继续")],
            "tool_rounds": 0,
            "retry": 1,
            "verification": {"status": "ok"},
        },
    )
    assert "次修复" not in llm.seen[0].content


def test_act_goes_through_the_async_llm_api() -> None:
    """act 必须走 `ainvoke`：同步 invoke 会被丢进线程池，取消信号传不进去
    （Ctrl-C 在模型生成期间完全失效）。这个桩**只**实现 ainvoke。"""
    llm = _AsyncOnlyLLM()
    out = _act(
        make_act_node(llm, max_tool_rounds=3),
        {"messages": [HumanMessage(content="x")], "tool_rounds": 0},
    )

    assert llm.calls == 1
    assert out["tool_rounds"] == 1


def test_act_counts_rounds() -> None:
    node = make_act_node(_EchoLLM(), max_tool_rounds=5)
    out = _act(node, {"messages": [HumanMessage(content="x")], "tool_rounds": 2})
    assert out["tool_rounds"] == 3


def test_act_stops_without_tool_calls_when_budget_exhausted() -> None:
    """这是路由能无条件信任 tool_calls 的前提：超预算的 act 不产出 tool_calls。"""
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=3)
    out = _act(node, {"messages": [HumanMessage(content="x")], "tool_rounds": 3})

    assert llm.seen == []  # 根本没调用模型
    message = out["messages"][0]
    assert not getattr(message, "tool_calls", None)
    assert message.content == BUDGET_EXHAUSTED_TEMPLATE.format(limit=3)
    assert "tool_rounds" not in out


# ---------------- tools ----------------

class _FakeTool:
    """返回真实工具同款的封装格式，这样 artifact 链路也被覆盖到。"""

    def __init__(self, name: str, result: str = "done", *, ok: bool = True) -> None:
        self.name = name
        self.result = result
        self.ok = ok
        self.seen: dict = {}

    def invoke(self, args, config=None):  # noqa: ANN001
        self.seen = args
        return pack(
            self.result,
            ShellArtifact(command=str(args.get("command", "")), ok=self.ok),
        )


class _BoomTool:
    name = "boom"

    def invoke(self, args, config=None):  # noqa: ANN001
        raise RuntimeError("kaboom")


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _approved_state(message: AIMessage, **extra) -> dict:
    """构造「审批已放行」的状态：call_id -> auto。"""
    calls = getattr(message, "tool_calls", None) or []
    approvals = {str(c.get("id", "")): "auto" for c in calls}
    return {"messages": [message], "cwd": "/home/u/ws", "approvals": approvals, **extra}


def test_tools_injects_state_cwd_when_model_omits_it() -> None:
    tool = _FakeTool("shell_exec")
    node = make_tools_node([tool])
    message = _tool_call("shell_exec", {"command": "ls", "reason": "r"})
    node(_approved_state(message), None)
    assert tool.seen["cwd"] == "/home/u/ws"


def test_tools_keeps_explicit_cwd() -> None:
    tool = _FakeTool("shell_exec")
    node = make_tools_node([tool])
    message = _tool_call("shell_exec", {"command": "ls", "reason": "r", "cwd": "/tmp"})
    node(_approved_state(message), None)
    assert tool.seen["cwd"] == "/tmp"


def test_tools_does_not_inject_cwd_for_other_tools() -> None:
    tool = _FakeTool("file_read")
    node = make_tools_node([tool])
    message = _tool_call("file_read", {"path": "a.py"})
    node(_approved_state(message), None)
    assert tool.seen == {"path": "a.py"}


def test_unknown_tool_is_reported_to_model() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    out = node(_approved_state(_tool_call("nope", {})), None)
    assert "不存在名为 nope 的工具" in out["messages"][0].content


def test_tool_exception_is_captured_not_raised() -> None:
    node = make_tools_node([_BoomTool()])
    out = node(_approved_state(_tool_call("boom", {})), None)
    assert "工具执行异常：RuntimeError: kaboom" in out["messages"][0].content


def test_tool_results_are_paired_with_call_ids() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    out = node(_approved_state(_tool_call("shell_exec", {}, call_id="abc123")), None)
    assert isinstance(out["messages"][0], ToolMessage)
    assert out["messages"][0].tool_call_id == "abc123"


# ---------------- 安全强制（tools 节点是唯一咽喉） ----------------

class _RecordingTool(_FakeTool):
    """记录是否被调用过，用来证明被拒的调用真的没执行。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.invoked = False

    def invoke(self, args, config=None):  # noqa: ANN001
        self.invoked = True
        return super().invoke(args, config)


def test_denied_call_is_not_executed() -> None:
    tool = _RecordingTool("shell_exec")
    node = make_tools_node([tool])
    message = _tool_call("shell_exec", {"command": "rm -rf /", "reason": "r"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "denied"}}, None)

    assert tool.invoked is False
    assert "用户拒绝了这条命令" in out["messages"][0].content


def test_missing_approval_fails_closed() -> None:
    """没有审批记录的调用一律不执行 —— 编排出问题时必须往安全侧倒。"""
    tool = _RecordingTool("shell_exec")
    node = make_tools_node([tool])
    out = node({"messages": [_tool_call("shell_exec", {"command": "ls"}, call_id="c1")]}, None)

    assert tool.invoked is False
    assert "没有获得审批记录" in out["messages"][0].content


def test_approved_call_is_executed() -> None:
    tool = _RecordingTool("shell_exec")
    node = make_tools_node([tool])
    message = _tool_call("shell_exec", {"command": "pip install x", "reason": "r"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "approved"}}, None)

    assert tool.invoked is True
    assert out["messages"][0].artifact["decision"] == "approved"


def test_approvals_are_consumed_after_use() -> None:
    """审批结果一次性有效，避免跨轮次误放行。"""
    node = make_tools_node([_FakeTool("shell_exec")])
    out = node(_approved_state(_tool_call("shell_exec", {}, call_id="c1")), None)
    assert out["approvals"] == {}


def test_denied_call_artifact_marks_decision() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "pip install x"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "denied"}}, None)

    artifact = out["messages"][0].artifact
    assert artifact["rejected"] is True
    assert artifact["decision"] == "denied"


def test_denied_non_file_tool_is_not_labeled_as_a_file_tool() -> None:
    """git_commit / deps_install / run_tests 既没有命令也没有路径，
    被拒时不该产出 FileArtifact —— 那会让事件层标成「文件工具」。"""
    node = make_tools_node([_FakeTool("git_commit")])
    message = _tool_call("git_commit", {"message": "x"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "denied"}}, None)

    artifact = out["messages"][0].artifact
    assert artifact["kind"] == "call"
    assert artifact["rejected"] is True
    assert artifact["level_label"] == "L2 变更性"


def test_denied_file_tool_still_gets_a_file_artifact() -> None:
    node = make_tools_node([_FakeTool("file_edit")])
    message = _tool_call("file_edit", {"path": "a.py"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "denied"}}, None)
    assert out["messages"][0].artifact["kind"] == "file"


# ---------------- dirty：变更必须触发验证 ----------------

def test_named_mutation_tool_marks_dirty() -> None:
    node = make_tools_node([_FakeTool("file_edit")])
    message = _tool_call("file_edit", {"path": "a.py"})
    out = node(_approved_state(message), None)
    assert out["dirty"] is True


def test_shell_mutation_marks_dirty() -> None:
    """经 shell 的变更（sed -i 等）也必须置 dirty，否则 verify 会被跳过。"""
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "sed -i 's/a/b/' f.py", "reason": "r"})
    out = node(_approved_state(message), None)
    assert out["dirty"] is True


def test_shell_git_add_marks_dirty() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "git add src/", "reason": "r"})
    out = node(_approved_state(message), None)
    assert out["dirty"] is True


def test_failed_shell_mutation_still_marks_dirty() -> None:
    """改了东西但非零退出，仍然要验证。

    典型的：sed -i 改完了后面的命令才失败、gcc 出了产物才编译报错。
    「工作区被改动了」与「这次调用成功了」是两件事，后者不该否决前者 ——
    否则这类失败恰好跳过验证，是最需要验证的时刻。
    """
    tool = _FakeTool("shell_exec", ok=False)
    node = make_tools_node([tool])
    message = _tool_call("shell_exec", {"command": "sed -i 's/a/b/' f.py && false", "reason": "r"})
    out = node(_approved_state(message), None)

    assert out["dirty"] is True
    # 提示同样要给出：改动确实发生了，而且没有留底
    assert "没有快照留底" in out["messages"][0].content


def test_failed_file_edit_does_not_mark_dirty() -> None:
    """文件工具的 ok=False 就是没写成（路径越界、替换没命中），不该触发验证。"""
    tool = _FakeTool("file_edit", ok=False)
    node = make_tools_node([tool])
    out = node(_approved_state(_tool_call("file_edit", {"path": "a.py"})), None)

    assert out["dirty"] is False


def test_shell_read_does_not_mark_dirty() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "ls -la", "reason": "r"})
    out = node(_approved_state(message), None)
    assert out["dirty"] is False


def test_denied_shell_mutation_does_not_mark_dirty() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "sed -i 's/a/b/' f.py"}, call_id="c1")
    out = node({"messages": [message], "approvals": {"c1": "denied"}}, None)
    assert out["dirty"] is False


def test_shell_mutation_warns_about_missing_snapshot() -> None:
    """经 shell 的改动没有留底、无法 file_restore —— 要提示模型改用文件工具。"""
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "sed -i 's/a/b/' f.py", "reason": "r"})
    out = node(_approved_state(message), None)
    assert "没有快照留底" in out["messages"][0].content


def test_shell_read_has_no_snapshot_warning() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    message = _tool_call("shell_exec", {"command": "ls -la", "reason": "r"})
    out = node(_approved_state(message), None)
    assert "没有快照留底" not in out["messages"][0].content


def test_file_edit_has_no_snapshot_warning() -> None:
    """文件工具本身就有快照，不该出现这条提示。"""
    node = make_tools_node([_FakeTool("file_edit")])
    out = node(_approved_state(_tool_call("file_edit", {"path": "a.py"})), None)
    assert "没有快照留底" not in out["messages"][0].content


# ---------------- compose_system_prompt ----------------

@pytest.mark.parametrize("allow_write", [True, False])
def test_compose_without_plan_is_just_base_plus_permission(allow_write: bool) -> None:
    out = compose_system_prompt(plan=None, allow_write=allow_write)
    assert "当前任务计划" not in out
    assert "会话权限" in out


def test_compose_states_readonly_permission() -> None:
    assert "L0 只读" in compose_system_prompt(allow_write=False)
    assert "L1 低风险写" in compose_system_prompt(allow_write=True)


def test_compose_with_empty_memories_omits_section() -> None:
    assert "长期记忆" not in compose_system_prompt(memories=[])
    assert "长期记忆" not in compose_system_prompt(memories=None)
