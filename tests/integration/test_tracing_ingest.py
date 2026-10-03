"""追踪链路：把 LangSmith 端点指向本地 mock，验证我们这侧确实把 trace 发出去了。

为什么要这样测：真实凭据不可用时（403），"追踪没数据"既可能是我们没发，
也可能是服务端不收。把端点指向本地就能把两件事分开 —— 这里只验证**我们这侧**：
trace 有没有被发出、run_name / tags / metadata 有没有带上。

不需要 API Key，也不需要网络。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from coding_agent.config import Settings
from coding_agent.runtime import AgentRuntime

pytestmark = pytest.mark.wsl


class _IngestRecorder(BaseHTTPRequestHandler):
    """接住 langsmith 的 ingestion 请求，把原始报文留下来。"""

    captured: list[bytes] = []
    paths: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).captured.append(body)
        type(self).paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self) -> None:  # noqa: N802
        type(self).paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args) -> None:
        pass  # 别往测试输出里灌 HTTP 日志


def _bypass_proxy(monkeypatch) -> None:
    """这台机器上配了系统代理，不加这个的话本地 mock 会被代理拦掉。"""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")


def _reset_langsmith_state() -> None:
    """把 langsmith 的进程级缓存清干净。

    两处都是**进程级**的，跨测试会互相污染：

    1. `run_trees._CLIENT` 是单例，端点被钉在第一次构造时读到的值上 ——
       不重置的话后续测试还在往上一个已关闭的 mock 端口发，表现为「连接被拒绝」。
    2. `langsmith.utils.get_env_var` 是 `lru_cache` —— 只要前面有任何代码
       （例如 `Settings.apply_tracing_env()` 写入 `LANGSMITH_TRACING=false`）
       让这个值被读过一次，后面的 `monkeypatch.setenv(..., "true")` 就再也读不到，
       表现为「追踪静默地什么都不发」。
    """
    import langsmith.run_trees as run_trees
    import langsmith.utils as ls_utils

    run_trees._CLIENT = None  # noqa: SLF001
    for cached in (ls_utils.get_env_var, ls_utils.get_tracer_project):
        cache_clear = getattr(cached, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


@pytest.fixture
def mock_langsmith(monkeypatch):
    _IngestRecorder.captured = []
    _IngestRecorder.paths = []
    server = HTTPServer(("127.0.0.1", 0), _IngestRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    _bypass_proxy(monkeypatch)
    _reset_langsmith_state()
    monkeypatch.setenv("LANGSMITH_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test_key_for_local_mock")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    yield _IngestRecorder

    server.shutdown()
    server.server_close()


def _flush_tracers() -> None:
    """ingestion 是后台批量上报的，要等它把 buffer 刷出去。"""
    from langchain_core.tracers.langchain import wait_for_all_tracers

    wait_for_all_tracers()


def _captured_text() -> str:
    return b"\n".join(_IngestRecorder.captured).decode("utf-8", errors="replace")


# ---------------- 基础连通：trace 真的发出去了 ----------------

def test_traces_are_ingested(mock_langsmith) -> None:
    chain = RunnableLambda(lambda x: x + 1)
    chain.invoke(1, {"run_name": "probe-chain", "tags": ["probe"]})
    _flush_tracers()

    assert mock_langsmith.captured, "没有向 LangSmith 端点发出任何 ingestion 请求"
    assert any("runs" in path for path in mock_langsmith.paths)


def test_run_name_and_tags_are_carried(mock_langsmith) -> None:
    chain = RunnableLambda(lambda x: x * 2)
    chain.invoke(2, {"run_name": "probe-named-run", "tags": ["coding-agent", "mode:ask"]})
    _flush_tracers()

    text = _captured_text()
    assert "probe-named-run" in text
    assert "coding-agent" in text


# ---------------- 运行时的元数据确实挂到了图上 ----------------

def test_agent_run_config_metadata_reaches_the_tracer(mock_langsmith, tmp_path) -> None:
    """用真实 AgentRuntime 构造的 config 跑一张无 LLM 的图，验证元数据随 trace 上报。"""
    runtime = AgentRuntime(
        Settings(_env_file=None, audit_dir=str(tmp_path)),
        workspace="/mnt/d/proj",
    )
    thread_id = "trace-probe-1"

    class _State(dict):
        pass

    from typing import TypedDict

    class S(TypedDict, total=False):
        n: int

    graph = StateGraph(S)
    graph.add_node("bump", lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "bump")
    graph.add_edge("bump", END)
    compiled = graph.compile()

    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": f"agent:{thread_id}",
        "tags": ["coding-agent", f"mode:{runtime.policy.approval_mode}"],
        "metadata": runtime._trace_metadata(thread_id),
    }
    compiled.invoke({"n": 0}, config)
    _flush_tracers()

    text = _captured_text()
    assert f"agent:{thread_id}" in text
    assert thread_id in text
    assert "/mnt/d/proj" in text            # workspace
    assert '"allow_write"' in text.replace(" ", "") or "allow_write" in text
    assert "approval_mode" in text


def test_metadata_is_json_serializable(mock_langsmith) -> None:
    """元数据要能被 ingestion 序列化，否则上报时才会炸。"""
    runtime = AgentRuntime(Settings(_env_file=None), workspace="/mnt/d/proj")
    meta = runtime._trace_metadata("t")
    assert json.loads(json.dumps(meta)) == meta


def test_tracing_disabled_sends_nothing(monkeypatch, tmp_path) -> None:
    """默认关闭时一个字都不该发出去。"""
    _IngestRecorder.captured = []
    _IngestRecorder.paths = []
    server = HTTPServer(("127.0.0.1", 0), _IngestRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _bypass_proxy(monkeypatch)
    _reset_langsmith_state()

    settings = Settings(_env_file=None, langsmith_tracing=False, audit_dir=str(tmp_path))
    settings.apply_tracing_env()
    assert os.environ["LANGSMITH_TRACING"] == "false"

    RunnableLambda(lambda x: x).invoke(1)
    _flush_tracers()

    server.shutdown()
    server.server_close()
    assert _IngestRecorder.captured == []
