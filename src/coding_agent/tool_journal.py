"""有副作用工具的执行日志。

崩溃恢复的最后一个漏洞：工具已经执行（shell 改了文件、git 提交了），但
checkpoint 还没记录结果。如果同一调用被重放，副作用会发生第二次。

这里用 `(thread_id, call_id)` 做键，记录 `started` / `finished`：

- 只有 `started` 没有 `finished` → 结果未知。**不自动重放**，把不确定性
  明确交给模型/用户；
- 有 `finished` → 直接复用当时的结果；
- 没有记录 → 正常执行。

日志写在宿主的审计目录（不是工作区），因此不受沙箱故障影响。写入失败
只降级为“没有日志”，不能阻断工具执行。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# 单条结果最多保留多少字符；超出部分截断并标记，避免日志无限膨胀。
MAX_RESULT_CHARS = 20_000
MAX_ARTIFACT_CHARS = 20_000
# 超过这个大小后轮转一次，避免长期使用无限增长。
MAX_JOURNAL_BYTES = 10 * 1024 * 1024


def _digest(args: dict[str, Any]) -> str:
    try:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(args)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _safe(value: Any) -> Any:
    # artifact 通常是可 JSON 化的 dict；不是的话转成字符串，别让日志写失败。
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except (TypeError, ValueError):
        return str(value)


class ToolJournal:
    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and self.path)

    def _append(self, record: dict[str, Any]) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if (
                self.path.exists()
                and self.path.stat().st_size > MAX_JOURNAL_BYTES
            ):
                self.path.replace(self.path.with_suffix(".1.jsonl"))
            line = json.dumps(
                record, ensure_ascii=False, default=str, separators=(",", ":")
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # 日志是增强能力：磁盘满/权限不足时继续执行。
            self.enabled = False

    def begin(
        self,
        thread_id: str,
        call_id: str,
        tool: str,
        args: dict[str, Any],
    ) -> None:
        self._append(
            {
                "event": "started",
                "thread_id": thread_id,
                "call_id": call_id,
                "tool": tool,
                "args_sha": _digest(args),
            }
        )

    def finish(
        self,
        thread_id: str,
        call_id: str,
        tool: str,
        *,
        ok: bool,
        content: str,
        artifact: Any,
    ) -> None:
        text = str(content)
        truncated = len(text) > MAX_RESULT_CHARS
        if truncated:
            text = text[:MAX_RESULT_CHARS] + "\n…（日志已截断）"
        artifact_text = json.dumps(
            _safe(artifact), ensure_ascii=False, default=str
        )
        if len(artifact_text) > MAX_ARTIFACT_CHARS:
            artifact_text = (
                artifact_text[:MAX_ARTIFACT_CHARS]
                + "…（日志已截断）"
            )
            artifact_truncated = True
        else:
            artifact_truncated = False
        self._append(
            {
                "event": "finished",
                "thread_id": thread_id,
                "call_id": call_id,
                "tool": tool,
                "ok": bool(ok),
                "content": text,
                "artifact": artifact_text,
                "artifact_truncated": artifact_truncated,
                "content_truncated": truncated,
            }
        )

    def lookup(self, thread_id: str, call_id: str) -> dict[str, Any] | None:
        """返回该调用的最新状态；没有记录或读失败时返回 None。"""
        if not self.enabled or self.path is None or not self.path.exists():
            return None
        latest: dict[str, Any] | None = None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        record.get("thread_id") == thread_id
                        and record.get("call_id") == call_id
                    ):
                        latest = record
        except OSError:
            return None
        return latest


def replayed_artifact(record: dict[str, Any], tool: str) -> dict[str, Any]:
    """把日志里的 artifact JSON 还原成 dict，并标注这是重放结果。"""
    raw = record.get("artifact")
    artifact: Any = None
    if isinstance(raw, str):
        try:
            artifact = json.loads(raw)
        except json.JSONDecodeError:
            artifact = None
    if not isinstance(artifact, dict):
        artifact = {"ok": record.get("ok"), "tool": tool}
    artifact = dict(artifact)
    artifact["replayed"] = True
    if record.get("artifact_truncated"):
        artifact["replayed_truncated"] = True
    return artifact


def unknown_result_artifact(tool: str) -> dict[str, Any]:
    return {
        "ok": None,
        "tool": tool,
        "interrupted": True,
        "replayed": False,
    }
