"""Phase 3D：实时用户干预的指令队列。

`AgentRuntime.steer()` 把用户命令放进这里；图节点在**边界**消费。
队列按 thread 隔离。配置了 `directory` 时，未消费指令与 pause 标记会
落盘到宿主 `.agent/steering/`，因此进程重启后仍能通过 `resume` 恢复。

为什么不是 LangGraph state：
- 命令是**外部输入**，不属于模型可写的状态；
- worker 需要非破坏性地轮询 cancel（`cancel_requested`），而 state 更新与图
  执行是串行的，做不到运行中读取；
- pause 恢复时节点会从头重跑，队列必须能跨这次重跑保留/回填。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from coding_agent.filelock import FileLock, NullLock

# resume 不走队列：它必须把挂起的图唤醒（Command(resume=...)），
# 放进队列只会在恢复之后才被消费，语义不对。
STEERING_ACTIONS = frozenset({"pause", "cancel", "add", "replan"})

# /add 只允许只读角色。写任务需要明确的 write_scope，自由文本无法安全推断，
# 而且并发写还受 scope 不重叠约束（见 orchestrator/graph.py）。
ADD_ROLES = frozenset({"explore", "test", "review"})


@dataclass(frozen=True, slots=True)
class SteeringCommand:
    """一条用户干预指令。"""

    action: str
    text: str = ""
    role: str = "explore"
    source: str = "user"
    ts: str = ""

    def __post_init__(self) -> None:
        if self.action not in STEERING_ACTIONS:
            raise ValueError(f"不支持的 steering 动作：{self.action}")
        if self.action == "add" and self.role not in ADD_ROLES:
            raise ValueError(f"/add 只支持只读角色：{sorted(ADD_ROLES)}")


class SteeringController:
    """按 thread_id 存放待消费指令与暂停标记。

    `directory=None` 时保持纯内存行为（单测/嵌入式调用）；
    传入目录后每次变更都会原子落盘，构造时按需懒加载。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._queues: dict[str, list[SteeringCommand]] = {}
        self._paused: set[str] = set()
        self._directory = Path(directory) if directory else None
        self._loaded: set[str] = set()
        # 每 thread 最近一次看到的文件状态；未变化时跳过整文件读取。
        self._disk_state: dict[str, tuple[int, int] | None] = {}
        self._lock = threading.Lock()
        if self._directory is not None:
            self._directory.mkdir(parents=True, exist_ok=True)

    # ---------------- 持久化 ----------------

    def _path(self, thread_id: str) -> Path:
        assert self._directory is not None
        digest = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:24]
        return self._directory / f"{digest}.json"

    @staticmethod
    def _file_state(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _file_lock(self, thread_id: str) -> FileLock | NullLock:
        if self._directory is None:
            return NullLock()
        path = self._path(thread_id)
        return FileLock(path.with_suffix(path.suffix + ".lock"))

    def _ensure_loaded(self, thread_id: str, *, force: bool = False) -> None:
        """从磁盘刷新该 thread 的状态。

        文件内容变化后重新读：这样另一个进程（例如 `agent steer`）写入的
        指令能被正在运行的 agent 在下一个边界看到。未变化时只做一次 stat；
        scheduler 的取消轮询会高频调用这里，整文件读取没有必要。

        `force=True` 用于写操作：拿到文件锁后必须重读，才能把别的进程刚写的
        指令合并进来，而不是用本地旧快照覆盖掉它。
        """
        if self._directory is None:
            return
        path = self._path(thread_id)
        state = self._file_state(path)
        if (
            not force
            and thread_id in self._loaded
            and self._disk_state.get(thread_id) == state
        ):
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # 文件被删除（clear 或从未写过）：同步清掉内存里的旧状态。
            if thread_id in self._loaded:
                self._queues.pop(thread_id, None)
                self._paused.discard(thread_id)
            self._loaded.add(thread_id)
            self._disk_state[thread_id] = None
            return
        except (OSError, json.JSONDecodeError):
            self._loaded.add(thread_id)
            self._disk_state[thread_id] = state
            return

        self._loaded.add(thread_id)
        self._disk_state[thread_id] = state
        if str(payload.get("thread_id", "")) != thread_id:
            return
        commands: list[SteeringCommand] = []
        for item in payload.get("commands") or []:
            try:
                commands.append(SteeringCommand(**item))
            except (TypeError, ValueError):
                continue
        if commands:
            self._queues[thread_id] = commands
        else:
            self._queues.pop(thread_id, None)
        if payload.get("paused"):
            self._paused.add(thread_id)
        else:
            self._paused.discard(thread_id)

    def _persist(self, thread_id: str) -> None:
        if self._directory is None:
            return
        path = self._path(thread_id)
        commands = self._queues.get(thread_id) or []
        paused = thread_id in self._paused
        if not commands and not paused:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._disk_state[thread_id] = None
            return
        payload = {
            "thread_id": thread_id,
            "paused": paused,
            "commands": [asdict(command) for command in commands],
        }
        temp = path.with_suffix(".json.tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temp, path)
            self._disk_state[thread_id] = self._file_state(path)
        except OSError:
            # 持久化失败不能拖垮当前进程；内存队列仍然可用。
            try:
                temp.unlink()
            except OSError:
                pass

    # ---------------- 队列 ----------------

    def submit(self, thread_id: str, command: SteeringCommand) -> None:
        with self._lock, self._file_lock(thread_id):
            self._ensure_loaded(thread_id, force=True)
            self._queues.setdefault(thread_id, []).append(command)
            self._persist(thread_id)

    def peek(self, thread_id: str) -> list[SteeringCommand]:
        """看一眼但不消费。worker 的取消轮询用这个。"""
        with self._lock:
            self._ensure_loaded(thread_id)
            return list(self._queues.get(thread_id) or ())

    def consume(self, thread_id: str) -> list[SteeringCommand]:
        """取走当前全部指令。节点恢复后重跑时只会看到新指令。"""
        with self._lock, self._file_lock(thread_id):
            self._ensure_loaded(thread_id, force=True)
            commands = self._queues.pop(thread_id, [])
            self._persist(thread_id)
            return commands

    def requeue(self, thread_id: str, commands: list[SteeringCommand]) -> None:
        """把这一批里**尚未处理**的指令放回队首，保持原顺序。

        pause 会 `interrupt()`，节点恢复时从头重跑；先消费再回填，避免
        同一条 pause 让节点无限挂起，也避免同批的 add/replan 丢失。
        """
        if not commands:
            return
        with self._lock, self._file_lock(thread_id):
            self._ensure_loaded(thread_id, force=True)
            queue = self._queues.setdefault(thread_id, [])
            queue[:0] = list(commands)
            self._persist(thread_id)

    def cancel_requested(self, thread_id: str) -> bool:
        """是否有未消费的 cancel。非破坏性，worker 每轮都可以问。"""
        return any(
            command.action == "cancel" for command in self.peek(thread_id)
        )

    def mark_paused(self, thread_id: str) -> None:
        with self._lock, self._file_lock(thread_id):
            self._ensure_loaded(thread_id, force=True)
            self._paused.add(thread_id)
            self._persist(thread_id)

    def clear_paused(self, thread_id: str) -> None:
        with self._lock, self._file_lock(thread_id):
            self._ensure_loaded(thread_id, force=True)
            self._paused.discard(thread_id)
            self._persist(thread_id)

    def is_paused(self, thread_id: str) -> bool:
        with self._lock:
            self._ensure_loaded(thread_id)
            return thread_id in self._paused

    def clear(self, thread_id: str) -> None:
        """一轮结束（或全新 run 开始）时清空。"""
        with self._lock, self._file_lock(thread_id):
            self._queues.pop(thread_id, None)
            self._paused.discard(thread_id)
            self._loaded.add(thread_id)
            self._persist(thread_id)
