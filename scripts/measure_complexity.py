"""静态复杂度度量（零依赖，只用标准库）。

docs/COMPLEXITY.md 里的数字必须能从源码重算，否则就是第二份会腐烂的手抄本。
本脚本是那组数字的事实源：

    python scripts/measure_complexity.py            # 文本报告
    python scripts/measure_complexity.py --json     # 机器可读

口径
----
- 规模：`src/coding_agent/**/*.py` 的总行数（含空行与注释）。
- 函数：`FunctionDef` / `AsyncFunctionDef`，嵌套函数各自计数。
- 圈复杂度：McCabe 近似 —— 基线 1，`if`/`for`/`while`/`except`/`assert`/
  `if` 表达式/推导式 `if` 各 +1，布尔表达式按操作数个数减一累加。
- 测试规模：`tests/**/*.py`；用例数用 pytest 实跑结果，不在此重复统计。

注意：这是**快照**，不是阈值门禁。数字变化不等于回退，判断要看热点与趋势。
"""

from __future__ import annotations

import argparse
import ast
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "coding_agent"
TESTS = ROOT / "tests"


def _cc(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """单函数的圈复杂度近似值。嵌套函数由调用方单独统计。"""

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.value = 1

        def _skip_nested(self, node: ast.AST) -> None:
            if node is fn:
                self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._skip_nested(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._skip_nested(node)

        def visit_If(self, node: ast.If) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_IfExp(self, node: ast.IfExp) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            self.value += len(node.values) - 1
            self.generic_visit(node)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            self.value += len(node.ifs)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(fn)
    return visitor.value


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())


def collect() -> dict:
    files = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
    functions: list[dict] = []
    total_lines = 0
    for path in files:
        total_lines += _lines(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(
                    {
                        "file": path.relative_to(ROOT).as_posix(),
                        "name": node.name,
                        "line": node.lineno,
                        "length": (node.end_lineno or node.lineno) - node.lineno + 1,
                        "cc": _cc(node),
                    }
                )

    test_files = sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)
    test_lines = sum(_lines(p) for p in test_files)

    lengths = [f["length"] for f in functions]
    ccs = [f["cc"] for f in functions]
    file_lines = Counter({p.relative_to(ROOT).as_posix(): _lines(p) for p in files})

    return {
        "measured_at": "repo snapshot",
        "source": {
            "files": len(files),
            "lines": total_lines,
            "functions": len(functions),
        },
        "function_length": {
            "median": statistics.median(lengths),
            "mean": round(statistics.mean(lengths), 1),
            "over_40_pct": round(100 * sum(x > 40 for x in lengths) / len(lengths), 1),
        },
        "cyclomatic": {
            "median": statistics.median(ccs),
            "mean": round(statistics.mean(ccs), 1),
            "over_10_pct": round(100 * sum(x > 10 for x in ccs) / len(ccs), 1),
            "max": max(ccs),
        },
        "tests": {
            "files": len(test_files),
            "lines": test_lines,
            "ratio": round(test_lines / total_lines, 2),
        },
        "top_cc": sorted(functions, key=lambda f: (-f["cc"], -f["length"]))[:12],
        "top_files": [
            {"file": p, "lines": n} for p, n in file_lines.most_common(10)
        ],
    }


def render(data: dict) -> str:
    s, fl, cy, t = (
        data["source"],
        data["function_length"],
        data["cyclomatic"],
        data["tests"],
    )
    out = [
        f"source files {s['files']} | source lines {s['lines']:,} | functions {s['functions']}",
        f"function length: median {fl['median']:.0f} / mean {fl['mean']}"
        f" / >40 lines {fl['over_40_pct']}%",
        f"cyclomatic: median {cy['median']:.0f} / mean {cy['mean']} / >10 {cy['over_10_pct']}%"
        f" / max {cy['max']}",
        f"tests: {t['files']} files / {t['lines']:,} lines / test:source {t['ratio']}:1",
        "",
        "top cyclomatic complexity:",
    ]
    for f in data["top_cc"]:
        out.append(f"  CC {f['cc']:>3} | {f['length']:>4} lines  {f['file']}::{f['name']}")
    out.append("")
    out.append("largest files:")
    for f in data["top_files"]:
        out.append(f"  {f['lines']:>5} lines  {f['file']}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="静态复杂度度量（零依赖）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
