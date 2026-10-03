from __future__ import annotations

from coding_agent.diffing import count_changes, unified_diff


def test_identical_content_has_no_diff() -> None:
    assert unified_diff("a\n", "a\n", "x.py") == ""


def test_diff_has_standard_headers() -> None:
    diff = unified_diff("a\n", "b\n", "x.py")
    assert "--- a/x.py" in diff
    assert "+++ b/x.py" in diff
    assert "-a" in diff and "+b" in diff


def test_handles_missing_trailing_newline() -> None:
    diff = unified_diff("a", "ab", "x.py")
    assert "+ab" in diff.replace("\\ No newline at end of file", "").replace("\n", "")


def test_empty_to_content() -> None:
    diff = unified_diff("", "new\n", "x.py")
    assert "+new" in diff


def test_content_to_empty() -> None:
    diff = unified_diff("gone\n", "", "x.py")
    assert "-gone" in diff


def test_count_changes_ignores_file_headers() -> None:
    """不跳过 +++ / --- 的话，每条 diff 都会虚增两行。"""
    diff = unified_diff("a\n", "b\n", "x.py")
    added, removed = count_changes(diff)
    assert (added, removed) == (1, 1)


def test_count_changes_multi_line() -> None:
    diff = unified_diff("a\nb\nc\n", "a\nB\nc\nD\n", "x.py")
    added, removed = count_changes(diff)
    assert added == 2
    assert removed == 1


def test_count_changes_on_empty_diff() -> None:
    assert count_changes("") == (0, 0)
