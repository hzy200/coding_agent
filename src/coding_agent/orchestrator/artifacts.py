"""fan-in artifact store。

`worker_context` 是给模型看的压缩文本；完整结果落盘，供主 agent 或用户
按需追溯。路径固定在 `.agent/artifacts/<run>/<task>/attempt-N/result.json`，
不会随会话/重试互相覆盖。

落盘失败**不能拖垮运行**：artifact 是诊断能力，不是正确性前提。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox

ARTIFACT_DIRNAME = ".agent/artifacts"
MAX_ARTIFACT_CHARS = 200_000


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:40]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{text}-{digest}" if text else digest


def make_run_id(thread_id: str, started_at: str) -> str:
    return _safe_name(f"{thread_id or 'run'}-{started_at}")


def _compact(value: Any, *, text_limit: int, list_limit: int) -> Any:
    """按字段截断，保证截断后的 artifact 仍是合法 JSON。

    直接切 JSON 字符串会在任意位置断开引号/括号，artifact 反而不可读。
    """
    if isinstance(value, str):
        if len(value) <= text_limit:
            return value
        return value[:text_limit] + f"…（已截断，原长 {len(value)}）"
    if isinstance(value, list):
        items = [
            _compact(item, text_limit=text_limit, list_limit=list_limit)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            items.append(f"…（其余 {len(value) - list_limit} 项已省略）")
        return items
    if isinstance(value, dict):
        return {
            str(key): _compact(
                item, text_limit=text_limit, list_limit=list_limit
            )
            for key, item in value.items()
        }
    return value


class ArtifactStore:
    def __init__(self, sandbox: WslSandbox, root: str, run_id: str) -> None:
        self._fs = SandboxFs(sandbox, root)
        self._root = root
        self._run = _safe_name(run_id)

    def save_result(self, result: dict[str, Any]) -> str:
        """写一条 WorkerResult；返回相对工作区根的 artifact 路径。"""
        task = _safe_name(str(result.get("task_id", "task")))
        attempt = max(1, int(result.get("attempt", 1) or 1))
        rel = posixpath.join(
            ARTIFACT_DIRNAME,
            self._run,
            task,
            f"attempt-{attempt}",
            "result.json",
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if len(payload) > MAX_ARTIFACT_CHARS:
            compacted = _compact(
                result, text_limit=4_000, list_limit=40
            )
            payload = json.dumps(
                {
                    "truncated": True,
                    "note": f"artifact 超过 {MAX_ARTIFACT_CHARS} 字符，已按字段截断",
                    "result": compacted,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        if len(payload) > MAX_ARTIFACT_CHARS:
            compacted = _compact(result, text_limit=800, list_limit=10)
            payload = json.dumps(
                {
                    "truncated": True,
                    "note": "artifact 超限，已再次压缩字段",
                    "result": compacted,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        try:
            self._fs.write_text(posixpath.join(self._root, rel), payload)
        except (SandboxFsError, SandboxPathError):
            return ""
        return rel
