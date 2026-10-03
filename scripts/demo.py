"""典型场景演示脚本。

按四个选题创新点各安排一个场景，顺序刻意递进：
只读 → 被拦 → 修复 → 回滚。

    python scripts/demo.py --list          # 只看场景说明
    python scripts/demo.py                 # 全部跑一遍
    python scripts/demo.py --only 3        # 只跑第 3 个

每个场景都会真实调用模型，会产生 API 费用，跑完大约几分钟。
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".venv" / "Scripts" / "agent.exe"
if not AGENT.exists():  # macOS / Linux
    AGENT = ROOT / ".venv" / "bin" / "agent"

# 放在 Windows 可见的临时目录下：走 /mnt/c 映射，`-C` 直接吃 Windows 路径，
# 不必用 wslpath 反查（那条路会被 WSL 自己的诊断输出污染）
DEMO_WIN = Path(tempfile.gettempdir()) / "coding-agent-demo"

BROKEN_CALC = """\
def add(a, b):
    return a + b


def average(numbers):
    # BUG：空列表会 ZeroDivisionError，且返回值没有四舍五入到 2 位
    return sum(numbers) / len(numbers)
"""

BROKEN_TEST = """\
import unittest

from calc import add, average


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 1), 2)

    def test_average(self):
        self.assertAlmostEqual(average([1, 2, 3]), 2.0)

    def test_average_empty(self):
        self.assertEqual(average([]), 0.0)
"""


SCENARIOS = [
    {
        "title": "场景 1 · 只读分析（L0 自动执行）",
        "point": "宿主内置工具、模型只输出意图",
        "prompt": "calc.py 里的 average 函数有什么问题？只做分析，不要改任何文件。",
        "extra": [],
        "watch": [
            "工具行带 [L0 只读] 标签 —— 等级由宿主判定，不采纳模型自报",
            "模型只输出结构化调用，命令由宿主在 WSL 沙箱里执行",
            "只读命令不打扰用户，直接放行",
        ],
    },
    {
        "title": "场景 2 · 命令分级拦截（L2 变更性）",
        "point": "可审计的命令安全策略",
        "prompt": "用 pip 装一个 requests 库。",
        "extra": ["--yes"],
        "extra_notes": "（--yes 只是把审批从「人工确认」降级为「自动批准」，等级判定不变）",
        "watch": [
            "pip install 被判为 L2，先过审批关卡才执行",
            "审计里记为 approved；去掉 --yes 则记为 denied",
        ],
    },
    {
        "title": "场景 3 · 失败驱动的修复循环",
        "point": "verify → repair → act",
        "prompt": "修好 average：空列表返回 0.0；非空时结果保留两位小数。改完确保测试全绿。",
        "extra": ["--write", "--yes"],
        "env": {"AGENT_VERIFY_COMMAND": "python3 -m unittest discover"},
        "watch": [
            "改完文件后自动跑验证（无需模型主动要求）",
            "验证失败 → 结构化错误回灌 → repair → 重试，上限 3 次",
            "输出里的「↻ 第 N/3 次修复」就是循环在跑",
        ],
    },
    {
        "title": "场景 4 · 回滚（可回滚的修改流程）",
        "point": "快照 + 先留底再覆盖",
        "prompt": "把 calc.py 里的 add 改成返回 a - b。",
        "extra": ["--write", "--yes"],
        "env": {"AGENT_VERIFY_ENABLED": "false"},
        "watch": [
            "编辑前自动留底到 .agent/backups/<快照id>/",
            "随后用 agent undo 一键回滚 —— 回滚本身也可回滚",
        ],
        "after": ["snapshots", "undo"],
    },
]


def _wsl(script: str) -> tuple[int, str]:
    """跑一段 WSL 脚本，返回 (退出码, stdout)。

    两个坑都在这里挡掉：

    - `wsl.exe` 的输出编码随调用环境在 UTF-16LE 与 UTF-8 之间摆动，
      直接按 UTF-8 解会留下 NUL 字节（拿去当路径就是 "embedded null character"）；
    - WSL 自己的诊断（比如 localhost 代理提示）走 stderr，不能混进 stdout，
      否则「取最后一行当路径」会取到警告文案。
    """
    from coding_agent.sandbox.wsl_exec import decode_wsl_output

    proc = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "--", "/bin/bash", "-lc", script],
        capture_output=True,
    )
    out = decode_wsl_output(proc.stdout)
    if proc.returncode != 0:
        out += "\n" + decode_wsl_output(proc.stderr)
    return proc.returncode, out


def setup() -> bool:
    """在 WSL 里铺一个带 bug 的小项目。"""
    from coding_agent.sandbox.pathguard import win_to_wsl

    root = win_to_wsl(str(DEMO_WIN))
    code, out = _wsl(
        f"rm -rf {root} && mkdir -p {root} && "
        f"cat > {root}/calc.py <<'PYEOF'\n{BROKEN_CALC}PYEOF\n"
        f"cat > {root}/test_calc.py <<'PYEOF'\n{BROKEN_TEST}PYEOF\n"
        f"echo READY"
    )
    if "READY" not in out:
        print("准备演示工作区失败：", out[:400])
        return False
    return True


def _configure_stdio() -> None:
    """Windows 的 stdout 默认走 GBK：重定向到文件/管道时，
    `↻` `·` 这类字符会直接抛 UnicodeEncodeError。

    与 `cli/app.py::_configure_stdio` 同样的处理 —— 这是第三次踩同一个坑
    （CLI、TUI 之外，脚本也要），所以写在这里时特意留了说明。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def banner(text: str, char: str = "=") -> None:
    print(f"\n{char * 72}\n{text}\n{char * 72}", flush=True)


