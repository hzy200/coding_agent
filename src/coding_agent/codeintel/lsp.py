"""最小 LSP 客户端（JSON-RPC over stdio）。

只依赖标准库，用于把可选的 language server（pylsp / pyright 等）接进
`symbol_search`。没有配置语言服务器时，工具继续使用 AST 索引。

命令由用户显式配置，例如：

    AGENT_LSP_COMMAND=pylsp
    AGENT_LSP_COMMAND=wsl.exe -d Ubuntu -- pylsp

客户端只做 initialize / workspace-symbol / shutdown；真正的语义能力取决于
所选 language server。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any
from urllib.parse import unquote, urlparse

from coding_agent.codeintel.symbols import Symbol

_KIND_NAMES = {
    5: "class",
    6: "method",
    9: "constructor",
    12: "function",
    13: "variable",
    14: "constant",
}


class LspError(RuntimeError):
    pass


def path_to_uri(path: str) -> str:
    normalized = str(path).replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return "file://" + normalized


def uri_to_path(uri: str) -> str:
    parsed = urlparse(str(uri))
    if parsed.scheme and parsed.scheme != "file":
        return str(uri)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"//{parsed.netloc}{path}"
    return path


def _locations(result: Any) -> list[tuple[str, int, int]]:
    """把 definition/references 的返回值统一成 (path, line, column)。"""
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    locations: list[tuple[str, int, int]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        if not isinstance(uri, str):
            continue
        line_range = item.get("range") or item.get("targetRange") or {}
        start = line_range.get("start") or {}
        locations.append(
            (
                uri_to_path(uri),
                int(start.get("line", 0)) + 1,
                int(start.get("character", 0)) + 1,
            )
        )
    return locations


class LspClient:
    def __init__(
        self,
        command: list[str],
        *,
        root: str,
        timeout: float = 10.0,
    ) -> None:
        if not command:
            raise LspError("LSP 命令为空")
        self.root = root
        self.timeout = timeout
        self._next_id = 0
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._opened: set[str] = set()
        self._version = 0
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_events: dict[str, threading.Event] = {}
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise LspError(f"无法启动 LSP：{exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name="lsp-reader", daemon=True
        )
        self._reader.start()

    # ---------------- 生命周期 ----------------

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        if not self.alive():
            return
        try:
            self._request("shutdown", None, timeout=min(self.timeout, 2.0))
            self._notify("exit", None)
        except LspError:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "processId": None,
                "rootUri": path_to_uri(self.root),
                "capabilities": {},
            },
        )
        self._notify("initialized", {})
        return result if isinstance(result, dict) else {}

    # ---------------- 查询 ----------------

    def workspace_symbol(self, query: str) -> list[Symbol]:
        raw = self._request("workspace/symbol", {"query": query})
        if not isinstance(raw, list):
            return []
        symbols: list[Symbol] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            location = item.get("location") or {}
            if not isinstance(location, dict):
                continue
            uri = location.get("uri") or location.get("targetUri")
            if not isinstance(uri, str):
                continue
            line_range = location.get("range") or {
                "start": {"line": 0}
            }
            start = line_range.get("start") or {}
            line = int(start.get("line", 0)) + 1
            name = str(item.get("name", ""))
            kind = _KIND_NAMES.get(int(item.get("kind", 0) or 0), "symbol")
            symbols.append(
                Symbol(name=name, kind=kind, path=uri_to_path(uri), line=line)
            )
        return symbols

    def did_open(self, path: str, text: str) -> None:
        """打开/刷新文档；definition/references/diagnostics 之前必须调用。"""
        uri = path_to_uri(path)
        with self._state_lock:
            self._version += 1
            version = self._version
            already = uri in self._opened
            self._opened.add(uri)
        if already:
            self._notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        else:
            language = "python" if uri.endswith(".py") else "plaintext"
            self._notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language,
                        "version": version,
                        "text": text,
                    }
                },
            )

    def definition(
        self, path: str, line: int, character: int
    ) -> list[tuple[str, int, int]]:
        result = self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": path_to_uri(path)},
                "position": {
                    "line": max(0, line - 1),
                    "character": max(0, character - 1),
                },
            },
        )
        return _locations(result)

    def references(
        self,
        path: str,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list[tuple[str, int, int]]:
        result = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": path_to_uri(path)},
                "position": {
                    "line": max(0, line - 1),
                    "character": max(0, character - 1),
                },
                "context": {
                    "includeDeclaration": include_declaration,
                },
            },
        )
        return _locations(result)

    def diagnostics(
        self, path: str, *, wait: float = 2.0
    ) -> list[dict[str, Any]]:
        """获取诊断：优先 LSP 3.17 pull，失败/为空时等待 publish 推送。

        类型检查器（pyright/mypy）通常同时支持 push；pull 能避免固定等待，
        但并非所有服务器都实现，因此两条路径都要保留。
        """
        uri = path_to_uri(path)
        with self._state_lock:
            event = self._diag_events.setdefault(uri, threading.Event())
            event.clear()
        try:
            result = self._request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": uri}},
            )
        except LspError:
            result = None
        if isinstance(result, dict):
            items = result.get("items")
            if isinstance(items, list):
                with self._state_lock:
                    self._diagnostics[uri] = list(items)
                if items:
                    return list(items)
        # pull 不支持或返回空时，可能已经有 push 结果；先取现有结果。
        with self._state_lock:
            existing = list(self._diagnostics.get(uri) or [])
        if existing:
            return existing
        event.wait(timeout=max(0.0, wait))
        with self._state_lock:
            return list(self._diagnostics.get(uri) or [])

    # ---------------- JSON-RPC ----------------

    def _notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self, method: str, params: Any, *, timeout: float | None = None
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = timeout if timeout is not None else self.timeout
        while True:
            message = self._read_message(deadline)
            if message is None:
                raise LspError("LSP 进程已退出")
            if message.get("id") != request_id:
                # 通知、日志、服务端请求都与当前请求无关；忽略。
                continue
            if "error" in message:
                raise LspError(str(message["error"]))
            return message.get("result")

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(header + data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LspError(f"LSP 写入失败：{exc}") from exc

    def _read_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self._messages.get(timeout=max(0.01, timeout))
        except queue.Empty as exc:
            raise LspError("LSP 响应超时") from exc

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        self._messages.put(None)
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    key, _, value = line.decode("ascii", errors="replace").partition(":")
                    headers[key.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                body = stream.read(length)
                if len(body) != length:
                    self._messages.put(None)
                    return
                try:
                    message = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(message, dict)
                    and "id" not in message
                    and message.get("method")
                    == "textDocument/publishDiagnostics"
                ):
                    params = message.get("params") or {}
                    uri = str(params.get("uri", ""))
                    if uri:
                        with self._state_lock:
                            self._diagnostics[uri] = list(
                                params.get("diagnostics") or []
                            )
                            event = self._diag_events.get(uri)
                        if event is not None:
                            event.set()
                    continue
                self._messages.put(message)
        except (OSError, ValueError):
            self._messages.put(None)


def command_from_settings(command: str) -> list[str]:
    """把 `AGENT_LSP_COMMAND` 拆成 argv；空串表示未配置。"""
    import shlex

    return shlex.split(str(command or ""), posix=True)
