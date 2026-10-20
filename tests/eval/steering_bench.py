"""干预延迟基准：pause / resume / cancel 在节点边界生效要多久。

口径
----

不调用模型、不依赖 WSL。合成图里有一个可控的 `work` 节点（`sleep` 模拟一次
正在执行的 LLM/工具轮次），后面接**真实的** `steering_step` 节点，由真实
`AgentRuntime` 驱动。因此：

- `wait`：从提交指令到当前节点结束 —— 这部分由工作本身决定，**不是**框架开销；
- `apply`：节点边界到 `SteeringApplied` 事件 —— 这才是 steering 的处理开销；
- `total` = wait + apply。

把三者分开报，避免拿"模型跑得慢"冒充"干预慢"。

用法：

    python tests/eval/steering_bench.py --repeat 3 --delay-ms 200
    python tests/eval/steering_bench.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows 管道默认 GBK，中文表头会乱码（与 runner.py 同款处理）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from coding_agent.audit import AuditLogger  # noqa: E402
from coding_agent.config import Settings  # noqa: E402
from coding_agent.events import RunFinished, SteeringApplied  # noqa: E402
from coding_agent.graph.nodes.steering import (  # noqa: E402
    make_steering_step_node,
)
from coding_agent.graph.state import AgentState  # noqa: E402
from coding_agent.runtime import AgentRuntime  # noqa: E402


class _NullMemory:
    def load(self) -> list[str]:
        return []

    def search(self, query: str = "", limit: int = 8) -> list[str]:
        return []


def _build_graph(
    runtime: AgentRuntime,
    settings: Settings,
    delay_seconds: float,
    started: asyncio.Event,
    boundary: dict[str, float],
):
    """START → work → steering_step → respond → END。"""

    async def work(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(delay_seconds)
        # 节点返回前的那一刻 = 真正的执行边界。之后到 steering 事件之间的
        # 时间才是框架处理开销（apply），不含等待。
        boundary["at"] = time.perf_counter()
        return {"messages": [AIMessage(content="work done")]}

    async def respond(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        return {"messages": [AIMessage(content="done")]}

    graph = StateGraph(AgentState)
    graph.add_node("work", work)
    graph.add_node(
        "steering_step", make_steering_step_node(runtime.steering, settings)
    )
    graph.add_node("respond", respond)
    graph.add_edge(START, "work")
    graph.add_edge("work", "steering_step")
    graph.add_edge("steering_step", "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=MemorySaver())


def _make_runtime(
    settings: Settings, delay_seconds: float
) -> tuple[AgentRuntime, asyncio.Event, dict[str, float]]:
    runtime = AgentRuntime(
        settings,
        workspace="/tmp/steering-bench",
        checkpointer=MemorySaver(),
        audit=AuditLogger(Path(tempfile.mkdtemp(prefix="steering-bench-"))),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    started = asyncio.Event()
    boundary: dict[str, float] = {}
    runtime._graph = _build_graph(
        runtime, settings, delay_seconds, started, boundary
    )
    return runtime, started, boundary


async def _pause_once_precise(delay_seconds: float) -> dict[str, float]:
    """pause + resume；把 wait（等工作结束）与 apply（边界→事件）分开。"""
    settings = Settings(_env_file=None)
    runtime, started, boundary = _make_runtime(settings, delay_seconds)
    thread_id = f"bench-pause-{time.perf_counter_ns()}"
    pause_at: float | None = None

    async def consume() -> None:
        nonlocal pause_at
        async for event in runtime.run("bench", thread_id=thread_id):
            if isinstance(event, SteeringApplied) and event.action == "pause":
                pause_at = time.perf_counter()

    task = asyncio.create_task(consume())
    await started.wait()
    submit_at = time.perf_counter()
    runtime.steer(thread_id, "pause")
    await task
    boundary_at = boundary.get("at")
    if boundary_at is None or pause_at is None:
        raise RuntimeError("没有观察到 pause 的边界/事件")

    resume_at = time.perf_counter()
    resumed = [event async for event in runtime.resume_steering(thread_id)]
    resume_done_at = time.perf_counter()
    if not any(isinstance(event, RunFinished) for event in resumed):
        raise RuntimeError("resume 之后没有 RunFinished")
    await runtime.aclose()

    return {
        # 并行负载下进程可能被调度走，started 与 submit 的先后不保证；
        # 延迟不允许为负，钳到 0 并保留其余测量。
        "wait_ms": max(0.0, (boundary_at - submit_at) * 1000),
        "apply_ms": max(0.0, (pause_at - boundary_at) * 1000),
        "total_ms": max(0.0, (pause_at - submit_at) * 1000),
        "resume_ms": max(0.0, (resume_done_at - resume_at) * 1000),
    }


async def _cancel_once(delay_seconds: float) -> dict[str, float]:
    settings = Settings(_env_file=None)
    runtime, started, boundary = _make_runtime(settings, delay_seconds)
    thread_id = f"bench-cancel-{time.perf_counter_ns()}"
    cancel_at: float | None = None

    async def consume() -> None:
        nonlocal cancel_at
        async for event in runtime.run("bench", thread_id=thread_id):
            if isinstance(event, SteeringApplied) and event.action == "cancel":
                cancel_at = time.perf_counter()

    task = asyncio.create_task(consume())
    await started.wait()
    submit_at = time.perf_counter()
    runtime.steer(thread_id, "cancel")
    await task
    boundary_at = boundary.get("at")
    if boundary_at is None or cancel_at is None:
        raise RuntimeError("没有观察到 cancel 的边界/事件")
    await runtime.aclose()

    return {
        "wait_ms": max(0.0, (boundary_at - submit_at) * 1000),
        "apply_ms": max(0.0, (cancel_at - boundary_at) * 1000),
        "total_ms": max(0.0, (cancel_at - submit_at) * 1000),
    }


def _aggregate(rows: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    keys = sorted({key for row in rows for key in row})
    return {
        key: {
            "min": round(min(row[key] for row in rows), 3),
            "median": round(statistics.median(row[key] for row in rows), 3),
            "max": round(max(row[key] for row in rows), 3),
        }
        for key in keys
    }


async def _run(repeat: int, delay_seconds: float) -> dict[str, Any]:
    pauses = [await _pause_once_precise(delay_seconds) for _ in range(repeat)]
    cancels = [await _cancel_once(delay_seconds) for _ in range(repeat)]
    return {
        "delay_ms": round(delay_seconds * 1000, 3),
        "repeat": repeat,
        "pause_resume": _aggregate(pauses),
        "cancel": _aggregate(cancels),
        "note": (
            "wait=提交到节点边界；apply=边界到 SteeringApplied；"
            "total=wait+apply。不含真实 LLM/网络耗时。"
        ),
    }


def run_benchmark(*, repeat: int = 3, delay_ms: float = 200.0) -> dict[str, Any]:
    """同步入口，供脚本与单测共用。"""
    return asyncio.run(_run(max(repeat, 1), max(delay_ms, 0.0) / 1000))


def _print(report: dict[str, Any]) -> None:
    print(
        f"干预延迟基准（delay={report['delay_ms']}ms，repeat={report['repeat']}）"
    )
    for scenario in ("pause_resume", "cancel"):
        print(f"\n[{scenario}]")
        for key, stats in report[scenario].items():
            print(
                f"  {key:<10} median {stats['median']:>8.2f}ms"
                f"   min {stats['min']:.2f} / max {stats['max']:.2f}"
            )
    print(f"\n口径：{report['note']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="steering 干预延迟基准")
    parser.add_argument("--repeat", type=int, default=3, help="每个场景重复次数")
    parser.add_argument("--delay-ms", type=float, default=200.0, help="work 节点的模拟耗时")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = run_benchmark(repeat=args.repeat, delay_ms=args.delay_ms)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
