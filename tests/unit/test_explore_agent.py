"""只读 Explore 子智能体的循环。

用脚本化 LLM + 假工具驱动，不碰 WSL、不碰真实模型。这里守的是：
独立上下文、只读白名单、轮次上限、超时/报告失败的降级，以及 token 累计。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.agents.explore import (
    ExploreReport,
    TaskProposal,
    make_explore_runner,
    make_review_runner,
)
from coding_agent.config import Settings
from coding_agent.tools.artifacts import CallArtifact, pack


class _FakeTool:
    def __init__(self, name: str, *, reply: str = "ok") -> None:
        self.name = name
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def invoke(self, args: dict[str, Any], config: Any = None) -> str:
        self.calls.append(dict(args))
        return pack(f"[{self.name}] {self.reply}", CallArtifact(tool=self.name, ok=True))


class _Structured:
    def __init__(
        self,
        report: ExploreReport | None = None,
        *,
        error: Exception | None = None,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> None:
        self.report = report
        self.error = error
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0

    def with_retry(self, **_kwargs: Any) -> _Structured:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "total_tokens": self.input_tokens + self.output_tokens,
                },
            ),
            "parsed": self.report,
            "parsing_error": None,
        }


class _ScriptedLLM:
    """按顺序返回 AIMessage；工具绑定与结构化输出都返回可用的假实现。"""

    def __init__(
        self,
        responses: list[Any],
        *,
        report: ExploreReport | None = None,
        report_error: Exception | None = None,
        usage: tuple[int, int] = (100, 20),
    ) -> None:
        self.responses = list(responses)
        self.report = report or ExploreReport(summary="完成")
        self.report_error = report_error
        self.usage = usage
        self.seen: list[list[Any]] = []
        self.bound: list[Any] = []
        self.structured = _Structured(self.report, error=report_error)

    def bind_tools(self, tools: list[Any]) -> _ScriptedLLM:
        self.bound = list(tools)
        return self

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _Structured:
        self.schema = schema
        return self.structured

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.seen.append(list(messages))
        if not self.responses:
            return AIMessage(content="收尾")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return item

    @staticmethod
    def tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id}],
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        )


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _runner(llm: _ScriptedLLM, tools: list[Any], **overrides: Any):
    return make_explore_runner(
        llm=llm, tools=tools, settings=_settings(explore_enabled=True, **overrides)
    )


def _run(runner, **kwargs: Any):
    return asyncio.run(
        runner(
            args=kwargs.pop("args"),
            call_id=kwargs.pop("call_id", "c1"),
            parent_request=kwargs.pop("parent_request", "父请求"),
            **kwargs,
        )
    )


def test_returns_a_structured_report() -> None:
    llm = _ScriptedLLM(
        [
            _ScriptedLLM.tool_call("file_read", {"path": "a.py", "reason": "看"}),
            AIMessage(content="看完了"),
        ],
        report=ExploreReport(
            summary="a.py 里是入口",
            relevant_files=["a.py"],
            findings=["main() 在 a.py:3"],
            open_questions=[],
            recommended_next_steps=["改 a.py"],
        ),
    )
    tool = _FakeTool("file_read")
    artifact = _run(_runner(llm, [tool]), args={"goal": "找入口"})

    assert artifact.status == "success"
    assert artifact.summary == "a.py 里是入口"
    assert artifact.relevant_files == ["a.py"]
    assert artifact.findings == ["main() 在 a.py:3"]
    assert len(artifact.tool_calls) == 1
    assert artifact.tool_calls[0]["name"] == "file_read"
    assert artifact.tool_calls[0]["ok"] is True
    assert artifact.rounds == 2
    assert tool.calls == [{"path": "a.py", "reason": "看"}]
    # 第一轮子模型（100/20）+ 第二轮无 usage + 报告调用（10/5）
    assert (artifact.input_tokens, artifact.output_tokens) == (110, 25)


def test_child_context_is_isolated() -> None:
    llm = _ScriptedLLM([AIMessage(content="完成")])
    _run(_runner(llm, [_FakeTool("file_read")]), args={"goal": "看结构"})

    first = llm.seen[0]
    assert [type(message).__name__ for message in first] == [
        "SystemMessage",
        "HumanMessage",
    ]
    # 父对话历史没有传进来；只有本次任务的目标与请求
    assert "看结构" in str(first[1].content)
    assert "父请求" in str(first[1].content)


def test_non_whitelisted_tool_call_is_refused() -> None:
    llm = _ScriptedLLM(
        [
            _ScriptedLLM.tool_call("file_write", {"path": "a.py", "reason": "写"}),
            AIMessage(content="没写成"),
        ]
    )
    artifact = _run(_runner(llm, [_FakeTool("file_read")]), args={"goal": "写文件"})

    records = [call for call in artifact.tool_calls if call["name"] == "file_write"]
    assert len(records) == 1
    assert records[0]["ok"] is False
    # 子智能体侧没有写工具，状态仍是 partial（报告生成成功但有失败调用）
    assert artifact.status == "partial"


def test_stops_at_max_rounds_and_marks_partial() -> None:
    call = _ScriptedLLM.tool_call("file_read", {"path": "a.py", "reason": "看"})
    llm = _ScriptedLLM([call, call, call])
    artifact = _run(
        _runner(llm, [_FakeTool("file_read")], explore_max_rounds=2),
        args={"goal": "一直看"},
    )

    assert artifact.rounds == 2
    assert artifact.status == "partial"
    assert len(artifact.tool_calls) == 2


def test_timeout_returns_a_failed_artifact() -> None:
    async def _slow() -> AIMessage:
        await asyncio.sleep(2)
        return AIMessage(content="太慢了")

    llm = _ScriptedLLM([_slow])
    artifact = _run(
        _runner(llm, [_FakeTool("file_read")], explore_timeout=1),
        args={"goal": "超时"},
    )

    assert artifact.status == "failed"
    assert "超时" in artifact.error
    assert artifact.tool_calls == []


def test_report_failure_falls_back_to_the_last_assistant_text() -> None:
    llm = _ScriptedLLM(
        [AIMessage(content="兜底结论")],
        report_error=RuntimeError("report boom"),
    )
    artifact = _run(_runner(llm, [_FakeTool("file_read")]), args={"goal": "兜底"})

    assert artifact.status == "partial"
    assert artifact.summary == "兜底结论"
    assert "报告生成失败" in artifact.error


def test_review_runner_uses_the_review_prompt_and_role() -> None:
    llm = _ScriptedLLM(
        [AIMessage(content="审查完成")],
        report=ExploreReport(
            summary="发现一个边界条件问题",
            findings=["[warning] a.py 缺少空输入处理"],
        ),
    )
    runner = make_review_runner(
        llm=llm,
        tools=[_FakeTool("file_read")],
        settings=_settings(explore_enabled=True),
    )
    artifact = _run(
        runner,
        args={"goal": "审查 a.py", "paths": ["a.py"], "reason": "review"},
        call_id="r1",
        parent_request="审查这个项目",
    )

    assert artifact.role == "review"
    assert artifact.agent_id == "review:r1"
    assert "审查" in str(llm.seen[0][0].content)
    assert artifact.summary == "发现一个边界条件问题"


def test_runner_refuses_non_readonly_tools_at_construction() -> None:
    llm = _ScriptedLLM([])
    with pytest.raises(ValueError, match="只读白名单"):
        make_explore_runner(
            llm=llm,
            tools=[_FakeTool("file_write")],
            settings=_settings(explore_enabled=True),
        )


def test_report_proposals_are_carried_to_the_artifact_with_a_cap() -> None:
    llm = _ScriptedLLM(
        [],
        report=ExploreReport(
            summary="完成",
            proposed_tasks=[
                TaskProposal(role="review", goal="检查边界"),
                TaskProposal(role="test", goal="跑回归"),
                TaskProposal(role="explore", goal="第三个会被截断"),
            ],
        ),
    )
    runner = _runner(llm, [_FakeTool("file_read")])

    artifact = _run(runner, args={"goal": "探索", "paths": []})

    assert len(artifact.proposed_tasks) == 2
    assert artifact.proposed_tasks[0]["role"] == "review"
    assert artifact.proposed_tasks[0]["goal"] == "检查边界"


def test_cancelled_explore_skips_the_report_call() -> None:
    """Phase 3D：取消在 LLM 轮次边界生效，不再为空结果花一次报告调用。"""
    llm = _ScriptedLLM([])
    runner = _runner(llm, [_FakeTool("file_read")])

    artifact = _run(
        runner,
        args={"goal": "探索", "paths": []},
        cancel=lambda: True,
    )

    assert artifact.status == "cancelled"
    assert "取消" in artifact.summary
    assert llm.seen == []
    assert llm.structured.calls == 0
