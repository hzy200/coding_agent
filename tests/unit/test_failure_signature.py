"""失败测试解析、失败签名与 repair 的测试选择。

这些函数决定了 repair 是「定向修一个失败」还是「把同一类修改重复到额度耗尽」。
"""

from __future__ import annotations

from coding_agent.tools.testrun import (
    VerifyIssue,
    _selected_command,
    compute_failure_signature,
    parse_failed_tests,
)

PYTEST_FAILURE = """\
FAILED tests/test_discount.py::test_net - AssertionError: assert 100 == 90
FAILED tests/test_core.py::TestOrder::test_ship - KeyError: 'discount_bps'
1 failed, 1 passed in 0.03s
"""

UNITTEST_FAILURE = """\
FAIL: test_net (tests.test_discount.TestDiscount.test_net)
ERROR: test_ship (tests.test_core.TestOrder.test_ship)
"""


def test_parses_pytest_failed_nodeids() -> None:
    assert parse_failed_tests(PYTEST_FAILURE) == [
        "tests/test_discount.py::test_net",
        "tests/test_core.py::TestOrder::test_ship",
    ]


def test_parses_unittest_failed_ids() -> None:
    assert parse_failed_tests(UNITTEST_FAILURE) == [
        "tests.test_discount.TestDiscount.test_net",
        "tests.test_core.TestOrder.test_ship",
    ]


def test_rejects_unsafe_nodeids() -> None:
    output = (
        "FAILED ../../evil.py::test_x - boom\n"
        "FAILED tests/x.py::test_y; rm -rf / - boom\n"
        "FAILED /abs/path.py::test_z - boom\n"
    )
    assert parse_failed_tests(output) == []


def test_signature_is_stable_across_line_numbers_and_addresses() -> None:
    first = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=[],
        issues=[VerifyIssue(location="app/x.py:10", message="AssertionError: 1 != 2")],
    )
    second = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=[],
        issues=[
            VerifyIssue(
                location="app/x.py:42",
                message="AssertionError: 1 != 2 at 0xdeadbeef",
            )
        ],
    )
    assert first == second


def test_signature_changes_when_the_same_test_fails_differently() -> None:
    test_id = ["tests/a.py::test_x"]
    a = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=test_id,
        issues=[VerifyIssue(location="app/x.py:10", message="AssertionError: 1 != 2")],
    )
    b = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=test_id,
        issues=[VerifyIssue(location="app/x.py:10", message="KeyError: 'discount_bps'")],
    )
    assert a != b


def test_signature_changes_when_a_different_test_fails() -> None:
    a = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=["tests/a.py::test_x"],
        issues=[],
    )
    b = compute_failure_signature(
        status="failed",
        kind="tests",
        failed_tests=["tests/b.py::test_y"],
        issues=[],
    )
    assert a != b


def test_signature_is_empty_when_verification_passed() -> None:
    assert (
        compute_failure_signature(
            status="ok", kind="tests", failed_tests=[], issues=[]
        )
        == ""
    )


def test_selected_command_appends_safe_nodeids() -> None:
    command = _selected_command(
        "python3 -m pytest -q",
        ("tests/test_x.py::test_a", "tests/test_x.py::TestB::test_c"),
    )
    assert command == (
        "python3 -m pytest -q tests/test_x.py::test_a "
        "tests/test_x.py::TestB::test_c"
    )


def test_selected_command_refuses_unsafe_nodeids() -> None:
    assert _selected_command("python3 -m pytest -q", ("../../evil.py::test",)) == ""
    assert _selected_command("python3 -m pytest -q", ("tests/x.py::a; rm -rf /",)) == ""


def test_selected_command_ignores_non_pytest_commands() -> None:
    assert _selected_command("cargo test", ("tests/a.py::test_x",)) == ""
    assert _selected_command("python3 - <<'__AGENT_SYNTAX__'", ("a.py::x",)) == ""
