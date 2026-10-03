"""错误解析：把测试/编译输出抽成「位置 + 消息」。

样本取自真实 pytest / mypy / gcc 的输出格式。解析错了的代价是
模型拿到错的定位，去改一个不存在的地方。
"""

from __future__ import annotations

from coding_agent.tools.testrun import (
    VerifyResult,
    detect_test_command,
    extract_summary,
    parse_issues,
)

PYTEST_FAILURE = """\
============================= test session starts =============================
collected 2 items

tests/test_math.py .F                                                    [100%]

================================== FAILURES ===================================
__________________________________ test_add ___________________________________

    def test_add():
>       assert add(1, 1) == 3
E       assert 2 == 3

tests/test_math.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert 2 == 3
========================= 1 failed, 1 passed in 0.03s =========================
"""

MYPY_OUTPUT = """\
src/app.py:12:5: error: Incompatible return value type (got "int", expected "str")
src/app.py:20:1: error: Missing return statement
Found 2 errors in 1 file (checked 3 source files)
"""

GCC_OUTPUT = """\
main.c:5:9: error: 'undeclared' undeclared (first use in this function)
main.c:7:3: warning: unused variable 'x'
"""

# 沙箱里 python3 自带 unittest（没装 pytest），这是最可能实际遇到的格式
UNITTEST_FAILURE = """\
F
======================================================================
FAIL: test_add (test_calc.TestCalc.test_add)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/verifyprobe/test_calc.py", line 6, in test_add
    self.assertEqual(add(1, 1), 2)
AssertionError: 0 != 2

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (failures=1)
"""

UNITTEST_OK = """\
.
----------------------------------------------------------------------
Ran 1 test in 0.000s

OK
"""


# ---------------- pytest ----------------

def test_parses_pytest_failure_with_assertion_line() -> None:
    issues = parse_issues(PYTEST_FAILURE)
    locations = {i.location for i in issues}
    assert "tests/test_math.py:5" in locations
    assert any("assert 2 == 3" in i.message for i in issues)


def test_parses_pytest_failed_summary_line() -> None:
    issues = parse_issues(PYTEST_FAILURE)
    summary_hits = [i for i in issues if "test_add" in i.location]
    assert summary_hits
    assert "assert 2 == 3" in summary_hits[0].message


def test_does_not_duplicate_the_same_failure() -> None:
    """同一个失败会同时出现在断言行和 FAILED 摘要里，不该记两条一样的。"""
    issues = parse_issues(PYTEST_FAILURE)
    messages = [i.message for i in issues if i.message.startswith("assert 2 == 3")]
    assert len(messages) == 1


def test_pytest_summary_is_extracted() -> None:
    assert "1 failed, 1 passed" in extract_summary(PYTEST_FAILURE)


# ---------------- unittest / Python traceback ----------------

def test_parses_unittest_traceback_location() -> None:
    issues = parse_issues(UNITTEST_FAILURE)
    assert issues[0].location == "/tmp/verifyprobe/test_calc.py:6"
    # 源码行比「in test_add」有用得多 —— 模型据此知道是哪一句出错
    assert "assertEqual" in issues[0].message


def test_parses_unittest_exception_reason() -> None:
    issues = parse_issues(UNITTEST_FAILURE)
    assert any("AssertionError: 0 != 2" in i.message for i in issues)


def test_unittest_summary_is_extracted() -> None:
    assert extract_summary(UNITTEST_FAILURE) == "FAILED (failures=1)"
    assert extract_summary(UNITTEST_OK) == "OK"


def test_unittest_success_has_no_issues() -> None:
    assert parse_issues(UNITTEST_OK) == []


# ---------------- 编译器风格 ----------------

def test_parses_located_errors() -> None:
    issues = parse_issues(MYPY_OUTPUT)
    assert [i.location for i in issues] == ["src/app.py:12", "src/app.py:20"]
    assert "Incompatible return value" in issues[0].message


def test_parses_c_error_with_column() -> None:
    issues = parse_issues(GCC_OUTPUT)
    assert issues[0].location == "main.c:5"
    assert "undeclared" in issues[0].message


def test_no_issues_on_clean_output() -> None:
    assert parse_issues("3 passed in 0.01s\n") == []


def test_falls_back_to_assertion_lines_when_no_location() -> None:
    """只有断言细节、没有定点信息时才用兜底路径。"""
    issues = parse_issues("E   AssertionError: boom\nE   assert 1 == 2\n")
    assert len(issues) == 2
    assert all(i.location == "" for i in issues)


def test_issue_list_is_capped() -> None:
    output = "\n".join(f"src/f{i}.py:1: error: problem {i}" for i in range(100))
    assert len(parse_issues(output)) <= 20


def test_long_messages_are_truncated() -> None:
    issues = parse_issues(f"src/a.py:1: error: {'x' * 1000}")
    assert len(issues[0].message) <= 400


def test_multiline_messages_are_collapsed() -> None:
    issues = parse_issues("src/a.py:1: error: 第一行\n  第二行\n")
    assert "\n" not in issues[0].message


# ---------------- VerifyResult 渲染 ----------------

def test_render_passed() -> None:
    result = VerifyResult(status="ok", command="pytest -q", exit_code=0, summary="2 passed")
    text = result.render()
    assert "验证通过" in text
    assert "pytest -q" in text


def test_render_failed_lists_issues() -> None:
    result = VerifyResult(status="failed", command="pytest -q", exit_code=1)
    result.issues = parse_issues(PYTEST_FAILURE)
    text = result.render()
    assert "发现" in text
    assert "tests/test_math.py:5" in text


def test_render_not_configured() -> None:
    assert "未检测到" in VerifyResult(status="not_configured").render()


def test_ok_property_covers_non_failures() -> None:
    assert VerifyResult(status="ok").ok
    assert VerifyResult(status="skipped").ok
    assert VerifyResult(status="not_configured").ok
    assert not VerifyResult(status="failed").ok


def test_ran_property() -> None:
    assert VerifyResult(status="ok").ran
    assert VerifyResult(status="failed").ran
    assert not VerifyResult(status="skipped").ran
    assert not VerifyResult(status="not_configured").ran


# ---------------- 命令探测（不依赖沙箱的分支） ----------------

def test_override_wins() -> None:
    class _UnusedSandbox:
        def run(self, *args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("有 override 时不该去探测")

    assert detect_test_command(_UnusedSandbox(), "/x", override="make check") == "make check"
