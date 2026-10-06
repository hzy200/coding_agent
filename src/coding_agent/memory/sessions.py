"""会话索引：从审计日志归纳出历史会话。

为什么不另建一张会话表：审计日志里已经有 `run_start`（含 thread_id、工作区、
用户提示词）和逐条 `tool_call`，这些正是"有哪些会话、聊了什么、动了多少工具"
所需的全部信息。再维护一份索引就是重复状态，还得处理两边不一致。

代价是**审计关闭时列不出会话** —— 这是可以接受的降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.audit.logger import audit_files_by_day, read_records
from coding_agent.audit.models import PLAN, RUN_END, RUN_START, TOOL_CALL

# 只扫最近这么多**天**的审计文件，避免长年累积后越读越慢。
# 单位是天而不是文件：启用轮转（AGENT_AUDIT_MAX_MB > 0）后一天会有多片，
# 按文件数回溯会让轮转片吃掉配额，更早的会话静默消失。
DEFAULT_LOOKBACK_DAYS = 30
TITLE_CHARS = 80


@dataclass(slots=True)
class SessionInfo:
    thread_id: str
    title: str = ""
    workspace: str = ""
    started_at: str = ""
    last_active: str = ""
    prompts: int = 0
    tool_calls: int = 0
    plans: int = 0

    @property
    def started_day(self) -> str:
        return self.started_at[:10]

    @property
    def last_seen(self) -> str:
        """给列表展示用的短时间戳。"""
        return self.last_active[:19].replace("T", " ")


class SessionIndex:
    """把审计日志按 thread_id 归纳成会话列表。"""

    def __init__(self, audit_dir: str | Path, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
        self.directory = Path(audit_dir)
        self.lookback_days = lookback_days

    def _audit_files(self) -> list[Path]:
        """最近 N **天**的全部片段，按时间序（旧 → 新，同一天内首片在前）。"""
        by_day = audit_files_by_day(self.directory)
        # 文件名是 YYYY-MM-DD.jsonl，字典序即时间序
        files: list[Path] = []
        for day in sorted(by_day)[-self.lookback_days :]:
            files.extend(by_day[day])
        return files

    def list(self, *, limit: int = 20) -> list[SessionInfo]:
        """按最近活跃时间倒序列出会话。"""
        sessions: dict[str, SessionInfo] = {}

        for path in self._audit_files():
            for record in read_records(path, limit=0):
                if not record.thread_id or not record.ts:
                    continue
                info = sessions.get(record.thread_id)
                if info is None:
                    info = SessionInfo(thread_id=record.thread_id)
                    sessions[record.thread_id] = info

                if record.ts > info.last_active:
                    info.last_active = record.ts

                if record.kind == RUN_START:
                    if not info.started_at or record.ts < info.started_at:
                        info.started_at = record.ts
                        # 标题取该会话最早一次提问
                        info.title = " ".join(record.detail.split())[:TITLE_CHARS]
                    if record.workspace:
                        info.workspace = record.workspace
                    info.prompts += 1
                elif record.kind == TOOL_CALL:
                    info.tool_calls += 1
                elif record.kind == PLAN:
                    info.plans += 1
                elif record.kind == RUN_END and info.title == "":
                    info.title = " ".join(record.detail.split())[:TITLE_CHARS]

        ordered = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
        # limit <= 0 表示不限量（与 audit.read_records 的约定一致）
        return ordered[:limit] if limit > 0 else ordered

    def get(self, thread_id: str) -> SessionInfo | None:
        for info in self.list(limit=0):
            if info.thread_id == thread_id:
                return info
        return None


def format_table_rows(sessions: list[SessionInfo]) -> list[tuple[str, ...]]:
    """给 CLI / TUI 共用的表格数据。"""
    return [
        (
            s.thread_id,
            s.last_seen,
            str(s.prompts),
            str(s.tool_calls),
            s.title or "（无记录）",
        )
        for s in sessions
    ]
