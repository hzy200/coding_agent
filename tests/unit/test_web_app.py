"""Web 最小验证界面。

用 FastAPI 的 TestClient 打真实端点，只把 AgentRuntime 换成假的 ——
LLM 与沙箱不参与，测的是「事件流 → SSE 帧」这条链路和端点形状。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.events import (
    AssistantToken,
    PlanCreated,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from coding_agent.runtime import HistoryMessage

SCRIPT = [
    RunStarted(thread_id="t"),
    PlanCreated(steps=["看目录"]),
    AssistantToken(node="act", text="先看看"),
    ToolCallStarted(name="shell_exec", args={}, summary="ls", level="L0 只读"),
    ToolCallFinished(name="shell_exec", ok=True, exit_code=0, duration_ms=10),
    AssistantToken(node="respond", text="目录是空的"),
    RunFinished(thread_id="t", answer="目录是空的"),
]


class FakeRuntime:
    """按脚本回放事件；可切换成「抛异常」来验证错误也走事件流。"""

    def __init__(self, *, events=None, error: Exception | None = None,
                 history=None, workspace: str = "/mnt/d/proj") -> None:
        self._events = events if events is not None else SCRIPT
        self._error = error
        self._history = history or []
        self.workspace = workspace
        self.ran: list[tuple[str, str]] = []
        self.closed = False

    async def run(self, prompt: str, *, thread_id: str):
        self.ran.append((prompt, thread_id))
        if self._error is not None:
            raise self._error
        for event in self._events:
            yield event

    async def history(self, thread_id: str):
        return self._history

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
    )


def _client(monkeypatch, settings: Settings, runtime: object | None = None) -> TestClient:
    """路由里是现建 AgentRuntime 的，这里把工厂整体换掉。

    用 monkeypatch 而不是直接赋值模块全局 —— 后者会跨用例泄漏。
    """
    runtime = runtime or FakeRuntime()
    monkeypatch.setattr(web.app, "AgentRuntime", lambda *a, **k: runtime)
    return TestClient(web.create_app(settings))


def _frames(text: str) -> list[dict]:
    out = []
    for frame in text.split("\n\n"):
        payload = frame.removeprefix("data: ").strip()
        if payload:
            out.append(json.loads(payload))
    return out


# ---------------- 页面与联通测试 ----------------

def test_index_page_is_served(settings) -> None:
    resp = TestClient(web.create_app(settings)).get("/")
    assert resp.status_code == 200
    assert "CodingAgent" in resp.text
    assert "text/event-stream" not in resp.text  # 是页面不是流


def test_index_has_no_build_chain(settings) -> None:
    """刻意不引前端构建链：页面必须自包含。"""
    html = TestClient(web.create_app(settings)).get("/").text
    assert "<script src=" not in html
    assert "import " not in html.split("<script>")[0]


def test_health_reports_configuration(settings) -> None:
    data = TestClient(web.create_app(settings)).get("/api/health").json()
    assert data["api_key"] is True
    assert data["reachable"] is True
    assert data["model"] == settings.deepseek_model
    assert "tracing" in data


def test_health_without_api_key(settings) -> None:
    bare = settings.model_copy(update={"deepseek_api_key": ""})
    data = TestClient(web.create_app(bare)).get("/api/health").json()
    assert data["reachable"] is False
    assert "DEEPSEEK_API_KEY" in data["error"]


def test_health_survives_broken_sandbox(monkeypatch, settings) -> None:
    """沙箱不可用时也要给出可读的错误，而不是 500。"""
    class _Broken:
        def __init__(self, *a, **k) -> None:
            pass

        @property
        def workspace(self) -> str:
            raise RuntimeError("WSL 不可用")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(web.app, "AgentRuntime", _Broken)
    data = TestClient(web.create_app(settings)).get("/api/health").json()
    assert data["reachable"] is True
    assert "WSL 不可用" in data["error"]
    assert data["workspace"] is None


# ---------------- 会话列表 ----------------

def test_sessions_are_listed(settings, tmp_path) -> None:
    from coding_agent.audit import AuditLogger, AuditRecord

    logger = AuditLogger(settings.resolved_audit_dir)
    logger.write(AuditRecord(ts="2026-10-03T10:00:00+00:00", kind="run_start",
                             thread_id="abc", detail="看看目录"))
    logger.write(AuditRecord(ts="2026-10-03T10:00:01+00:00", kind="tool_call",
                             thread_id="abc", tool="shell_exec", decision="auto", ok=True))

    data = TestClient(web.create_app(settings)).get("/api/sessions").json()
    assert len(data["sessions"]) == 1
    row = data["sessions"][0]
    assert row["thread_id"] == "abc"
    assert row["title"] == "看看目录"
    assert row["prompts"] == 1
    assert row["tool_calls"] == 1


def test_sessions_empty(settings) -> None:
    assert TestClient(web.create_app(settings)).get("/api/sessions").json() == {"sessions": []}


def test_sessions_respects_limit(settings, monkeypatch) -> None:
    from coding_agent.audit import AuditLogger, AuditRecord

    logger = AuditLogger(settings.resolved_audit_dir)
    for i in range(5):
        logger.write(AuditRecord(ts=f"2026-10-0{i + 1}T10:00:00+00:00", kind="run_start",
                                 thread_id=f"t{i}", detail=f"会话 {i}"))

    data = TestClient(web.create_app(settings)).get("/api/sessions?limit=2").json()
    assert [s["thread_id"] for s in data["sessions"]] == ["t4", "t3"]


# ---------------- 流式执行 ----------------

def test_run_streams_domain_events(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    resp = client.post("/api/run", json={"prompt": "看看目录"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    kinds = [f["type"] for f in _frames(resp.text)]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert {"plan_created", "assistant_token", "tool_call_started"} <= set(kinds)


def test_run_assigns_a_thread_id_when_missing(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    resp = client.post("/api/run", json={"prompt": "hi"})
    assert resp.headers["x-thread-id"]
    assert len(resp.headers["x-thread-id"]) == 8


def test_run_reuses_given_thread_id(monkeypatch, settings) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(web.app, "AgentRuntime", lambda *a, **k: runtime)

    TestClient(web.create_app(settings)).post(
        "/api/run", json={"prompt": "接着聊", "thread_id": "t-9"}
    )
    assert runtime.ran == [("接着聊", "t-9")]


def test_run_errors_become_events_not_500(monkeypatch, settings) -> None:
    """运行期异常要以 run_failed 事件送到前端，而不是断掉连接。"""
    client = _client(monkeypatch, settings, FakeRuntime(error=RuntimeError("模型炸了")))
    resp = client.post("/api/run", json={"prompt": "hi"})

    assert resp.status_code == 200
    kinds = [f["type"] for f in _frames(resp.text)]
    assert "run_failed" in kinds
    assert "模型炸了" in next(f for f in _frames(resp.text) if f["type"] == "run_failed")["message"]


def test_run_closes_the_runtime(monkeypatch, settings) -> None:
    """每次请求用完就释放 sqlite 连接，否则跑久了会攒住句柄。"""
    runtime = FakeRuntime()
    monkeypatch.setattr(web.app, "AgentRuntime", lambda *a, **k: runtime)

    TestClient(web.create_app(settings)).post("/api/run", json={"prompt": "hi"})
    assert runtime.closed is True


def test_run_rejects_empty_prompt(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    assert client.post("/api/run", json={"prompt": ""}).status_code == 422


def test_run_denies_mutations_by_default(monkeypatch, settings) -> None:
    """Web 没有审批交互，所以必须固定拒绝变更类命令（fail closed）。"""
    seen: list[str] = []

    def _capture(*args, **kwargs):
        seen.append(kwargs.get("approval_mode", ""))
        return FakeRuntime()

    monkeypatch.setattr(web.app, "AgentRuntime", _capture)
    TestClient(web.create_app(settings)).post("/api/run", json={"prompt": "hi"})

    assert seen == [web.app.WEB_APPROVAL_MODE]
    assert web.app.WEB_APPROVAL_MODE == "deny"


# ---------------- 历史 ----------------

def test_thread_history(monkeypatch, settings) -> None:
    runtime = FakeRuntime(history=[
        HistoryMessage("user", "之前问的"),
        HistoryMessage("assistant", "之前的回答"),
    ])
    monkeypatch.setattr(web.app, "AgentRuntime", lambda *a, **k: runtime)

    data = TestClient(web.create_app(settings)).get("/api/thread/abc").json()
    assert data["messages"] == [
        {"role": "user", "text": "之前问的"},
        {"role": "assistant", "text": "之前的回答"},
    ]


def test_thread_history_empty(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    assert client.get("/api/thread/none").json() == {"messages": []}


def test_thread_history_reports_unavailable(monkeypatch, settings) -> None:
    """历史拿不到要给 503 与原因，而不是空列表让前端以为没记录。"""

    class _Broken(FakeRuntime):
        async def history(self, thread_id: str):
            raise RuntimeError("checkpoint 打不开")

    monkeypatch.setattr(web.app, "AgentRuntime", lambda *a, **k: _Broken())

    resp = TestClient(web.create_app(settings)).get("/api/thread/abc")
    assert resp.status_code == 503
    assert "checkpoint 打不开" in resp.json()["detail"]
