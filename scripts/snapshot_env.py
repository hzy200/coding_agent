"""环境版本快照的生成与校验。

`docs/ENVIRONMENT.md` 第 4 节是**实测快照**，不是声明依赖（后者在
`pyproject.toml` / `docs/contracts/dependencies.md`）。手抄版本号必然漂移，
所以这里提供两个动作：

    python scripts/snapshot_env.py           # 打印可直接替换的表格
    python scripts/snapshot_env.py --check   # 与文档对比，不一致时退出 1

需要在项目环境（`conda activate agent`）里运行；CI 的 docs.yml 不安装项目
依赖，因此这项检查放在本地或 `make docs`，不进 GitHub Actions。
"""

from __future__ import annotations

import importlib.metadata as metadata
import platform
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENT = ROOT / "docs" / "ENVIRONMENT.md"

# (分组, 包名) —— 与 ENVIRONMENT.md 第 4 节的表格顺序一致
PACKAGES: list[tuple[str, str]] = [
    ("运行时", "langgraph"),
    ("运行时", "langgraph-checkpoint-sqlite"),
    ("运行时", "aiosqlite"),
    ("运行时", "langchain-core"),
    ("运行时", "langchain-openai"),
    ("运行时", "langsmith"),
    ("运行时", "pydantic"),
    ("运行时", "pydantic-settings"),
    ("运行时", "typer"),
    ("运行时", "rich"),
    ("运行时", "python-dotenv"),
    ("开发", "pytest"),
    ("开发", "pytest-xdist"),
    ("开发", "pytest-cov"),
    ("开发", "ruff"),
    ("UI", "textual"),
    ("Web", "fastapi"),
    ("Web", "uvicorn"),
]

KNOWN_PACKAGES = {package for _, package in PACKAGES}
ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*$")
SUMMARY_RE = re.compile(r"Python\s+([\d.]+)\s*·\s*pip\s+([\d.]+)\s*·\s*环境内共\s*(\d+)\s*个包")


def current() -> tuple[list[tuple[str, str, str]], str, str, int]:
    rows: list[tuple[str, str, str]] = []
    for group, package in PACKAGES:
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            version = "MISSING"
        rows.append((group, package, version))
    try:
        pip_version = metadata.version("pip")
    except metadata.PackageNotFoundError:
        pip_version = "MISSING"
    package_count = len(list(metadata.distributions()))
    return rows, platform.python_version(), pip_version, package_count


def render(rows: list[tuple[str, str, str]], py: str, pip: str, count: int) -> str:
    lines = ["| 分组 | 包 | 版本 |", "|---|---|---|"]
    for group, package, version in rows:
        lines.append(f"| {group} | `{package}` | {version} |")
    lines.append("")
    lines.append(f"Python {py} · pip {pip} · 环境内共 {count} 个包。")
    return "\n".join(lines)


def parse_doc(text: str) -> tuple[dict[str, str], dict[str, str]]:
    versions: dict[str, str] = {}
    summary: dict[str, str] = {}
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match and match.group(2) in KNOWN_PACKAGES:
            versions[match.group(2)] = match.group(3)
        summary_match = SUMMARY_RE.search(line)
        if summary_match:
            summary = {
                "python": summary_match.group(1),
                "pip": summary_match.group(2),
                "packages": summary_match.group(3),
            }
    return versions, summary


def main() -> int:
    rows, py, pip, count = current()

    if "--check" not in sys.argv:
        print(render(rows, py, pip, count))
        return 0

    if not ENVIRONMENT.exists():
        print(f"MISSING: {ENVIRONMENT}")
        return 1

    documented, summary = parse_doc(ENVIRONMENT.read_text(encoding="utf-8"))
    problems: list[str] = []
    for _, package, version in rows:
        expected = documented.get(package)
        if expected is None:
            problems.append(f"NOT DOCUMENTED: {package} (actual {version})")
        elif expected != version:
            problems.append(f"MISMATCH: {package} doc={expected} actual={version}")
    if summary:
        if summary["python"] != py:
            problems.append(f"MISMATCH: python doc={summary['python']} actual={py}")
        if summary["pip"] != pip:
            problems.append(f"MISMATCH: pip doc={summary['pip']} actual={pip}")
        if summary["packages"] != str(count):
            problems.append(
                f"MISMATCH: package count doc={summary['packages']} actual={count}"
            )

    if problems:
        print("ENVIRONMENT.md 与当前环境不一致：")
        for item in problems:
            print(f"  - {item}")
        print("重生成表格：python scripts/snapshot_env.py")
        return 1
    print("ENVIRONMENT.md 与当前环境一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
