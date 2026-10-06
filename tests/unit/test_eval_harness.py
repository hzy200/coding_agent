"""评测集自身的完整性。

评测集是"能力的尺子"。尺子本身错了，后面所有"提升了 X 个百分点"都是假的，
而它的错法很隐蔽 —— **判在种子状态就通过的任务是假阳性**，会把基线抬高，
却让人误以为智能体会做。

这里的用例全部**不需要 WSL、不需要 API Key**（判定在宿主机上跑一遍，用的是
本机同一个 Python），所以它能进 CI。真实环境下的同一项自检由
`python tests/eval/runner.py --verify-seeds` 在 WSL 里再跑一次。
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from harness import (  # noqa: E402
    _NO_TESTS_RE,
    BASIC,
    CATEGORIES,
    DEEP,
    DEFAULT_JUDGE,
    EVAL_DIRNAME,
    LONG,
    SPEC,
    TIERS,
    TaskResult,
    aggregate_runs,
    clean_workspace,
    comparability,
    eval_audit_dir,
    format_table,
    mechanism_summary,
    noise_summary,
    summarize,
)
from repogen import (  # noqa: E402
    RepoSpec,
    build_repo,
    build_variant,
    changed_sources,
    file_stats,
)
from tasks import TASKS  # noqa: E402
from tasks_long import LONG_TASKS  # noqa: E402

IDS = [task.id for task in TASKS]


def _write(task, root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _write_seed(task, root: Path) -> None:
    """还原到「智能体还没动过」的状态，并摆好判定文件（含隐藏测试）。

    必须加上隐藏测试 —— 否则 spec 类任务的可见测试本来就是绿的，
    种子检查会误判成"任务在种子状态就已解决"。
    """
    _write(task, root, task.seed_files)
    _write(task, root, task.judge_files)


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------

def test_task_ids_are_unique() -> None:
    duplicates = [task_id for task_id, count in Counter(IDS).items() if count > 1]
    assert not duplicates, f"任务 id 重复：{duplicates}"


def test_every_category_has_tasks() -> None:
    present = {task.category for task in TASKS}
    assert present == set(CATEGORIES), f"类别覆盖不全：{present}"


def test_the_baseline_is_wide_enough_to_mean_something() -> None:
    """太小的集合上，一个任务的成败就是好几个百分点。

    注意默认套件里**没有** long 档 —— 长程任务单独成套件（成本高得多，
    而且波动量级不同），所以这里是 `<=` 而不是 `==`。
    """
    assert len(TASKS) >= 26, "任务总量太少"
    per_tier = Counter(task.tier for task in TASKS)
    assert set(per_tier) <= set(TIERS), f"有未登记的档位：{dict(per_tier)}"
    assert per_tier[BASIC] >= 20 and per_tier[DEEP] >= 6
    assert LONG not in per_tier, "长程档不该混进默认套件"
    per_category = Counter(task.category for task in TASKS)
    assert set(per_category) == set(CATEGORIES), f"类别覆盖不全：{dict(per_category)}"
    assert min(per_category.values()) >= 2, f"某一类任务太少：{dict(per_category)}"


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_task_is_well_formed(task) -> None:
    assert task.prompt.strip(), "任务必须有指令"
    assert task.tests, "任务必须带测试文件，否则判定无从谈起"
    assert task.sources, "任务必须带源码种子"
    assert task.tier in TIERS, f"未知档位：{task.tier}"
    # 同一个路径不能出现在两个集合里：判定前会互相覆盖，语义会含糊
    keys = [set(task.sources), set(task.tests), set(task.hidden_tests)]
    assert not (keys[0] & keys[1]), "源码与可见测试路径重叠"
    assert not (keys[1] & keys[2]), "可见测试与隐藏测试路径重叠"
    assert not (keys[0] & keys[2]), "源码与隐藏测试路径重叠"


@pytest.mark.parametrize("task", [t for t in TASKS if t.category == SPEC], ids=lambda t: t.id)
def test_spec_tasks_have_hidden_tests(task) -> None:
    """spec 类的定义就是「可见测试没覆盖需求」，所以必须有隐藏测试。

    没有隐藏测试的 spec 任务会退化成普通任务：判定只看可见测试，
    而可见测试在种子状态就是绿的 —— 它会变成一个永远"已通过"的假任务。
    """
    assert task.hidden_tests, f"{task.id} 属于 spec 类却没有隐藏测试"


# --------------------------------------------------------------------------
# 种子必须失败（评测集最容易出的错）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_task_files_are_valid_python(task) -> None:
    """先把「种子写错了」与「种子还没解决」分开 —— 语法错是静态可判的。

    没有这一条，"收集期失败"就有两种含义（模块还没建 vs 种子本身是坏的），
    而它们需要完全相反的处理。
    """
    for relpath, content in {**task.sources, **task.judge_files}.items():
        if not relpath.endswith(".py"):
            continue  # 种子可以带 README / TODO 这类非 Python 文件
        try:
            compile(content, relpath, "exec")
        except SyntaxError as exc:
            pytest.fail(f"{task.id} 的 {relpath} 有语法错误：{exc}")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_seed_is_unsolved_under_the_judge(task, tmp_path: Path) -> None:
    """在种子状态跑一遍判定，必须**不通过**。

    退出码 1 = 有用例失败；2 = 收集期就失败（例如任务要求新建的模块还不存在，
    这是合法的未解决状态）。语法错已由上一条用例挡掉，所以这里不必再区分。
    0 说明任务在种子状态就已通过 —— 假阳性，会把基线抬得虚高。
    3/4/5（内部错误 / 用法错 / 一个用例都没收集到）说明判定本身写错了。
    """
    _write_seed(task, tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    # 退出码 1 = 有用例失败或导入失败（都算"未解决"）；0 但零用例是判定失效。
    assert proc.returncode == 1 and "FAILED" in output, (
        f"{task.id} 的种子状态判定异常（退出码 {proc.returncode}）。"
        f"退出码 0 说明任务在种子状态就已通过（假阳性）；"
        f"没有 FAILED 字样说明用例根本没跑起来。\n{output[-800:]}"
    )


@pytest.mark.parametrize("task", [t for t in TASKS if t.reference], ids=lambda t: t.id)
def test_reference_solution_passes_the_judge(task, tmp_path: Path) -> None:
    """任务必须**可满足**：种子 + 参考解要能让判定通过。

    这条挡的是"可见测试与新需求互相矛盾"这类错误 —— 那样的任务无论怎么做都过不了，
    会把做对的智能体判成失败。它比"种子必须失败"更隐蔽：从结果看只是"智能体没做出来"。

    实际踩过一次：`deep/field_propagation` 的可见测试断言了旧输出格式，
    而新需求必然要改它 —— 智能体做对了，却被判失败。
    """
    _write_seed(task, tmp_path)
    _write(task, tmp_path, task.reference)

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{task.id} 的参考解没能通过判定 —— 任务不可满足。\n"
        f"{proc.stdout[-1200:]}{proc.stderr[-400:]}"
    )


def test_deep_tier_has_references() -> None:
    """进阶档必须都提供参考解。

    它们涉及多文件改动，最容易写出自相矛盾的任务；没有参考解就没有任何机制
    能发现"这任务根本做不出来"。
    """
    missing = [t.id for t in TASKS if t.tier == DEEP and not t.reference]
    assert not missing, f"进阶档缺少参考解：{missing}"


def test_zero_discovered_tests_is_not_a_pass() -> None:
    """`unittest discover` 零用例时退出码是 0 —— 这是最危险的假阳性，必须拦住。"""
    assert _NO_TESTS_RE.search("Ran 0 tests in 0.000s\n\nOK\n")
    assert not _NO_TESTS_RE.search("Ran 3 tests in 0.010s\n\nOK\n")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_judge_is_the_dependency_free_default(task) -> None:
    """判定必须零依赖：沙箱里没有 pytest / make，装了才跑得起来的判定等于没有判定。"""
    assert task.judge == DEFAULT_JUDGE
    assert "pytest" not in task.judge


# --------------------------------------------------------------------------
# 安全护栏
# --------------------------------------------------------------------------

def test_audit_dir_is_a_host_side_absolute_path() -> None:
    """审计是宿主侧组件，不能拿沙箱路径喂它。

    `Path("/home/hong/...")` 在 Windows 上 `is_absolute()` 为假（没有盘符），
    会被当成"当前盘符下的相对路径" —— 实测就是这么把审计写到 `D:\\home\\...` 去的。
    """
    assert eval_audit_dir().is_absolute()


def test_clean_refuses_paths_outside_the_eval_dir() -> None:
    """清理会 rm -rf，前缀校验必须在动沙箱之前就拦住。"""
    base = f"/home/user/agent-ws/{EVAL_DIRNAME}"

    with pytest.raises(ValueError):
        clean_workspace(None, "/home/user/agent-ws", base=base)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        # 前缀相近但不在其下 —— 子串判断会放过这种
        clean_workspace(None, f"{base}extra/x", base=base)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        # 不许直接清掉评测根目录本身
        clean_workspace(None, base, base=base)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        # 目录穿越
        clean_workspace(None, f"{base}/../agent-ws", base=base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 机制指标：回答「那条路真的被走过吗」
#
# 只比通过率会得出反向结论 —— README 记着一次真实误读：`--ab` 跑出
# 「开 3/3 对 关 2/3」，看着像重规划有效，实际 replans=0（节点一次都没执行），
# 那 33 个百分点纯是运行间噪声。
# --------------------------------------------------------------------------

def _result(task_id: str, verdict: str = "passed", **kwargs) -> TaskResult:
    return TaskResult(
        task_id=task_id, category="debug", tier=BASIC, verdict=verdict, **kwargs
    )


def test_mechanism_distinguishes_executed_from_skipped_verification() -> None:
    """`not_configured` 与 `ok` 必须分得开。

    前者意味着**验证根本没生效**（识别不出测试命令），而通过率本身看不出来 ——
    「验证跑过了」和「验证以为没什么可跑」在通过率上是同一张脸。
    """
    summary = mechanism_summary([
        _result("a", verifications={"ok": 2}),
        _result("b", verifications={"not_configured": 3}),
        _result("c", verifications={"failed": 1, "ok": 1}, repairs=2),
    ])

    assert summary["verifications"] == {"failed": 1, "not_configured": 3, "ok": 3}
    assert summary["tasks_with_verification"] == 3
    assert summary["repairs"] == 2
    assert summary["tasks_with_repair"] == 1


def test_mechanism_counts_only_tasks_that_reported_tokens() -> None:
    """部分缺失必须看得见，且**不能把 None 当 0**。

    把没回报的任务按 0 计入，会让均值系统性偏低 —— 而缺回报的多半是失败路径，
    也就是花费最多的那批。
    """
    summary = mechanism_summary([
        _result("a", input_tokens=100, output_tokens=10),
        _result("b", input_tokens=50, output_tokens=5),
        _result("c"),  # provider 没回报
    ])

    assert summary["input_tokens"] == 150
    assert summary["output_tokens"] == 15
    assert summary["tasks_reporting_tokens"] == 2


def test_mechanism_reports_none_when_nobody_reported() -> None:
    """一个都没回报时是 None，不是 0 —— 「没量到」与「量到零」含义不同。"""
    summary = mechanism_summary([_result("a")])
    assert summary["input_tokens"] is None
    assert summary["output_tokens"] is None


def test_summary_carries_mechanism_and_per_task_detail() -> None:
    summary = summarize([
        _result("a", verifications={"ok": 1}, repairs=1, input_tokens=7),
    ])
    assert summary["mechanism"]["verifications"] == {"ok": 1}
    task = summary["tasks"][0]
    assert task["verifications"] == {"ok": 1}
    assert task["repairs"] == 1
    assert task["input_tokens"] == 7


# --------------------------------------------------------------------------
# 噪声地板
# --------------------------------------------------------------------------

def _run_with(verdicts: dict[str, list[str]]) -> list[dict]:
    """把 {任务: [各次结果]} 摊成逐次运行的 summary。"""
    repeats = len(next(iter(verdicts.values())))
    return [
        summarize([_result(tid, verdicts[tid][i]) for tid in verdicts])
        for i in range(repeats)
    ]


def test_noise_counts_repeats_and_one_task_granularity() -> None:
    """`one_task_flip` 是算术粒度，**不是**分辨率 —— 两者别混。"""
    runs = _run_with({"a": ["passed", "passed"], "b": ["failed", "failed"]})
    noise = noise_summary(runs)

    assert noise["repeats"] == 2
    assert noise["pass_rate_per_run"] == [0.5, 0.5]
    assert noise["flipped"] == []
    assert noise["one_task_flip"] == 0.5  # 2 个任务
    assert noise["spread"] == 0.0


def test_noise_floor_is_the_spread_not_the_task_granularity() -> None:
    """能分辨的最小差异由**波动**决定，而不是"翻一个任务"。

    实测过的那组：同一份代码两遍是 52% 与 83%。29 个任务时 one_task_flip 只有
    3.4 个百分点，看着尺子很精细；但真实波动是 31 个百分点 —— 拿 3.4 去读变化，
    读到的全是运气。把这两个数放在一起报，才不会误读。
    """
    runs = _run_with({
        # 前 11 个任务第 1 轮挂、第 2 轮过 —— 与实测那组的形状一致
        f"t{i}": (["failed", "passed"] if i < 11 else ["passed", "passed"])
        for i in range(29)
    })
    noise = noise_summary(runs)

    assert noise["one_task_flip"] == round(1 / 29, 4)
    # 第 1 轮 18/29（62%），第 2 轮 29/29（100%）
    assert noise["pass_rate_per_run"] == [round(18 / 29, 4), 1.0]
    assert noise["spread"] == round(11 / 29, 4)
    assert noise["spread"] > noise["one_task_flip"] * 5
    assert len(noise["flipped"]) == 11


def test_noise_exposes_flips_that_cancel_out() -> None:
    """两次运行通过率完全相同、但**成员不同** —— 这正是噪声的典型形态。

    只看通过率会得出"没有任何变化"；`flipped` 才告诉你这两个任务的结果
    取决于运气。
    """
    runs = _run_with({"a": ["passed", "failed"], "b": ["failed", "passed"]})
    noise = noise_summary(runs)

    assert noise["pass_rate_per_run"] == [0.5, 0.5]
    assert noise["spread"] == 0.0
    assert noise["flipped"] == ["a", "b"]


def test_aggregate_single_run_stays_comparable_with_old_schema() -> None:
    """k=1 时保持原样：老 baseline 的消费方不会被新 schema 打断。"""
    summary = aggregate_runs([_run_with({"a": ["passed"]})[0]])
    assert summary["aggregation"] == "single_run"
    assert summary["pass_rate"] == 1.0


def test_aggregate_merges_mechanism_across_runs() -> None:
    """机制指标也必须跨轮合并 —— 这条踩过。

    聚合时漏了 `mechanism`，于是那份 baseline 里**通过率是两轮的均值、机制却只是
    第 1 轮的**：两个数字口径不同却长得一样。数不清口径的指标比没有指标更糟。
    """
    runs = [
        summarize([_result("a", verifications={"ok": 1, "failed": 2}, repairs=3,
                           input_tokens=100, output_tokens=10)]),
        summarize([_result("a", verifications={"ok": 4}, repairs=1,
                           input_tokens=50, output_tokens=5)]),
    ]
    merged = aggregate_runs(runs)["mechanism"]

    assert merged["runs"] == 2
    assert merged["verifications"] == {"failed": 2, "ok": 5}  # 求和，不是取平均
    assert merged["repairs"] == 4
    assert merged["input_tokens"] == 150
    assert merged["output_tokens"] == 15


def test_single_run_mechanism_carries_its_run_count() -> None:
    """k=1 也带 `runs`，两种口径形状一致 —— 消费方不必分情况处理。"""
    merged = aggregate_runs(_run_with({"a": ["passed"]}))["mechanism"]
    assert merged["runs"] == 1
    assert merged["input_tokens"] is None  # 一条都没回报时是 None，不是 0


def test_aggregate_marks_flaky_tasks_as_their_own_verdict() -> None:
    """忽过忽不过的任务不能混进 passed 或 failed。

    混进去会掩盖「这个任务的结论取决于运气」，而那是最该被看见的信息。
    """
    summary = aggregate_runs(_run_with(
        {"stable": ["passed", "passed"], "coin": ["passed", "failed"]}
    ))

    assert summary["aggregation"] == "mean_over_repeats"
    by_id = {t["id"]: t for t in summary["tasks"]}
    assert by_id["stable"]["verdict"] == "passed"
    assert by_id["stable"]["passes"] == 2
    assert by_id["coin"]["verdict"] == "flaky"
    assert by_id["coin"]["passes"] == 1


def test_table_marks_not_configured_verification_visibly() -> None:
    """`nc` 必须在表格里看得见 —— 它意味着验证脊柱没生效。"""
    table = format_table([_result("a", verifications={"not_configured": 1})])
    assert "nc" in table
    assert "未触发" not in table


# --------------------------------------------------------------------------
# 可比性守卫
# --------------------------------------------------------------------------

def test_comparability_passes_on_a_clean_full_run() -> None:
    assert comparability(
        full_set=True, capabilities={"pytest": True}, git={"git_dirty": False}
    ) == []


@pytest.mark.parametrize(
    ("full_set", "capabilities", "git", "needle"),
    [
        (False, {"pytest": True}, {"git_dirty": False}, "子集"),
        (True, {"pytest": True}, {"git_dirty": True}, "未提交改动"),
        (True, {"pytest": False}, {"git_dirty": False}, "pytest"),
    ],
)
def test_comparability_rejects_each_reason(full_set, capabilities, git, needle) -> None:
    """三种情况都会让通过率说不清含义。

    其中"无 pytest"最隐蔽：它不改变任何数字，只是让数字的含义变成
    「没有修复循环的智能体」。
    """
    reasons = comparability(full_set=full_set, capabilities=capabilities, git=git)
    assert len(reasons) == 1
    assert needle in reasons[0]


def test_comparability_lists_every_reason_not_just_the_first() -> None:
    """要一次把话说全，否则修掉第一条又撞上第二条，来回几轮。"""
    reasons = comparability(
        full_set=False, capabilities={"pytest": False}, git={"git_dirty": True}
    )
    assert len(reasons) == 3


# --------------------------------------------------------------------------
# 长程档：几十个文件的仓库
#
# 这一档的判定走**显式模块名**而不是 `unittest discover`。discover 在多层包的
# 大仓库上会踩一串问题（同名 basename、子目录可导入性、收集顺序），最要命的是
# **智能体写在 tests/ 里的临时测试会被一并收进来** —— 而 prepare_for_judging
# 只删根级 test_*.py。收窄到指定模块，这一整类风险就结构性地消失了。
# --------------------------------------------------------------------------

LONG_IDS = [task.id for task in LONG_TASKS]
GENERATOR_SPEC = RepoSpec()


def _judge_modules(task) -> list[str]:
    """从判定命令里取出模块名（把 `python3` 换成当前解释器）。"""
    return [part for part in task.judge.split() if part.startswith("tests.")]


def _run_judge(
    task, root: Path, *, modules: list[str] | None = None
) -> subprocess.CompletedProcess:
    args = [sys.executable, "-m", "unittest", "-v", *(modules or _judge_modules(task))]
    return subprocess.run(
        args, cwd=root, capture_output=True, text=True, timeout=180, check=False
    )


@pytest.mark.parametrize("task", LONG_TASKS, ids=LONG_IDS)
def test_long_tasks_are_well_formed(task) -> None:
    assert task.tier == LONG
    assert task.prompt.strip() and task.sources and task.tests
    # 长程档必须能证明可满足 —— 多文件改动最容易写出自相矛盾的任务
    assert task.reference, f"{task.id} 长程档必须提供参考解"
    assert task.agent_timeout > 0, "长程任务必须有墙钟护栏"
    keys = [set(task.sources), set(task.tests), set(task.hidden_tests)]
    assert not (keys[0] & keys[1]) and not (keys[1] & keys[2])


@pytest.mark.parametrize("task", LONG_TASKS, ids=LONG_IDS)
def test_long_tasks_judge_by_explicit_modules(task) -> None:
    """判定必须显式列模块，且零依赖。"""
    assert "discover" not in task.judge, f"{task.id} 不该用 discover 判定大仓库"
    assert "pytest" not in task.judge
    assert _judge_modules(task), f"{task.id} 的判定命令里没有 tests.* 模块"


def test_the_long_repo_is_actually_large() -> None:
    """「几十个文件」是这个档位存在的理由，不是装饰。"""
    stats = file_stats(build_variant(GENERATOR_SPEC))
    assert stats["files"] >= 30, f"仓库只有 {stats['files']} 个文件，量级不够"
    assert stats["lines"] >= 800, f"仓库只有 {stats['lines']} 行"


def test_the_generator_is_deterministic() -> None:
    """同一份规格必须逐字节生成同一份仓库 —— 否则基线不可复现。"""
    assert build_variant(GENERATOR_SPEC).files == build_variant(GENERATOR_SPEC).files
    assert (
        build_variant(GENERATOR_SPEC, features=("discount",)).files
        == build_variant(GENERATOR_SPEC, features=("discount",)).files
    )


def test_generator_patches_must_match_exactly_once() -> None:
    """`patch()` 的锚点必须恰好出现一次。

    这是整个生成方案的结构性保障：模板改了而功能线没跟着改时，补丁会**静默失效**
    （功能没接上），而"种子必挂 / 参考解必过"这两条会随之静默走样。
    """
    files = build_repo(GENERATOR_SPEC)
    files.patch("app/models.py", "class Order:", "class Order:  # patched")
    assert "# patched" in files["app/models.py"]

    with pytest.raises(AssertionError, match="恰好 1 次"):
        files.patch("app/models.py", "不存在的锚点", "x")
    with pytest.raises(AssertionError, match="恰好 1 次"):
        files.patch("app/models.py", "def ", "x")  # 出现多次


def test_the_feature_spans_several_layers() -> None:
    """功能增量必须**贯通多层**，否则量不出长程。

    少于 5 个文件就说明它退化成"改一两行"了 —— 那正是这一档要避免的。
    """
    seed = build_variant(GENERATOR_SPEC)
    reference = build_variant(GENERATOR_SPEC, features=("discount",))
    changed = changed_sources(seed, reference)
    assert len(changed) >= 5, f"折扣功能只改了 {len(changed)} 个文件，太短"


@pytest.mark.parametrize("task", LONG_TASKS, ids=LONG_IDS)
def test_long_seed_is_unsolved_under_its_own_judge(task, tmp_path: Path) -> None:
    """种子状态必须不通过（含隐藏测试）。"""
    _write_seed(task, tmp_path)
    proc = _run_judge(task, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "FAILED" in output, (
        f"{task.id} 的种子状态判定异常（退出码 {proc.returncode}）\n{output[-800:]}"
    )


@pytest.mark.parametrize("task", LONG_TASKS, ids=LONG_IDS)
def test_long_reference_solution_passes_its_own_judge(task, tmp_path: Path) -> None:
    """种子 + 参考解必须通过 —— 挡"任务根本无解"这类假阴性。"""
    _write_seed(task, tmp_path)
    _write(task, tmp_path, task.reference)
    proc = _run_judge(task, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{task.id} 参考解没通过判定（任务不可满足）\n{output[-1200:]}"


@pytest.mark.parametrize(
    "task", [t for t in LONG_TASKS if t.hidden_tests], ids=lambda t: t.id
)
def test_long_spec_visible_tests_are_green_on_the_seed(task, tmp_path: Path) -> None:
    """spec 类的前提：**可见测试在种子态全绿**。

    否则它就不是"可见测试全绿、需求未满足"，而是普通的"测试是红的"任务 ——
    那样测的是"让测试变绿"，而不是"照着需求做对"。
    """
    _write(task, tmp_path, task.seed_files)
    visible = [m for m in _judge_modules(task) if "hidden" not in m]
    proc = _run_judge(task, tmp_path, modules=visible)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{task.id} 的可见测试在种子态就是红的\n{output[-800:]}"


def test_long_tasks_have_distinct_ids_and_a_tier_that_shows_up() -> None:
    assert len(set(LONG_IDS)) == len(LONG_IDS)
    assert {t.tier for t in LONG_TASKS} == {LONG}
    assert "long" in TIERS and LONG == "long"


@pytest.mark.parametrize("task", LONG_TASKS, ids=LONG_IDS)
def test_long_budget_overrides_reach_real_settings_fields(task) -> None:
    """预算覆盖的键名必须真的存在 —— 写错不会报错，只会**静默不生效**。

    `Settings.model_copy(update=...)` 不做校验，塞一个不存在的键只会多出一个
    无用的属性，而预算看起来"配了"其实没生效。这种静默失效正是这一轮在治的病。
    """
    from coding_agent.config import Settings

    assert task.budget, f"{task.id} 长程任务必须显式声明预算（探路实测它会撞上限）"
    unknown = set(task.budget) - set(Settings.model_fields)
    assert not unknown, f"{task.id} 的预算里有不存在的配置项：{sorted(unknown)}"
    for name, value in task.budget.items():
        assert isinstance(value, int) and value > 0, f"{task.id} 的 {name}={value!r} 不合法"


def test_long_budget_is_larger_than_the_default() -> None:
    """抬预算是为了把「被预算卡住」与「真做不成」分开，所以必须**确实更大**。"""
    from coding_agent.config import Settings

    defaults = Settings(_env_file=None)
    budget = LONG_TASKS[0].budget or {}
    for name, value in budget.items():
        assert value > getattr(defaults, name), (
            f"{name}={value} 没有超过默认值 {getattr(defaults, name)}，抬预算没有意义"
        )


def test_aggregate_merges_every_per_task_number_not_just_the_verdict() -> None:
    """`--repeat k` 时逐任务的**数值字段也必须跨轮聚合**。

    这条踩过两次：先是 `mechanism`，后是逐任务行 —— 后者只覆盖了 verdict/passes，
    其余数值留着第 1 轮的，于是一份 `--repeat 2` 的 baseline 里通过率是两轮聚合、
    逐任务的 token 与验证次数却是第 1 轮的。口径不同却长得一样。
    """
    run1 = summarize([_result("t", verifications={"failed": 3}, repairs=2, writes=4,
                              tool_calls=10, input_tokens=100)])
    run2 = summarize([_result("t", "failed", verifications={"ok": 1}, repairs=5, writes=0,
                              tool_calls=7, input_tokens=50)])
    row = aggregate_runs([run1, run2])["tasks"][0]

    assert row["verdict"] == "flaky" and row["passes"] == 1 and row["repeats"] == 2
    assert row["tool_calls"] == 17          # 求和，不是取第 1 轮的 10
    assert row["repairs"] == 7
    assert row["writes"] == 4
    assert row["verifications"] == {"failed": 3, "ok": 1}
    assert row["input_tokens"] == 150


def test_writes_distinguishes_did_nothing_from_did_it_wrong() -> None:
    """「只读不写」与「改了但没做对」在通过率上长得一样，`writes` 才分得开。

    实测里出现过前一种：模型全程只读、计划照常推进到最后一步（`dirty` 一直是
    False，verify 与 review 都短路）。两种失败的处置完全不同 —— 前者要查为什么
    不动手，后者才是能力问题。
    """
    did_nothing = _result("a", "failed", writes=0, tool_calls=15, steps=7)
    tried_and_failed = _result("b", "failed", writes=6, tool_calls=27, steps=2)

    summary = summarize([did_nothing, tried_and_failed])
    by_id = {t["id"]: t for t in summary["tasks"]}

    assert by_id["a"]["verdict"] == by_id["b"]["verdict"] == "failed"  # 通过率相同
    assert by_id["a"]["writes"] == 0 and by_id["b"]["writes"] == 6      # 但这个分得开


def test_steps_aggregates_as_a_mean_not_a_sum() -> None:
    """`steps` 是「走到了第几步」，不是「发生过几次」。

    跨轮求和会得出比任何一次计划都长的数（5 步 + 6 步 = 11 步）—— 这条踩过一次，
    当时把所有数值字段一律当成事件计数求和。
    """
    run1 = summarize([_result("t", "passed", steps=5)])
    run2 = summarize([_result("t", "passed", steps=6)])
    row = aggregate_runs([run1, run2])["tasks"][0]

    assert row["steps"] == 6  # 均值 5.5 取整，而不是 11
    assert row["repeats"] == 2


def test_the_repeated_run_keeps_its_raw_per_task_rows() -> None:
    """各轮的原始逐任务行必须留存。

    聚合口径已经出过三次错，而每次发现时原始数据都没了 —— 只能重跑或手改。
    存下来之后，改口径只需重算。
    """
    run1 = summarize([_result("a", "passed", tool_calls=10)])
    run2 = summarize([_result("a", "failed", tool_calls=3)])
    summary = aggregate_runs([run1, run2])

    assert len(summary["per_run"]) == 2
    assert [row["tool_calls"] for row in summary["per_run"][0]] == [10]
    assert [row["verdict"] for row in summary["per_run"][1]] == ["failed"]


def test_the_baseline_file_itself_does_not_count_as_dirty() -> None:
    """要写入的基线文件不算"脏改动" —— 它是测量的产物，不是输入。

    少了这条会有个很别扭的死结：第一次写 baseline 把树弄脏，于是"再测另一个
    套件"永远被拒。踩过一次，白跑一轮（约 550 万 token）。
    """
    from harness import _dirty_entries, baseline_path

    target = str(baseline_path("long"))
    porcelain = "\n".join([" M src/coding_agent/runtime.py", f"?? {target}", ""])

    assert _dirty_entries(porcelain) != []  # 默认算脏
    # 只忽略目标文件，其余照旧
    assert _dirty_entries(porcelain, ignore=(target,)) == [" M src/coding_agent/runtime.py"]
    assert _dirty_entries(f"?? {target}\n", ignore=(target,)) == []
