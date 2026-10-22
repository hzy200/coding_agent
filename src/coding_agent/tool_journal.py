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

from coding_agent.filelock import FileLock
from coding_agent.security.secrets import redact_secrets
from coding_agent.tools.artifacts import InterruptedArtifact

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


def _redact_value(value: Any) -> Any:
    """在序列化**之前**对字符串字段脱敏，保持 JSON 仍然合法。"""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class ToolJournal:
    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and self.path)
        # 惰性索引：lookup 不再逐次扫描整个文件。文件变化（轮转/另一进程写入）
        # 时通过 (mtime_ns, size) 检测并重建。
        self._cache: dict[tuple[str, str], dict[str, Any]] | None = None
        self._cache_state: tuple[int, int] | None = None

    def _file_state(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _append(self, record: dict[str, Any]) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = self.path.with_suffix(self.path.suffix + ".lock")
            with FileLock(lock):
                if (
                    self.path.exists()
                    and self.path.stat().st_size > MAX_JOURNAL_BYTES
                ):
                    self.path.replace(self.path.with_suffix(".1.jsonl"))
                    self._cache = None
                    self._cache_state = None
                line = json.dumps(
                    record, ensure_ascii=False, default=str, separators=(",", ":")
                )
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                if self._cache is not None:
                    key = (
                        str(record.get("thread_id", "")),
                        str(record.get("call_id", "")),
                    )
                    self._cache[key] = record
                    self._cache_state = self._file_state()
        except OSError:
            # 日志是增强能力：磁盘满/权限不足时继续执行。
            self.enabled = False
            self._cache = None
            self._cache_state = None

    def _load_cache(self) -> dict[tuple[str, str], dict[str, Any]]:
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        if self.path is None or not self.path.exists():
            return cache
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
                    key = (
                        str(record.get("thread_id", "")),
                        str(record.get("call_id", "")),
                    )
                    cache[key] = record
        except OSError:
            return {}
        return cache

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
        text = redact_secrets(str(content))
        truncated = len(text) > MAX_RESULT_CHARS
        if truncated:
            text = text[:MAX_RESULT_CHARS] + "\n…（日志已截断）"
        # 工具输出/artifact 可能包含密钥；日志是落盘数据，必须先脱敏。
        # 在 json.dumps **之前**处理字符串，避免破坏转义。
        artifact_text = json.dumps(
            _redact_value(_safe(artifact)), ensure_ascii=False, default=str
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
        """返回该调用的最新状态；没有记录或读失败时返回 None。

        首次查找或文件发生变化时整文件加载一次，之后走内存索引。文件很小
        但也经不起每次副作用调用都全量扫描。
        """
        if not self.enabled or self.path is None:
            return None
        state = self._file_state()
        if self._cache is None or state != self._cache_state:
            self._cache = self._load_cache()
            self._cache_state = state
        return self._cache.get((thread_id, call_id))


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
    """结果未知的结构化产物。

    必须带 `kind`：否则事件层只能按文本前缀猜成败，而这段提示文本不含
    “失败”标记，会被误记成 ok=True。
    """
    return InterruptedArtifact(tool=tool).model_dump()
