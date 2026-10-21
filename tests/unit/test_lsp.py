"""最小 LSP 客户端：initialize / workspace-symbol / shutdown。"""

from __future__ import annotations

import sys

from coding_agent.codeintel.lsp import (
    LspClient,
    command_from_settings,
    path_to_uri,
    uri_to_path,
)
from coding_agent.config import Settings
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.code_intel import build_code_intel_tool

FAKE_SERVER = r'''
import json, sys

PULL = "--no-pull" not in sys.argv

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

def send(payload):
    data = json.dumps(payload).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(data)}\r\n\r\n".encode() + data
    )
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "workspace/symbol":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": [{
                "name": "DiscountService",
                "kind": 5,
                "location": {
                    "uri": "file:///tmp/ws/app.py",
                    "range": {"start": {"line": 1}, "end": {"line": 2}},
                },
            }],
        })
    elif method == "textDocument/didOpen":
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": "file:///tmp/ws/app.py",
                "diagnostics": [{
                    "range": {"start": {"line": 2}, "end": {"line": 2}},
                    "severity": 2,
                    "message": "unused variable",
                }],
            },
        })
    elif method in {"textDocument/definition", "textDocument/references"}:
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": [{
                "uri": "file:///tmp/ws/app.py",
                "range": {"start": {"line": 4, "character": 2}},
            }],
        })
    elif method == "textDocument/diagnostic":
        if PULL:
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "kind": "full",
                    "items": [{
                        "range": {"start": {"line": 2}, "end": {"line": 2}},
                        "severity": 1,
                        "source": "pyright",
                        "code": "reportGeneralTypeIssues",
                        "message": "type error",
                    }],
                },
            })
        else:
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "not supported"},
            })
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
'''


def test_workspace_symbol_roundtrip(tmp_path) -> None:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    client = LspClient(
        [sys.executable, str(script)], root=str(tmp_path), timeout=5
    )
    try:
        client.initialize()
        symbols = client.workspace_symbol("Discount")
    finally:
        client.close()

    assert len(symbols) == 1
    assert symbols[0].name == "DiscountService"
    assert symbols[0].kind == "class"
    assert symbols[0].path == "/tmp/ws/app.py"
    assert symbols[0].line == 2


def test_definition_references_and_diagnostics(tmp_path) -> None:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    client = LspClient(
        [sys.executable, str(script)], root=str(tmp_path), timeout=5
    )
    try:
        client.initialize()
        client.did_open("/tmp/ws/app.py", "x = 1\n")
        assert client.definition("/tmp/ws/app.py", 2, 3) == [
            ("/tmp/ws/app.py", 5, 3)
        ]
        assert client.references("/tmp/ws/app.py", 2, 3) == [
            ("/tmp/ws/app.py", 5, 3)
        ]
        diagnostics = client.diagnostics("/tmp/ws/app.py", wait=2)
    finally:
        client.close()

    assert diagnostics[0]["message"] == "type error"
    assert diagnostics[0]["severity"] == 1
    assert diagnostics[0]["source"] == "pyright"


def test_diagnostics_falls_back_to_publish(tmp_path) -> None:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    client = LspClient(
        [sys.executable, str(script), "--no-pull"],
        root=str(tmp_path),
        timeout=5,
    )
    try:
        client.initialize()
        client.did_open("/tmp/ws/app.py", "x = 1\n")
        diagnostics = client.diagnostics("/tmp/ws/app.py", wait=2)
    finally:
        client.close()

    assert diagnostics[0]["message"] == "unused variable"
    assert diagnostics[0]["severity"] == 2


def test_uri_helpers() -> None:
    assert path_to_uri("/home/u/app.py") == "file:///home/u/app.py"
    assert uri_to_path("file:///home/u/app.py") == "/home/u/app.py"


def test_command_from_settings() -> None:
    assert command_from_settings("pylsp --check") == ["pylsp", "--check"]
    assert command_from_settings("") == []


def test_code_intel_without_lsp_is_skipped_not_failed() -> None:
    settings = Settings(
        _env_file=None, deepseek_api_key="x", wsl_workspace="/ws"
    )
    tool = build_code_intel_tool(settings, object())  # type: ignore[arg-type]

    text, artifact = unpack(
        tool.invoke(
            {
                "action": "diagnostics",
                "path": "app.py",
                "reason": "检查",
            }
        )
    )

    assert "未配置" in text
    assert artifact is not None
    assert artifact["ok"] is False
    assert artifact["skipped"] is True
