"""Phase 3D：实时用户干预的指令队列。

`AgentRuntime.steer()` 把用户命令放进这里；图节点在**边界**消费。
队列按 thread 隔离，只活在当前进程里 —— 挂起状态由 checkpoint 保存，
但没消费完的 add/replan 命令不会跨进程恢复（见 docs/HARNESS.md 的已知限制）。

为什么不是 LangGraph state：
- 命令是**外部输入**，不属于模型可写的状态；
- worker 需要非破坏性地轮询 cancel（`cancel_requested`），而 state 更新与图
  执行是串行的，做不到运行中读取；
- pause 恢复时节点会从头重跑，队列必须能跨这次重跑保留/回填。
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """按 thread_id 存放待消费指令与暂停标记。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[SteeringCommand]] = {}
        self._paused: set[str] = set()

    def submit(self, thread_id: str, command: SteeringCommand) -> None:
        self._queues.setdefault(thread_id, []).append(command)

    def peek(self, thread_id: str) -> list[SteeringCommand]:
        """看一眼但不消费。worker 的取消轮询用这个。"""
        return list(self._queues.get(thread_id) or ())

    def consume(self, thread_id: str) -> list[SteeringCommand]:
        """取走当前全部指令。节点恢复后重跑时只会看到新指令。"""
        return self._queues.pop(thread_id, [])

    def requeue(self, thread_id: str, commands: list[SteeringCommand]) -> None:
        """把这一批里**尚未处理**的指令放回队首，保持原顺序。

        pause 会 `interrupt()`，节点恢复时从头重跑；先消费再回填，避免
        同一条 pause 让节点无限挂起，也避免同批的 add/replan 丢失。
        """
        if not commands:
            return
        queue = self._queues.setdefault(thread_id, [])
        queue[:0] = list(commands)

    def cancel_requested(self, thread_id: str) -> bool:
        """是否有未消费的 cancel。非破坏性，worker 每轮都可以问。"""
        return any(
            command.action == "cancel"
            for command in self._queues.get(thread_id) or ()
        )

    def mark_paused(self, thread_id: str) -> None:
        self._paused.add(thread_id)

    def clear_paused(self, thread_id: str) -> None:
        self._paused.discard(thread_id)

    def is_paused(self, thread_id: str) -> bool:
        return thread_id in self._paused

    def clear(self, thread_id: str) -> None:
        """一轮结束（或全新 run 开始）时清空。"""
        self._queues.pop(thread_id, None)
        self._paused.discard(thread_id)
