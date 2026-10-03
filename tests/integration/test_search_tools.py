"""集成测试：检索工具。

后端按环境在 ripgrep / grep 之间自动切换，两个后端的输出格式一致，
所以这些用例不关心具体跑了哪个二进制。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, unpack
from coding_agent.tools.search import (
    FIND_FILES,
    SEARCH_CODE,
    build_search_tools,
    search_backend,
)

pytestmark = pytest.mark.wsl

PY_A = "def classify(x):\n    return 'read'\n\n\ndef handler():\n    pass\n"
PY_B = "def classify(y):\n    return 'write'\n"
TXT = "classify 这个词出现在文本文件里\n"


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-search-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}/pkg {shlex.quote(root)}/docs")
    for rel, content in (("pkg/a.py", PY_A), ("pkg/b.py", PY_B), ("docs/note.txt", TXT)):
        target = shlex.quote(f"{root}/{rel}")
        require_wsl.run(f"printf %s {shlex.quote(content)} > {target}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def tools(require_wsl: WslSandbox, workdir: str, settings) -> dict:
    scoped = settings.model_copy(update={"wsl_workspace": workdir})
    return {t.name: t for t in build_search_tools(scoped, require_wsl)}


def _invoke(tool, **args) -> tuple[str, ShellArtifact]:
    text, artifact = unpack(tool.invoke(args))
    assert artifact is not None
    return text, ShellArtifact.model_validate(artifact)


# ---------------- 后端 ----------------

def test_backend_is_reported(require_wsl: WslSandbox) -> None:
    assert search_backend(require_wsl) in ("ripgrep", "grep")


# ---------------- search_code ----------------

def test_finds_matches_with_relative_paths(tools) -> None:
    text, artifact = _invoke(tools[SEARCH_CODE], pattern="def classify", reason="找函数")
    assert artifact.ok
    assert "命中 2 条" in text
    assert "pkg/a.py" in text and "pkg/b.py" in text
    # 输出应是工作区相对路径，不该泄露绝对路径
    assert "/mnt/" not in text.split("\n$ ", 1)[1] if "\n$ " in text else True


def test_reports_line_numbers(tools) -> None:
    text, _ = _invoke(tools[SEARCH_CODE], pattern="def classify", reason="带行号")
    assert "pkg/a.py:1" in text


def test_glob_filters_files(tools) -> None:
    text, _ = _invoke(
        tools[SEARCH_CODE], pattern="classify", reason="只看 python", glob="*.py"
    )
    assert "pkg/a.py" in text
    assert "note.txt" not in text


def test_pattern_can_match_non_python(tools) -> None:
    text, _ = _invoke(tools[SEARCH_CODE], pattern="classify", reason="全类型")
    assert "note.txt" in text


def test_no_match_is_not_a_failure(tools) -> None:
    """「没搜到」不是错误，否则模型会以为工具坏了。"""
    text, artifact = _invoke(tools[SEARCH_CODE], pattern="zzz-not-here-zzz", reason="搜不到")
    assert artifact.ok is True
    assert "命中 0 条" in text
    assert "（无匹配）" in text


def test_max_results_truncates_and_says_so(tools) -> None:
    text, _ = _invoke(
        tools[SEARCH_CODE], pattern="def ", reason="限量", max_results=1
    )
    assert "命中 3 条，显示前 1 条" in text


def test_fixed_strings_treats_pattern_literally(tools) -> None:
    """`classify(x)` 里的括号在正则下有特殊含义，字面量模式必须能避开。"""
    regex_text, _ = _invoke(tools[SEARCH_CODE], pattern="classify(x)", reason="正则")
    # 正则里 (x) 是一个分组，整体匹配的是 "classifyx"，所以搜不到
    assert "命中 0 条" in regex_text

    fixed_text, _ = _invoke(
        tools[SEARCH_CODE], pattern="classify(x)", reason="字面量", fixed=True
    )
    assert "命中 1 条" in fixed_text
    assert "pkg/a.py:1" in fixed_text


def test_ignore_case(tools) -> None:
    """默认区分大小写；开了 ignore_case 才放宽。"""
    sensitive, artifact = _invoke(tools[SEARCH_CODE], pattern="CLASSIFY", reason="区分大小写")
    assert "命中 0 条" in sensitive
    assert artifact.ok is True  # 无匹配不算失败

    text, _ = _invoke(
        tools[SEARCH_CODE], pattern="CLASSIFY", reason="忽略大小写", ignore_case=True
    )
    # docs/note.txt 里也有一处，共 3 条
    assert "命中 3 条" in text


def test_path_narrows_the_search(tools) -> None:
    text, _ = _invoke(tools[SEARCH_CODE], pattern="classify", reason="只看 docs", path="docs")
    assert "note.txt" in text
    assert "pkg/a.py" not in text


def test_path_escape_is_rejected(tools) -> None:
    text, artifact = _invoke(tools[SEARCH_CODE], pattern="root", reason="越界", path="/etc")
    assert artifact.rejected is True
    assert "路径非法" in text


def test_pattern_with_quotes_does_not_break(tools) -> None:
    """模式逐参数传递，模型不需要自己处理引号。"""
    text, artifact = _invoke(tools[SEARCH_CODE], pattern="'read'", reason="带引号", fixed=True)
    assert artifact.ok
    assert "pkg/a.py" in text


# ---------------- find_files ----------------

def test_lists_files_by_pattern(tools) -> None:
    text, artifact = _invoke(tools[FIND_FILES], pattern="*.py", reason="列出 py")
    assert artifact.ok
    assert "找到 2 个文件" in text
    assert "pkg/a.py" in text and "pkg/b.py" in text


def test_find_is_sorted_and_deterministic(tools) -> None:
    first, _ = _invoke(tools[FIND_FILES], pattern="*", reason="全部")
    second, _ = _invoke(tools[FIND_FILES], pattern="*", reason="全部")
    assert first == second


def test_find_limits_results(tools) -> None:
    text, _ = _invoke(tools[FIND_FILES], pattern="*", reason="限量", max_results=1)
    assert "找到 3 个文件，显示前 1 个" in text


def test_find_no_match(tools) -> None:
    text, artifact = _invoke(tools[FIND_FILES], pattern="*.rs", reason="没有")
    assert artifact.ok is True
    assert "找到 0 个文件" in text


def test_find_scoped_to_subdirectory(tools) -> None:
    text, _ = _invoke(tools[FIND_FILES], pattern="*.py", reason="只看 pkg", path="pkg")
    assert "pkg/a.py" in text


def test_find_rejects_path_escape(tools) -> None:
    _, artifact = _invoke(tools[FIND_FILES], pattern="*", reason="越界", path="/etc")
    assert artifact.rejected is True


# ---------------- 排除与安全 ----------------

def test_heavy_directories_are_excluded(require_wsl, workdir, settings) -> None:
    """grep 不读 .gitignore，这些目录必须显式排除，否则会扫到天荒地老。"""
    require_wsl.run(
        f"mkdir -p {shlex.quote(workdir)}/.venv/lib && "
        f"printf 'def classify(): pass\\n' > {shlex.quote(workdir)}/.venv/lib/junk.py"
    )
    scoped = settings.model_copy(update={"wsl_workspace": workdir})
    tools = {t.name: t for t in build_search_tools(scoped, require_wsl)}

    # 断言命中结果里没有它 —— 命令行本身会回显 --exclude-dir，不能一起断言
    text, _ = _invoke(tools[SEARCH_CODE], pattern="def classify", reason="排除检查")
    assert "命中 2 条" in text
    assert "junk.py" not in text

    listing, _ = _invoke(tools[FIND_FILES], pattern="*.py", reason="排除检查")
    assert "junk.py" not in listing


def test_search_tools_are_read_only(tools) -> None:
    _, artifact = _invoke(tools[SEARCH_CODE], pattern="def", reason="只读")
    assert artifact.level_label == "L0 只读"
