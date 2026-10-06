"""评测 runner。需要真实 WSL 沙箱；跑全量还需要 DEEPSEEK_API_KEY。

    python tests/eval/runner.py --list            # 看任务清单
    python tests/eval/runner.py --verify-seeds    # 不需要 API Key：确认种子都是"未解决"
    python tests/eval/runner.py --only debug      # 只跑某一类
    python tests/eval/runner.py --only debug/output_order
    python tests/eval/runner.py                   # 全量，结果写 baseline.json

**为什么是独立脚本而不是 pytest 用例**：一次全量要真实调用模型、耗时以十分钟计，
且结果不该让 CI 变红。它度量的是能力，不是回归；回归由 tests/unit 与
tests/integration 守着。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (_REPO_ROOT / "src", Path(__file__).resolve().parent):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

# Windows 的标准流默认走 GBK：管道/重定向时中文输出会乱码。与 cli/app.py 同款处理，
# 否则这个脚本的表头、拒绝理由在 CI 日志里全是问号。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from harness import (  # noqa: E402
    CATEGORIES,
    CATEGORY_LABELS,
    TIER_LABELS,
    EvalTask,
    aggregate_runs,
    apply_reference,
    baseline_path,
    clean_workspace,
    comparability,
    eval_root,
    format_table,
    git_state,
    judge,
    materialize,
    preflight,
    run_task,
    sandbox_capabilities,
    summarize,
    task_root,
)
from tasks import TASKS  # noqa: E402

from coding_agent.config import get_settings  # noqa: E402
from coding_agent.sandbox.fs import SandboxFs  # noqa: E402
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace  # noqa: E402


def _select(only: str | None) -> list[EvalTask]:
    if not only:
        return list(TASKS)
    # 支持按类别（single_file）、档位（deep）或任务 id 筛选
    chosen = [t for t in TASKS if t.category == only or t.tier == only or t.id == only]
    if not chosen:
        chosen = [t for t in TASKS if only in t.id]
    if not chosen:
        raise SystemExit(f"没有匹配的任务：{only}")
    return chosen


def _list() -> None:
    print(f"共 {len(TASKS)} 个任务\n")
    for category in CATEGORIES:
        rows = [t for t in TASKS if t.category == category]
        print(f"[{category}] {CATEGORY_LABELS[category]}（{len(rows)}）")
        for task in rows:
            print(f"  - {task.id}")
        print()


def _verify_seeds(tasks: list[EvalTask], sandbox: WslSandbox, fs: SandboxFs, settings) -> int:
    """双向自检：种子必须**不通过**，参考解必须**通过**。

    两个方向挡的是两类相反的错误，都会让 baseline 失真：

    - **种子就通过** → 假阳性。任务什么都没测，却把通过率抬上去。
    - **参考解也不通过** → 假阴性。任务根本无解（典型成因：可见测试与新需求
      互相矛盾），把做对的智能体判成失败。

    只做前一半是不够的 —— 后者更隐蔽，因为它看起来像"智能体没做出来"。
    """
    bad: list[str] = []
    base = eval_root(settings, sandbox)
    for task in tasks:
        root = task_root(settings, sandbox, task.id)
        clean_workspace(sandbox, root, base=base)
        materialize(task, fs, root)
        passed, detail, _ = judge(task, sandbox, fs, root)
        if passed:
            bad.append(f"{task.id}：种子状态下判定就已通过（假阳性）\n    {detail}")
            print(f"  {task.id:<34} 种子已解决(!!)")
            continue

        if not task.reference:
            print(f"  {task.id:<34} 未解决  · 参考解未提供")
            continue

        ref_root = f"{base}/_ref/{task.id}"
        clean_workspace(sandbox, ref_root, base=base)
        materialize(task, fs, ref_root)
        apply_reference(task, fs, ref_root)
        ref_passed, ref_detail, _ = judge(task, sandbox, fs, ref_root)
        if ref_passed:
            print(f"  {task.id:<34} 未解决  · 参考解通过")
        else:
            bad.append(f"{task.id}：参考解也没通过判定（任务不可满足）\n    {ref_detail}")
            print(f"  {task.id:<34} 未解决  · 参考解失败(!!)")

    print()
    if bad:
        print("以下任务有问题，必须先修好任务本身：")
        for item in bad:
            print(f"  - {item}")
        return 1
    checked = sum(1 for t in tasks if t.reference)
    print(f"双向自检通过：{len(tasks)} 个任务种子均未解决；{checked} 个提供了参考解且都能通过。")
    return 0


async def _run_all(
    tasks: list[EvalTask], sandbox: WslSandbox, fs: SandboxFs, settings
) -> dict:
    results = []
    for task in tasks:
        print(f"→ {task.id} ...", flush=True)
        result = await run_task(
            task, settings, sandbox, fs, task_root(settings, sandbox, task.id)
        )
        results.append(result)
        mark = {"passed": "通过", "failed": "未通过", "error": "无效"}[result.verdict]
        print(
            f"  {mark}（工具调用 {result.tool_calls}，验证 "
            f"{_short_mechanism(result)}，{result.agent_seconds:.1f}s）"
        )

    print()
    print(format_table(results))
    summary = summarize(results)
    _print_summary(summary)

    summary["recorded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    summary["model"] = settings.deepseek_model
    summary.update(git_state())
    return summary  # type: ignore[return-value]


def _short_mechanism(result) -> str:  # noqa: ANN001 - TaskResult，避免循环导入注解
    if not result.verifications:
        return "未触发"
    return "+".join(f"{k}×{v}" for k, v in sorted(result.verifications.items()))


def _print_summary(summary: dict) -> None:
    print(f"\n通过率：{summary['passed']}/{summary['total']} = {summary['pass_rate']:.0%}")
    print("  按类别：")
    for category, stats in summary["by_category"].items():
        print(f"    {CATEGORY_LABELS[category]:<10} {stats['passed']}/{stats['total']}")
    print("  按档位（看区分度）：")
    for tier, stats in summary["by_tier"].items():
        rate = stats["passed"] / stats["total"] if stats["total"] else 0
        print(f"    {TIER_LABELS[tier]:<10} {stats['passed']}/{stats['total']}  = {rate:.0%}")

    mechanism = summary.get("mechanism") or {}
    if mechanism:
        print("  机制（这些回路真的被走过吗）：")
        print(f"    验证：{mechanism.get('verifications') or '{}'}"
              f"（{mechanism.get('tasks_with_verification', 0)} 个任务触发过）")
        print(f"    修复：{mechanism.get('repairs', 0)} 次"
              f"（{mechanism.get('tasks_with_repair', 0)} 个任务）"
              f"   重规划：{mechanism.get('replans', 0)} 次")
        tokens = (
            f"in {mechanism['input_tokens']} / out {mechanism['output_tokens']}"
            if mechanism.get("input_tokens")
            else "未回报"
        )
        print(f"    token：{tokens}"
              f"（{mechanism.get('tasks_reporting_tokens', 0)}/{summary['total']} 个任务有回报）")


def _ab(
    tasks: list[EvalTask],
    sandbox: WslSandbox,
    fs: SandboxFs,
    settings,
    capabilities: dict[str, bool],
    target: Path | None,
) -> int:
    """对照实验：同一批任务跑两遍，只差「开不开重规划」。

    重规划的主要收益是**省**（砍掉多余的步骤、少烧轮次），所以只比通过率是不够的 ——
    两栏都要看：通过率变化说明"做不做得成"，步数与工具调用变化说明"做得省不省"。
    """
    results = {}
    for label, max_replans in (("开", 2), ("关", 0)):
        arm = settings.model_copy(update={"max_replans": max_replans})
        print(f"\n===== 重规划{label}（max_replans={max_replans}）=====\n")
        results[label] = asyncio.run(_run_all(tasks, sandbox, fs, arm))
        results[label]["sandbox_capabilities"] = capabilities
        results[label]["max_replans"] = max_replans

    on, off = results["开"], results["关"]
    print("\n===== 对照 =====")
    print(f"{'任务':<34} {'开:结果/调用/步':<20} {'关:结果/调用/步':<20}")
    print("-" * 78)
    by_id = {t["id"]: t for t in off["tasks"]}
    for row in on["tasks"]:
        other = by_id.get(row["id"], {})
        left = f"{row['verdict']}/{row['tool_calls']}/{row['steps']}"
        right = (
            f"{other.get('verdict', '?')}/{other.get('tool_calls', '?')}"
            f"/{other.get('steps', '?')}"
        )
        print(f"{row['id']:<34} {left:<20} {right:<20}")
    print()
    print(f"通过率：开 {on['passed']}/{on['total']}   关 {off['passed']}/{off['total']}")
    print(f"工具调用：开 {sum(t['tool_calls'] for t in on['tasks'])}"
          f"   关 {sum(t['tool_calls'] for t in off['tasks'])}")
    print(f"总步数：开 {sum(t['steps'] for t in on['tasks'])}"
          f"   关 {sum(t['steps'] for t in off['tasks'])}")
    print(f"重规划次数：开 {sum(t['replans'] for t in on['tasks'])}   关 0（已关闭）")

    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"replan_on": on, "replan_off": off}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n对照结果已写入 {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端能力评测")
    parser.add_argument("--list", action="store_true", help="列出任务")
    parser.add_argument(
        "--verify-seeds", action="store_true", help="只跑判定，确认种子都未解决（不需要 API Key）"
    )
    parser.add_argument("--only", help="只看/只跑某个类别（single_file）、档位（deep）或任务 id")
    parser.add_argument("--baseline", type=Path, help="baseline 输出路径")
    parser.add_argument(
        "--no-replan", action="store_true", help="关掉重规划（AGENT_MAX_REPLANS=0），用于对照实验"
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        help="对照实验：同一批任务跑两遍（开/关重规划）并打印差值",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="K",
        help="整批任务重复 K 次，用于量化运行间噪声（默认 1；成本是 K 倍）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使这份数字不可比也写入 baseline（会标记 comparable=false）",
    )
    args = parser.parse_args(argv)

    if args.list:
        _list()
        return 0

    settings = get_settings()
    if not WslSandbox.available(settings.wsl_distro):
        print(f"WSL 发行版 {settings.wsl_distro} 不可用，评测无法进行。")
        return 2
    sandbox = WslSandbox(settings)
    fs = SandboxFs(sandbox, resolve_workspace(settings, sandbox))
    base = eval_root(settings, sandbox)
    tasks = _select(args.only)

    # 先证明判定能通过，再谈任何通过率。否则可能得到一个"看上去干净、
    # 实际什么都没测出来"的结果（沙箱缺 pytest 时就会这样）。
    try:
        preflight(sandbox, fs, base)
    except RuntimeError as exc:
        print(exc)
        return 2

    capabilities = sandbox_capabilities(sandbox)
    print(f"判定自检通过。沙箱运行器：{'、'.join(k for k, v in capabilities.items() if v)}")
    if not capabilities.get("pytest"):
        print(
            "注意：沙箱里没有 pytest，智能体的自动验证对 Python 项目不会生效 ——\n"
            "      本次测到的是「没有修复循环」的智能体。"
        )
    print()

    if args.verify_seeds:
        print(f"种子自检（{len(tasks)} 个任务，不调用模型）\n")
        return _verify_seeds(tasks, sandbox, fs, settings)

    if not settings.deepseek_api_key:
        print("未配置 DEEPSEEK_API_KEY，无法跑评测。可先用 --verify-seeds 做种子自检。")
        return 2

    # 可比性守卫放在**开跑之前**：三条理由（子集 / 脏工作区 / 沙箱无 pytest）
    # 此刻全都已知，没有任何理由先烧掉十分钟的模型调用再拒绝。
    reasons = comparability(
        full_set=args.only is None, capabilities=capabilities, git=git_state()
    )
    writing_default = args.baseline is None and not args.ab
    if reasons and writing_default and not args.force:
        print("\n拒绝跑评测 —— 这份数字会不可比：")
        for reason in reasons:
            print(f"  - {reason}")
        print(
            "\n改法：\n"
            "  --force                     强制跑并写入（标记 comparable=false 并写明原因）\n"
            "  --baseline <路径>           写到别处，不覆盖 canonical baseline\n"
            "  git stash / 提交改动         让工作区干净（最推荐）"
        )
        return 2
    if reasons:
        print("注意：本次结果**不可比**（写入时会标记 comparable=false）：")
        for reason in reasons:
            print(f"  - {reason}")
        print()

    print(f"评测目录：{eval_root(settings, sandbox)}")
    print(f"模型：{settings.deepseek_model}\n")

    if args.ab:
        return _ab(tasks, sandbox, fs, settings, capabilities, args.baseline)

    if args.no_replan:
        settings = settings.model_copy(update={"max_replans": 0})

    repeats = max(args.repeat, 1)
    if repeats == 1:
        print(
            f"提示：单次运行的分辨率是 1 个任务（约 {1 / len(tasks):.1%}）。"
            "比它更小的差异无法与运行间噪声区分 ——\n"
            "      要看噪声地板请用 --repeat 2 以上（成本随之翻倍）。\n"
        )

    runs: list[dict] = []
    for index in range(repeats):
        if repeats > 1:
            print(f"\n===== 第 {index + 1}/{repeats} 次 =====\n")
        runs.append(asyncio.run(_run_all(tasks, sandbox, fs, settings)))
    summary = aggregate_runs(runs)

    if repeats > 1:
        noise = summary["noise"]
        print(
            f"\n噪声：逐次通过率 {noise['pass_rate_per_run']}，波动 {noise['spread']:.1%}；"
            f"翻转过的任务：{noise['flipped'] or '无'}"
        )

    summary["sandbox_capabilities"] = capabilities
    summary["max_replans"] = settings.max_replans
    # 配置戳：review 会改变 repairs / replans 这些机制指标，开关两态的数字不可比。
    # 与 max_replans 一样属于"必须一起报"的元信息。
    summary["review_enabled"] = settings.review_enabled
    summary["comparable"] = not reasons
    summary["comparable_reason"] = "；".join(reasons)

    target = args.baseline or baseline_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nbaseline 已写入 {target}")
    return 0 if summary["pass_rate"] >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
