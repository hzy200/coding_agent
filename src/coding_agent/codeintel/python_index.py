"""Python AST 索引：import 图、测试影响分析与轻量 repo map。

索引在宿主侧构建，源码由**一次**沙箱调用收集（避免每个文件一次 wsl.exe）。
这里只做静态近似：包根、动态 import、条件 import 都可能让结果偏保守；
真正的 lint/类型信息仍留给 LSP（尚未接入）。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from coding_agent.sandbox.wsl_exec import WslSandbox

_IGNORED_DIRS = {
    ".git",
    ".agent",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}
_PACKAGE_ROOTS = {"src", "lib"}


@dataclass(slots=True)
class PythonIndex:
    # path -> 该文件 import 的模块名（绝对/相对已归一化）
    imports: dict[str, frozenset[str]] = field(default_factory=dict)
    # module name -> 文件 path（一个模块可能有 src/pkg 与 pkg 两种写法）
    module_files: dict[str, set[str]] = field(default_factory=dict)
    # path -> 顶层符号摘要
    symbols: dict[str, list[str]] = field(default_factory=dict)
    tests: set[str] = field(default_factory=set)

    def module_candidates(self, path: str) -> set[str]:
        return set(_module_candidates(path))

    def modules_for_paths(self, paths: Iterable[str]) -> set[str]:
        result: set[str] = set()
        for path in paths:
            result.update(_module_candidates(path))
        return result

    def importers_of(self, modules: set[str]) -> set[str]:
        found: set[str] = set()
        for path, imported in self.imports.items():
            if any(_import_matches(item, modules) for item in imported):
                found.add(path)
        return found


def _module_candidates(path: str) -> list[str]:
    text = str(path).replace("\\", "/").lstrip("./")
    if not text.endswith(".py"):
        return []
    parts = text[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    candidates = [".".join(parts)]
    if parts[0] in _PACKAGE_ROOTS and len(parts) > 1:
        candidates.append(".".join(parts[1:]))
    # 去重但保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _package_of(candidate: str, path: str) -> str:
    if path.replace("\\", "/").endswith("/__init__.py") or path == "__init__.py":
        return candidate
    return candidate.rsplit(".", 1)[0] if "." in candidate else ""


def _resolve_relative(candidate: str, path: str, level: int, module: str | None) -> str:
    package = _package_of(candidate, path)
    parts = [part for part in package.split(".") if part]
    # level=1 表示当前包，level=2 表示上一级
    up = max(level - 1, 0)
    if up:
        parts = parts[:-up] if up <= len(parts) else []
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _imports_of(tree: ast.AST, path: str) -> set[str]:
    candidates = _module_candidates(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                for candidate in candidates:
                    base = _resolve_relative(
                        candidate, path, node.level, node.module
                    )
                    if base:
                        found.add(base)
                        for alias in node.names:
                            if alias.name != "*":
                                found.add(f"{base}.{alias.name}")
            elif node.module:
                found.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"{node.module}.{alias.name}")
    return found


def _symbols_of(tree: ast.AST) -> list[str]:
    symbols: list[str] = []
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            symbols.append(f"{prefix}{node.name}")
    return symbols


def _is_test_file(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def build_index(sources: dict[str, str]) -> PythonIndex:
    index = PythonIndex()
    for path, source in sources.items():
        normalized = str(path).replace("\\", "/").lstrip("./")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            index.imports[normalized] = frozenset()
            index.symbols[normalized] = []
        else:
            index.imports[normalized] = frozenset(
                _imports_of(tree, normalized)
            )
            index.symbols[normalized] = _symbols_of(tree)
        for candidate in _module_candidates(normalized):
            index.module_files.setdefault(candidate, set()).add(normalized)
        if _is_test_file(normalized):
            index.tests.add(normalized)
    return index


def _import_matches(imported: str, modules: set[str]) -> bool:
    for module in modules:
        if imported == module:
            return True
        # import app 会影响 app.foo；from app.foo import x 会被解析成 app.foo
        if imported.startswith(module + ".") or module.startswith(imported + "."):
            return True
    return False


def related_test_files(
    index: PythonIndex,
    changed: Iterable[str],
    *,
    limit: int = 20,
) -> list[str]:
    """反向依赖闭包 + 文件名启发式，返回相关测试文件。"""
    changed_paths = {
        str(path).replace("\\", "/").lstrip("./")
        for path in changed
        if str(path).endswith(".py")
    }
    if not changed_paths:
        return []

    selected: set[str] = set()
    # 1) 改的本身就是测试
    selected.update(path for path in changed_paths if path in index.tests)

    # 2) 文件名启发式（先给一个稳定的兜底）
    stems = {
        path.rsplit("/", 1)[-1][:-3]
        for path in changed_paths
        if path.rsplit("/", 1)[-1].endswith(".py")
    }
    for test in index.tests:
        name = test.rsplit("/", 1)[-1]
        if name in {f"test_{stem}.py" for stem in stems} or name in {
            f"{stem}_test.py" for stem in stems
        }:
            selected.add(test)

    # 3) 反向依赖闭包：谁 import 了改动模块，谁就是受影响文件
    impacted: set[str] = set(changed_paths)
    frontier = index.modules_for_paths(changed_paths)
    while frontier:
        importers = index.importers_of(frontier) - impacted
        if not importers:
            break
        impacted |= importers
        frontier = index.modules_for_paths(importers)

    selected.update(path for path in impacted if path in index.tests)
    return sorted(selected)[:limit]


def build_repo_map(
    index: PythonIndex, *, max_chars: int = 4_000
) -> str:
    """压缩成「文件: 顶层符号」的 repo map，供 planner/worker 参考。"""
    lines: list[str] = []
    for path in sorted(index.symbols):
        symbols = index.symbols[path]
        if not symbols:
            continue
        suffix = " …" if len(symbols) > 8 else ""
        lines.append(f"{path}: " + ", ".join(symbols[:8]) + suffix)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（repo map 已截断）"
    return text


def collect_python_sources(
    sandbox: WslSandbox,
    root: str,
    *,
    max_files: int = 2_000,
    max_bytes: int = 1_000_000,
) -> dict[str, str]:
    """一次沙箱调用收集 .py 源码；失败返回空 dict。"""
    ignored = json.dumps(sorted(_IGNORED_DIRS))
    script = f"""\
python3 - <<'__AGENT_INDEX__'
import json, os
ignored = set({ignored})
out = {{}}
count = 0
for base, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ignored]
    for name in files:
        if not name.endswith('.py'):
            continue
        path = os.path.relpath(os.path.join(base, name), '.')
        path = path.replace(os.sep, '/')
        try:
            if os.path.getsize(path) > {int(max_bytes)}:
                continue
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                out[path] = fh.read()
        except OSError:
            continue
        count += 1
        if count >= {int(max_files)}:
            break
    if count >= {int(max_files)}:
        break
print('AGENT_INDEX_JSON:' + json.dumps(out))
__AGENT_INDEX__
"""
    try:
        result = sandbox.run(script, cwd=root)
    except Exception:  # noqa: BLE001 - 索引失败必须回退，不影响主流程
        return {}
    if not result.ok:
        return {}
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("AGENT_INDEX_JSON:"):
            try:
                payload = json.loads(line[len("AGENT_INDEX_JSON:") :])
            except json.JSONDecodeError:
                return {}
            if isinstance(payload, dict):
                return {
                    str(key): str(value)
                    for key, value in payload.items()
                    if isinstance(value, str)
                }
    return {}
