from __future__ import annotations

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
        "retry": 0,
    }


# ---------------- act ----------------

def test_act_injects_plan_and_marks_current_step() -> None:
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5)
    node(
        {
            "messages": [HumanMessage(content="do it")],
            "plan": ["first", "second"],
            "step_idx": 1,
            "tool_rounds": 0,
        },
        None,
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
    node(
        {
            "messages": [HumanMessage(content="do it")],
            "memories": ["这个仓库用 pytest", "别动 legacy/"],
            "tool_rounds": 0,
        },
        None,
    )
    system = llm.seen[0].content
    assert "这个仓库用 pytest" in system
    assert "别动 legacy/" in system
    assert "长期记忆" in system


def test_act_without_memories_has_no_memory_section() -> None:
    llm = _EchoLLM()
    make_act_node(llm, max_tool_rounds=5)(
        {"messages": [HumanMessage(content="x")], "tool_rounds": 0}, None
    )
    assert "长期记忆" not in llm.seen[0].content


def test_act_injects_verification_feedback_on_retry() -> None:
    """修复轮次里，结构化错误必须进系统提示 —— 否则模型在盲改。"""
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3)
    node(
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
        None,
    )
    system = llm.seen[0].content
    assert "calc.py:2" in system
    assert "第 1/3 次" in system


def test_act_has_no_feedback_on_the_first_attempt() -> None:
    llm = _EchoLLM()
    make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3)(
        {"messages": [HumanMessage(content="开始")], "tool_rounds": 0, "retry": 0}, None
    )
    assert "次修复" not in llm.seen[0].content


def test_act_ignores_stale_passing_verification() -> None:
    llm = _EchoLLM()
    make_act_node(llm, max_tool_rounds=5, max_repair_rounds=3)(
        {
            "messages": [HumanMessage(content="继续")],
            "tool_rounds": 0,
            "retry": 1,
            "verification": {"status": "ok"},
        },
        None,
    )
    assert "次修复" not in llm.seen[0].content


def test_act_counts_rounds() -> None:
    node = make_act_node(_EchoLLM(), max_tool_rounds=5)
    out = node({"messages": [HumanMessage(content="x")], "tool_rounds": 2}, None)
    assert out["tool_rounds"] == 3


def test_act_stops_without_tool_calls_when_budget_exhausted() -> None:
    """这是路由能无条件信任 tool_calls 的前提：超预算的 act 不产出 tool_calls。"""
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=3)
    out = node({"messages": [HumanMessage(content="x")], "tool_rounds": 3}, None)

    assert llm.seen == []  # 根本没调用模型
    message = out["messages"][0]
    assert not getattr(message, "tool_calls", None)
    assert message.content == BUDGET_EXHAUSTED_TEMPLATE.format(limit=3)
    assert "tool_rounds" not in out


# ---------------- tools ----------------

class _FakeTool:
    """返回真实工具同款的封装格式，这样 artifact 链路也被覆盖到。"""

    def __init__(self, name: str, result: str = "done") -> None:
        self.name = name
        self.result = result
        self.seen: dict = {}

    def invoke(self, args, config=None):  # noqa: ANN001
        self.seen = args
        return pack(self.result, ShellArtifact(command=str(args.get("command", "")), ok=True))


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
