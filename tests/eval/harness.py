"""端到端评测 harness。

评测集回答的问题是「这个智能体能不能真的完成任务」，与另外两层测试分工不同：

- `tests/unit` / `tests/integration` 测**机制**（判定逻辑、沙箱行为、路由收敛）
- `tests/eval` 测**能力**（给定一个任务，智能体能否把它做完）

两条判定原则
------------
1. **只看外部可观察结果**：判定是「在沙箱里跑项目自带的测试」，不看智能体
   用了哪些工具、改了几个文件、说了什么。内部实现随便换，只要结果对。
2. **判定不可被篡改**：每个任务的测试文件在判定前会被 harness 里的**纯净副本**
   覆盖回去。智能体改测试文件没有任何收益，它必须真的改源码。

因此 `EvalTask.tests` 里的内容会被写两次：一次作为种子（让智能体自己也能跑测试），
一次在判定前恢复。这不是冗余，是判定的可信性来源。
"""

from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from coding_agent.audit import AuditLogger
from coding_agent.config import Settings
from coding_agent.events import (
    PlanRevised,
    RepairStarted,
    RunFailed,
    RunFinished,
    StepStarted,
    ToolCallStarted,
    Verification,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace

# 判定用 **stdlib 的 unittest**，不用 pytest。这不是口味问题：
# 沙箱里 python3 是有的，但 pip / ensurepip / pytest 都没有（Ubuntu 默认如此），
# 而装它们要动用户的 WSL（sudo apt）。判定是"尺子"，必须零依赖、到哪都能跑。
DEFAULT_JUDGE = "python3 -m unittest discover -v"

# 评测工作区放在 agent 自己的私有目录下：`.agent/` 已经被系统提示、
# git_add 过滤、检索排除等多重机制保护，不必让日常会话在 `ls` 里看到 20 个
# 评测项目目录。每个任务再独立成 `<eval_root>/<task_id>/`，跑评测时智能体的
# 沙箱根就是那个任务目录，看不到别的任务。
EVAL_DIRNAME = ".agent/eval"

# 每个评测工作区都带一个 pytest.ini。**这不是给评测开后门，是把环境补成常态。**
#
# 项目自己的自动验证靠 `detect_test_command` 推导命令，而它要求工作区里有
# `tests/` 目录或 `pytest.ini` / `tox.ini`（见 tools/testrun.py）。种子把测试放在
# 根目录，缺这个信号时 verify 会一直返回 `not_configured` —— 于是 repair、
# 以及「修不动就改计划」那条 replan 入口**永远够不着**，评测就测不到它们。
# 真实项目通常至少有一个，所以统一补上。
#
# 判定用的是 stdlib unittest（见 DEFAULT_JUDGE），不受这个文件影响。
PYTEST_INI_PATH = "pytest.ini"
PYTEST_INI = "[pytest]\ntestpaths = .\n"

SINGLE_FILE = "single_file"
CROSS_FILE = "cross_file"
DEBUG = "debug"
# 需求未被测试覆盖：可见测试全绿，但行为不满足需求描述。
# 这类任务测的是"会不会只盯着绿化测试"，而不是照着需求把行为做对。
SPEC = "spec"
CATEGORIES = (SINGLE_FILE, CROSS_FILE, DEBUG, SPEC)

CATEGORY_LABELS = {
    SINGLE_FILE: "单文件改动",
    CROSS_FILE: "跨文件重构",
    DEBUG: "排障修复",
    SPEC: "需求未覆盖",
}


BASIC = "basic"
DEEP = "deep"
TIERS = (BASIC, DEEP)

TIER_LABELS = {
    BASIC: "基础",
    DEEP: "进阶",
}


@dataclass(frozen=True, slots=True)
class EvalTask:
    id: str
    category: str
    prompt: str
    # 智能体可见的项目文件（不含测试）
    sources: dict[str, str]
    # 可见测试：初始写进工作区，判定前用这份纯净版覆盖回去
    tests: dict[str, str]
    # 隐藏测试：**从不写进工作区**，只在判定时加进来。
    # 用于"可见测试全部通过、但需求未被满足"的任务 —— 这类任务测的是
    # 智能体会不会只盯着绿化测试，而不是照着需求把行为做对。
    hidden_tests: dict[str, str] = field(default_factory=dict)
    # 参考解：改动后的文件内容。用于验证任务**可满足** ——
    # 种子 + 参考解必须能让判定通过。它挡住的是"可见测试与新需求互相矛盾"
    # 这类错误：那种任务无论怎么做都过不了，会把做对的智能体判成失败（假阴性），
    # 与假阳性同样有害。
    reference: dict[str, str] = field(default_factory=dict)
    tier: str = BASIC
    judge: str = DEFAULT_JUDGE
    timeout: int = 300

    @property
    def seed_files(self) -> dict[str, str]:
        return {**self.sources, **self.tests}

    @property
    def judge_files(self) -> dict[str, str]:
        return {**self.tests, **self.hidden_tests}


@dataclass(slots=True)
class TaskResult:
    task_id: str
    category: str
    # passed（判定通过）/ failed（判定不通过）/ error（没跑起来，判定无效）
    verdict: str
    tier: str = BASIC
    detail: str = ""
    agent_seconds: float = 0.0
    judge_seconds: float = 0.0
    tool_calls: int = 0
    # 实际走过的步数，以及重规划被触发的次数。
    # 重规划的主要收益是**省**（砍掉多余的步骤、少烧轮次），所以只比通过率
    # 是量不出它的：真正该看的成本指标是这两个。
    steps: int = 0
    replans: int = 0
    answer: str = ""
    # ---- 机制指标：回答「那条路真的被走过吗」----
    # 只比通过率会得出反向结论。README 记着一次真实误读：`--ab` 跑出
    # 「开 3/3 对 关 2/3」，看着像重规划有效，实际 replans=0 —— 节点一次都没
    # 执行，那 33 个百分点纯是运行间噪声。要看清这种事，必须把"机制有没有
    # 发生"也记下来。
    #
    # `verifications` 是 status → 次数，不是单个计数：`not_configured`（识别不出
    # 测试命令）与 `ok` 的差别，恰恰是「验证脊柱有没有生效」的判据。
    # **口径**：只统计真正执行过的验证 —— `dirty=false` 时 verify 节点短路返回，
    # 不发事件，所以"每步都过 verify"不等于这里计数 ≥ 步数。
    verifications: dict[str, int] = field(default_factory=dict)
    repairs: int = 0
    # provider 未回报用量时为 None。**不要当 0** —— 那会把缺失算成"没花钱"。
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def solved(self) -> bool:
        return self.verdict == "passed"


# --------------------------------------------------------------------------
# 工作区
# --------------------------------------------------------------------------

def eval_root(settings: Settings, sandbox: WslSandbox) -> str:
    return f"{resolve_workspace(settings, sandbox)}/{EVAL_DIRNAME}"


def task_root(settings: Settings, sandbox: WslSandbox, task_id: str) -> str:
    return f"{eval_root(settings, sandbox)}/{task_id}"


def clean_workspace(sandbox: WslSandbox, root: str, *, base: str) -> None:
    """清空一个任务工作区。

    这是 `rm -rf`，所以**必须**确认 root 严格位于 base 之下。三个都不能省：

    - 归一化后再比：`<base>/../agent-ws` 的原始字符串确实以 base 开头，但归一化
      之后跑到评测根外面去了 —— 只做前缀匹配会删掉用户的工作区。
    - 不用子串判断：`<base>extra/x` 会蒙混过关。
    - 不许清 base 本身。
    """
    prefix = posixpath.normpath(base).rstrip("/") + "/"
    normalized = posixpath.normpath(root)
    if not normalized.startswith(prefix):
        raise ValueError(f"拒绝清理评测目录之外的路径：{root}（评测根 {base}）")
    result = sandbox.run(
        f"rm -rf -- {shlex.quote(normalized)} && mkdir -p -- {shlex.quote(normalized)}"
    )
    if not result.ok:
        raise RuntimeError(f"无法准备工作区 {normalized}：{result.render(300)}")


def materialize(task: EvalTask, fs: SandboxFs, root: str) -> None:
    """把种子写进工作区（测试文件也在内，让智能体能自己跑）。"""
    files = dict(task.seed_files)
    files.setdefault(PYTEST_INI_PATH, PYTEST_INI)
    for relpath, content in files.items():
        fs.write_text(f"{root}/{relpath}", content)


def apply_reference(task: EvalTask, fs: SandboxFs, root: str) -> None:
    """把参考解覆盖到种子上，用于验证任务可满足。"""
    for relpath, content in task.reference.items():
        fs.write_text(f"{root}/{relpath}", content)


def prepare_for_judging(task: EvalTask, sandbox: WslSandbox, fs: SandboxFs, root: str) -> None:
    """把工作区整理成可判定的状态。

    两件事：

    1. 删掉根目录下**不属于本任务**的 `test_*.py` —— 智能体自己写的临时测试
       不该参与判定（它可能本来就是正在调试的、失败的）。
    2. 写回纯净的可见测试，并加入隐藏测试。智能体改测试文件因此没有收益。
    """
    sandbox.run(f"rm -f {shlex.quote(root)}/test_*.py")
    for relpath, content in task.judge_files.items():
        fs.write_text(f"{root}/{relpath}", content)


_NO_TESTS_RE = re.compile(r"^Ran 0 tests", re.MULTILINE)


def judge(task: EvalTask, sandbox: WslSandbox, fs: SandboxFs, root: str) -> tuple[bool, str, float]:
    """恢复测试文件后执行判定命令。返回 (是否通过, 说明, 耗时)。"""
    prepare_for_judging(task, sandbox, fs, root)
    started = time.perf_counter()
    result = sandbox.run(task.judge, cwd=root, timeout=task.timeout)
    seconds = time.perf_counter() - started

    if result.timed_out:
        return False, f"判定超时（{task.timeout}s）", seconds

    output = result.stdout + result.stderr
    # `unittest discover` 一个用例都没发现时**退出码是 0**，会被当成通过。
    # 这是最危险的一种假阳性（任务其实什么都没验证），必须单独拦。
    if result.ok and _NO_TESTS_RE.search(output):
        return False, "判定一个用例都没发现（检查测试文件命名与位置）", seconds

    tail = output.strip()[-600:]
    return result.ok, tail or f"exit={result.exit_code}", seconds


# --------------------------------------------------------------------------
# 跑一个任务
# --------------------------------------------------------------------------

async def run_task(
    task: EvalTask,
    settings: Settings,
    sandbox: WslSandbox,
    fs: SandboxFs,
    root: str,
) -> TaskResult:
    """清理工作区 → 写种子 → 跑智能体 → 判定。"""
    clean_workspace(sandbox, root, base=eval_root(settings, sandbox))
    materialize(task, fs, root)

    # 评测里没有人在场，审批必须预先给定。这是 **harness 的显式选择**，
    # 不是智能体绕过了审批：审批链路本身仍原样生效，只是答案由 harness 给出。
    runtime = AgentRuntime(
        settings,
        workspace=root,
        allow_write=True,
        approval_mode="approve",
        checkpointer=MemorySaver(),
        audit=AuditLogger(eval_audit_dir()),
    )

    tool_calls = 0
    steps = 0
    replans = 0
    verifications: Counter[str] = Counter()
    repairs = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    answer = ""
    agent_error = ""
    started = time.perf_counter()
    try:
        async for event in runtime.run(task.prompt, thread_id=f"eval-{task.id}"):
            if isinstance(event, ToolCallStarted):
                tool_calls += 1
            elif isinstance(event, StepStarted):
                steps = max(steps, event.index + 1)
            elif isinstance(event, PlanRevised):
                replans += 1
            elif isinstance(event, Verification):
                verifications[event.status] += 1
            elif isinstance(event, RepairStarted):
                repairs += 1
            elif isinstance(event, RunFinished):
                answer = event.answer
                input_tokens, output_tokens = event.input_tokens, event.output_tokens
            elif isinstance(event, RunFailed):
                agent_error = event.message
                # 失败路径的用量同样入账：失败的任务往往花费最多
                input_tokens, output_tokens = event.input_tokens, event.output_tokens
    except Exception as exc:  # noqa: BLE001 - 编排层崩了也要留下记录，而不是中断整轮评测
        agent_error = f"{type(exc).__name__}: {exc}"
    finally:
        await runtime.aclose()
    agent_seconds = time.perf_counter() - started

    try:
        passed, detail, judge_seconds = judge(task, sandbox, fs, root)
    except Exception as exc:  # noqa: BLE001 - 沙箱不可用等，判定无效
        return TaskResult(
            task_id=task.id,
            category=task.category,
            verdict="error",
            tier=task.tier,
            detail=f"判定无法执行：{type(exc).__name__}: {exc}",
            agent_seconds=agent_seconds,
            tool_calls=tool_calls,
            steps=steps,
            replans=replans,
            verifications=dict(verifications),
            repairs=repairs,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    if agent_error:
        detail = f"智能体运行失败：{agent_error}\n{detail}"
    return TaskResult(
        task_id=task.id,
        category=task.category,
        verdict="passed" if passed else "failed",
        tier=task.tier,
        detail=detail,
        agent_seconds=agent_seconds,
        judge_seconds=judge_seconds,
        tool_calls=tool_calls,
        steps=steps,
        replans=replans,
        answer=answer,
        verifications=dict(verifications),
        repairs=repairs,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _bucket(rows: list[TaskResult]) -> dict[str, int]:
    return {"passed": sum(r.solved for r in rows), "total": len(rows)}


_VERIFY_SHORT = {
    "ok": "ok",
    "failed": "bad",
    "not_configured": "nc",
    "skipped": "skip",
}


def _verification_cell(counts: dict[str, int]) -> str:
    """把 status→次数 压成一格，好放进表格。

    `nc`（not_configured：识别不出测试命令）必须看得见 —— 那时验证脊柱**没有生效**，
    而通过率本身不会体现这一点。
    """
    if not counts:
        return "-"
    return "+".join(
        f"{_VERIFY_SHORT.get(status, status)}{'×' + str(count) if count > 1 else ''}"
        for status, count in sorted(counts.items())
    )


def mechanism_summary(results: list[TaskResult]) -> dict:
    """机制指标：这条回路真的被走过吗。

    它在评测里的地位与通过率并列 —— 一次「通过率没变」的改动，如果
    `verifications` 从 0 变成了 12，那是完全不同的结论。
    """
    statuses: Counter[str] = Counter()
    for result in results:
        statuses.update(result.verifications)
    with_tokens = [r for r in results if r.input_tokens is not None]
    return {
        "verifications": dict(sorted(statuses.items())),
        "tasks_with_verification": sum(1 for r in results if r.verifications),
        "repairs": sum(r.repairs for r in results),
        "tasks_with_repair": sum(1 for r in results if r.repairs),
        "replans": sum(r.replans for r in results),
        # 只汇总**有回报**的那些任务，并记下有几个 —— 部分缺失必须看得见，
        # 把 None 当 0 会让均值系统性偏低。
        "input_tokens": sum(r.input_tokens for r in with_tokens if r.input_tokens) or None,
        "output_tokens": sum(r.output_tokens for r in with_tokens if r.output_tokens) or None,
        "tasks_reporting_tokens": len(with_tokens),
    }


def summarize(results: list[TaskResult]) -> dict:
    """汇总成可直接写进 baseline 的结构。

    **必须按 tier 分开报**：基础档 100% 而进阶档 30% 与"总共 65%"是完全不同的
    两回事 —— 后者会让人以为还有很大提升空间，前者才说明尺子有区分度。
    """
    by_category = {
        category: _bucket([r for r in results if r.category == category])
        for category in CATEGORIES
        if any(r.category == category for r in results)
    }
    by_tier = {
        tier: _bucket([r for r in results if r.tier == tier])
        for tier in TIERS
        if any(r.tier == tier for r in results)
    }
    solved = sum(r.solved for r in results)
    return {
        "passed": solved,
        "total": len(results),
        "pass_rate": round(solved / len(results), 4) if results else 0.0,
        "by_category": by_category,
        "by_tier": by_tier,
        "mechanism": mechanism_summary(results),
        "tasks": [
            {
                "id": r.task_id,
                "category": r.category,
                "tier": r.tier,
                "verdict": r.verdict,
                "tool_calls": r.tool_calls,
                "steps": r.steps,
                "replans": r.replans,
                "verifications": r.verifications,
                "repairs": r.repairs,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "agent_seconds": round(r.agent_seconds, 1),
            }
            for r in results
        ],
    }


def noise_summary(runs: list[dict]) -> dict:
    """逐次运行的波动 —— **观测值，不是统计推断**。

    刻意不写「置信区间 / 显著性」：重复 2~3 次没有那种效力，写上去等于给
    噪声套上科学的壳。这里只报告观测到的范围与翻转过哪些任务，让读者自己
    判断某次改动是否落在噪声里。

    `resolution` 是这把尺子的**分辨率**：翻一个任务相当于多少个百分点。
    29 个任务时它是 3.4% —— 比它更小的"提升"在单次运行里根本不可分辨。
    """
    rates = [run["pass_rate"] for run in runs]
    per_task: dict[str, list[str]] = {}
    for run in runs:
        for task in run["tasks"]:
            per_task.setdefault(task["id"], []).append(task["verdict"])
    total = runs[0]["total"] if runs else 0
    return {
        "repeats": len(runs),
        "pass_rate_per_run": [round(rate, 4) for rate in rates],
        "min": round(min(rates), 4) if rates else 0.0,
        "max": round(max(rates), 4) if rates else 0.0,
        "spread": round(max(rates) - min(rates), 4) if rates else 0.0,
        # 同一任务在不同次之间结果不一致 —— 这些就是"噪声"的具体成员
        "flipped": sorted(tid for tid, vs in per_task.items() if len(set(vs)) > 1),
        "resolution": round(1 / total, 4) if total else 0.0,
        "note": "观测到的逐次波动，不是置信区间；重复次数少时无统计效力。",
    }


def _aggregate_verdict(verdicts: list[str], repeats: int) -> str:
    """逐任务在 k 次里的合成结论。

    **不稳定单独成一态**（`flaky`）：把它混进 passed 或 failed 都会掩盖
    「这个任务的结论取决于运气」这件事，而它恰恰是最该被看见的信息。
    """
    passes = verdicts.count("passed")
    if passes == repeats:
        return "passed"
    if passes == 0:
        return "failed"
    return "flaky"


def aggregate_runs(runs: list[dict]) -> dict:
    """把 k 次运行合成一份 baseline。

    k=1 时原样返回；k>1 时头条数字取**各次均值**，逐任务改记「k 次里过了几次」
    —— 单次的 verdict 在重复运行下没有意义（同一任务可能忽过忽不过）。
    """
    if len(runs) == 1:
        summary = dict(runs[0])
        summary["aggregation"] = "single_run"
        summary["noise"] = noise_summary(runs)
        return summary

    total = runs[0]["total"]
    per_task: dict[str, list[str]] = {}
    for run in runs:
        for task in run["tasks"]:
            per_task.setdefault(task["id"], []).append(task["verdict"])

    mean_rate = sum(run["pass_rate"] for run in runs) / len(runs)
    summary = dict(runs[0])
    summary["aggregation"] = "mean_over_repeats"
    summary["pass_rate"] = round(mean_rate, 4)
    summary["passed"] = round(mean_rate * total)
    for key in ("by_tier", "by_category"):
        merged = {}
        for name in runs[0][key]:
            rate = sum(
                run[key][name]["passed"] / run[key][name]["total"] for run in runs
            ) / len(runs)
            merged[name] = {
                "passed": round(rate * runs[0][key][name]["total"]),
                "total": runs[0][key][name]["total"],
                "pass_rate": round(rate, 4),
            }
        summary[key] = merged
    summary["tasks"] = [
        {
            **task,
            "verdict": _aggregate_verdict(per_task[task["id"]], len(runs)),
            "passes": per_task[task["id"]].count("passed"),
            "repeats": len(runs),
        }
        for task in runs[0]["tasks"]
    ]
    summary["noise"] = noise_summary(runs)
    return summary


def comparability(
    *, full_set: bool, capabilities: dict[str, bool], git: dict
) -> list[str]:
    """列出「这份数字不可比」的理由；空列表表示可比。

    三种情况都会让通过率**说不清含义**，而不清含义的数字一旦写进 baseline，
    下一版拿它做对照就得出错误结论：

    - **子集**：`--only debug` 会把 10 个任务的结果覆盖写到全量 baseline 上。
    - **脏工作区**：涨了 10 个点，是改了能力，还是跑的时候躺着别的改动？
    - **沙箱无 pytest**：Python 项目的自动验证不生效，测的是「没有修复循环」的智能体。
    """
    reasons = []
    if not full_set:
        reasons.append("只跑了任务子集（--only）：统计口径与全量 baseline 不同")
    if git.get("git_dirty"):
        reasons.append("工作区有未提交改动：数字说不清对应哪版代码")
    if not capabilities.get("pytest"):
        reasons.append(
            "沙箱里没有 pytest：自动验证对 Python 项目不生效，"
            "测到的是「没有修复循环」的智能体"
        )
    return reasons


def format_table(results: list[TaskResult]) -> str:
    lines = [
        f"{'任务':<34} {'档':<4} {'类别':<12} {'结果':<8} "
        f"{'工具调用':>8} {'步数':>5} {'验证':<10} {'修复':>4} {'重规划':>6} {'耗时':>8}",
        "-" * 118,
    ]
    for r in results:
        mark = {"passed": "通过", "failed": "未通过", "error": "无效"}[r.verdict]
        tier = {"basic": "基础", "deep": "进阶"}.get(r.tier, r.tier)
        lines.append(
            f"{r.task_id:<34} {tier:<4} {CATEGORY_LABELS.get(r.category, r.category):<12} "
            f"{mark:<8} {r.tool_calls:>8} {r.steps:>5} "
            f"{_verification_cell(r.verifications):<10} {r.repairs:>4} {r.replans:>6} "
            f"{r.agent_seconds:>7.1f}s"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 判定能力自检
# --------------------------------------------------------------------------

# 一个已经正确的项目。判定在它上面**必须通过** —— 否则问题出在环境或判定命令，
# 而不是被测的智能体。
PREFLIGHT_TASK = EvalTask(
    id="_preflight",
    category=SINGLE_FILE,
    prompt="（自检任务，不会被交给智能体）",
    sources={"calc.py": "def add(a, b):\n    return a + b\n"},
    tests={
        "test_calc.py": (
            "import unittest\n"
            "\n"
            "from calc import add\n"
            "\n"
            "\n"
            "class AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n"
        )
    },
)


def preflight(sandbox: WslSandbox, fs: SandboxFs, base: str) -> None:
    """先证明「判定能通过」，再开始评测。

    这道检查来自一次真实事故：沙箱里没有 pytest，`python3 -m pytest` 于是对
    所有任务都返回失败，而**种子自检看到的是「20/20 未解决」** —— 一个看起来
    很干净、实际上什么都没测出来的结果。种子自检只能证明"没通过"，证明不了
    "有可能通过"；只有前置自检能。任何"判定永远不可能通过"的环境问题都在此拦下。
    """
    root = f"{base}/{PREFLIGHT_TASK.id}"
    clean_workspace(sandbox, root, base=base)
    materialize(PREFLIGHT_TASK, fs, root)
    passed, detail, _ = judge(PREFLIGHT_TASK, sandbox, fs, root)
    if not passed:
        raise RuntimeError(
            "判定自检未通过：在一个已知正确的项目上，判定命令没能通过。\n"
            "此时任何评测结果都不可信 —— 先修好判定环境或判定命令。\n"
            f"判定命令：{PREFLIGHT_TASK.judge}\n"
            f"输出：\n{detail}"
        )


def sandbox_capabilities(sandbox: WslSandbox) -> dict[str, bool]:
    """沙箱里有哪些运行器。

    记进 baseline 是为了让数字可解释：**如果 `pytest` 是 false，那么智能体自己的
    自动验证（`detect_test_command` 需要它）在整个评测里从未生效过** ——
    这时测到的是"没有修复循环的智能体"。同一份通过率，含义完全不同。
    """
    probes = {
        "python3": "python3 -V",
        "pytest": "python3 -c 'import pytest'",
        "make": "command -v make",
        "node": "command -v node",
        "cargo": "command -v cargo",
        "go": "command -v go",
    }
    return {name: sandbox.run(command).ok for name, command in probes.items()}


def baseline_path() -> Path:
    return Path(__file__).resolve().parent / "baseline.json"


def eval_audit_dir() -> Path:
    """评测运行的审计日志目录 —— **宿主侧**路径。

    `AuditLogger` 是宿主侧组件，别把沙箱路径交给它：`pathlib.Path("/home/hong/...")`
    在 Windows 上不是绝对路径（没有盘符），会被当成"当前盘符下的相对路径"，
    于是审计静默写到 `D:\\home\\hong\\...` 去。这正是本仓库文档里反复提醒的
    "宿主侧与沙箱侧是两处，别混淆"。
    """
    return Path(tempfile.gettempdir()) / "coding-agent-eval-audit"


def git_state() -> dict[str, object]:
    """baseline 对应的代码版本。

    一份说不清对应哪版代码的通过率没有意义：下一版涨了 10 个点，是因为改了能力，
    还是因为跑的时候工作区里躺着别的改动？`git_dirty` 就是把这件事写明白 ——
    只在干净工作区上跑出来的数字才可比较。
    """
    root = Path(__file__).resolve().parents[2]

    def _git(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip()

    return {
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
    }
