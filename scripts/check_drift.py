"""文档—代码漂移扫描（零依赖）。

`check_docs.py` 校验文档之间的链接与契约；本脚本补另一类漂移：

1. 代码注释 / docstring 里引用的 `docs/...` 文件是否存在；
2. 文档里反引号标注的 `src/` / `tests/` / `scripts/` 代码路径是否存在。

这类引用不会被 Markdown 链接检查覆盖，但同样会变成"幽灵文件"——
读者（或 Agent）点不开、查不到，却以为有一份说明在。

用法：
    python scripts/check_drift.py
    python scripts/check_drift.py --json

只做存在性检查，不判断内容是否仍然正确。内容是否过时由 owner 与
`last_reviewed` 负责；这里只拦住"指向不存在路径"这种硬错误。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".agent",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
}

# 代码注释里引用文档（允许 .md / .yaml / .json）
DOC_REF = re.compile(r"docs/[\w./-]+\.(?:md|yaml|yml|json)")
# 文档里用反引号标注的代码路径；`::test_name` 只校验文件本身
CODE_REF = re.compile(r"`((?:src|tests|scripts)/[\w./-]+\.py)(?:::[\w:\[\]-]+)?`")
# 文档里用反引号标注的文档路径
DOC_IN_DOC = re.compile(r"`(docs/[\w./-]+\.(?:md|yaml|yml|json))`")

SOURCE_SUFFIXES = {".py"}
DOC_SUFFIXES = {".md", ".yaml", ".yml", ".json"}


def _iter_files(base: str, suffixes: set[str]):
    root = ROOT / base
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _docs_files() -> list[Path]:
    files = list(_iter_files("docs", DOC_SUFFIXES))
    for name in ("README.md", "AGENTS.md", "llms.txt"):
        path = ROOT / name
        if path.exists():
            files.append(path)
    return files


def _exists(rel: str) -> bool:
    if any(ch in rel for ch in "*{}<>"):
        return True  # 通配/占位符不是可校验路径
    return (ROOT / rel).exists()


def scan() -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []

    for path in list(_iter_files("src", SOURCE_SUFFIXES)) + list(
        _iter_files("tests", SOURCE_SUFFIXES)
    ) + list(_iter_files("scripts", SOURCE_SUFFIXES)):
        in_tests = path.relative_to(ROOT).parts[0] == "tests"
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            # 测试函数体里的 `docs/...` 多半是样例数据（临时工作区文件），
            # 只检查注释，以及文件头部的说明性 docstring。
            if in_tests and not (line.lstrip().startswith("#") or lineno <= 20):
                continue
            for rel in DOC_REF.findall(line):
                if not _exists(rel):
                    problems.append(
                        {
                            "kind": "code-reference",
                            "file": path.relative_to(ROOT).as_posix(),
                            "line": str(lineno),
                            "target": rel,
                        }
                    )

    for path in _docs_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            for rel in CODE_REF.findall(line) + DOC_IN_DOC.findall(line):
                if not _exists(rel):
                    problems.append(
                        {
                            "kind": "doc-reference",
                            "file": path.relative_to(ROOT).as_posix(),
                            "line": str(lineno),
                            "target": rel,
                        }
                    )

    return problems


def main() -> int:
    problems = scan()
    if "--json" in sys.argv:
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=True, indent=2))
        return 1 if problems else 0

    if problems:
        print(f"文档—代码漂移（{len(problems)} 项）：")
        for item in problems:
            print(f"  - {item['file']}:{item['line']} -> {item['target']}（{item['kind']}）")
        return 1
    print("文档—代码漂移检查通过：没有指向不存在路径的引用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
