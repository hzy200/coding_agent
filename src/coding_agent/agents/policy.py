"""委派的成本策略：根据仓库规模与当前步骤决定「能不能委派、最多几次」。

A/B 的教训：38 个文件的长程任务里，模型会一次拆 3–5 个子智能体；探索本身
确实有用，但每个子智能体都要独立跑一轮 LLM + 多次只读工具，成本直接翻倍。
所以委派不能只看「开关是否打开」，还要看：

- **仓库规模**：小仓库直接读更便宜；只有跨模块的大仓库才值得拆。
- **当前步骤类型**：只有 `kind=explore` 的步骤允许委派；实现/验证步自己完成。
- **任务级预算**：按规模给 0 / 2 / 4 / 6 次，而不是一律给最大值。

探测失败（profile 未知）时**不关闭**功能：这通常意味着沙箱本身有问题，
此时再用规模阈值去限制只会让 agent 更早卡死。保持配置的最大值，让上层
的通用错误处理去暴露真正的问题。
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox


@dataclass(frozen=True, slots=True)
class RepoProfile:
    """仓库的轻量画像。`file_count == 0` 表示没探到（未知，不是空仓库）。"""

    file_count: int = 0
    test_file_count: int = 0

    @property
    def known(self) -> bool:
        return self.file_count > 0


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    """一次运行的委派策略。"""

    allowed: bool
    max_calls: int
    reason: str


# 一条命令同时数「全部文件」与「测试文件」。排除的目录与检索工具的口径一致。
_PROFILE_SCRIPT = r"""\
total=$(find . -type f \
  -not -path './.git/*' -not -path './.agent/*' \
  -not -path '*/__pycache__/*' -not -path '*/node_modules/*' \
  -not -path '*/.venv/*' 2>/dev/null | wc -l)
tests=$(find . -type f \( -name 'test_*.py' -o -name '*_test.py' \) \
  -not -path './.git/*' -not -path './.agent/*' \
  -not -path '*/__pycache__/*' 2>/dev/null | wc -l)
echo "$total $tests"
"""


def profile_workspace(sandbox: WslSandbox, root: str) -> RepoProfile:
    """一次沙箱调用拿到文件数；失败时返回未知画像。"""
    try:
        result = sandbox.run(_PROFILE_SCRIPT, cwd=root)
    except Exception:  # noqa: BLE001 - 画像失败不能拖垮 run()
        return RepoProfile()
    if not result.ok:
        return RepoProfile()
    parts = result.stdout.split()
    if len(parts) < 2:
        return RepoProfile()
    try:
        return RepoProfile(file_count=int(parts[0]), test_file_count=int(parts[1]))
    except ValueError:
        return RepoProfile()


def delegation_policy(profile: RepoProfile, settings: Settings) -> DelegationPolicy:
    """按仓库规模计算委派预算。"""
    if not settings.explore_enabled:
        return DelegationPolicy(False, 0, "explore disabled")

    if not profile.known:
        # 探测失败：不因规模判断而误伤功能
        return DelegationPolicy(
            True, settings.max_delegate_calls, "repo size unknown"
        )

    if profile.file_count < settings.delegate_min_files:
        return DelegationPolicy(
            False, 0, f"{profile.file_count} files < {settings.delegate_min_files}"
        )
    if profile.file_count < settings.delegate_medium_files:
        return DelegationPolicy(
            True,
            min(settings.max_delegate_calls, settings.delegate_medium_calls),
            f"medium repo ({profile.file_count} files)",
        )
    if profile.file_count < settings.delegate_large_files:
        return DelegationPolicy(
            True,
            min(settings.max_delegate_calls, settings.delegate_large_calls),
            f"large repo ({profile.file_count} files)",
        )
    return DelegationPolicy(
        True,
        settings.max_delegate_calls,
        f"very large repo ({profile.file_count} files)",
    )