def show_scenario(index: int, scenario: dict) -> None:
    banner(f"{scenario['title']}")
    print(f"对应创新点：{scenario['point']}", flush=True)
    print(f"\n指令：{scenario['prompt']}", flush=True)
    if scenario.get("extra_notes"):
        print(scenario["extra_notes"], flush=True)
    print("\n观察点：", flush=True)
    for item in scenario["watch"]:
        print(f"  · {item}", flush=True)
    print(flush=True)


def run_agent(args: list[str], env: dict[str, str]) -> int:
    import os

    merged = os.environ.copy()
    merged.update(env)
    merged.setdefault("DEEPSEEK_MODEL", "deepseek-chat")  # 避开思考模式的 reasoning_content 问题
    cmd = [str(AGENT), *args, "-C", DEMO_WIN]
    print(f"$ agent {' '.join(shlex.quote(a) for a in args)}\n")
    return subprocess.run(cmd, env=merged).returncode


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="CodingAgent 典型场景演示")
    parser.add_argument("--only", type=int, help="只跑第 N 个场景（从 1 开始）")
    parser.add_argument("--list", action="store_true", help="只列出场景，不执行")
    parser.add_argument("--keep", action="store_true", help="跑完保留演示工作区")
    args = parser.parse_args()

    if args.list:
        for i, scenario in enumerate(SCENARIOS, start=1):
            show_scenario(i, scenario)
        return 0

    if not AGENT.exists():
        print(f"找不到 CLI：{AGENT}\n请先 pip install -e \".[dev,ui]\"")
        return 2

    if not setup():
        return 2
    print(f"演示工作区：{DEMO_WIN}")

    selected = SCENARIOS if args.only is None else [SCENARIOS[args.only - 1]]
    offset = 0 if args.only is None else args.only - 1

    for i, scenario in enumerate(selected, start=offset + 1):
        show_scenario(i, scenario)
        # 每个场景都从干净的项目状态开始，否则上一个场景的改动会串味
        setup()
        code = run_agent(
            ["run", scenario["prompt"], *scenario["extra"]],
            scenario.get("env", {}),
        )
        if code != 0:
            print(f"（场景 {i} 退出码 {code}）")
        for sub in scenario.get("after", []):
            run_agent([sub], scenario.get("env", {}))

    if not args.keep:
        from coding_agent.sandbox.pathguard import win_to_wsl

        _wsl(f"rm -rf {win_to_wsl(str(DEMO_WIN))}")
        print("已清理演示工作区（--keep 可保留）")

    banner("演示结束", "-")
    print(textwrap.dedent("""\
        回看这四条主线：
          1. 模型只输出意图，命令由宿主在沙箱里执行
          2. 等级判定在宿主，危险命令过不去
          3. 失败会被结构化捕获并驱动修复，修不好就如实上报
          4. 每次改动都可回滚，且回滚本身也可回滚
        """))
    return 0


if __name__ == "__main__":
    sys.exit(main())
