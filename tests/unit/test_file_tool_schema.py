"""file_read 的入参上界。

`limit` 一度只有下界（`ge=1`）：模型传 `limit=100000` 就能把整个文件（读上限
2MB）一次性灌进上下文，而上下文裁剪**不碰最近 keep_recent 条**，任何裁剪都拦
不住它 —— 后果不是变慢，而是超长请求直接 400。

这里钉住那道上界；真正的上下文保证在 `llm/context.py` 的单条消息上限。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coding_agent.tools.files import (
    DEFAULT_READ_LINES,
    MAX_READ_LINES,
    ReadInput,
)


def test_limit_has_an_upper_bound() -> None:
    with pytest.raises(ValidationError):
        ReadInput(path="a.txt", reason="读", limit=10**9)


def test_limit_still_allows_a_generous_single_read() -> None:
    """上界要够宽：默认值的数倍仍该放行，别把正常的整文件读取挡在外面。"""
    assert ReadInput(path="a.txt", reason="读", limit=MAX_READ_LINES).limit == MAX_READ_LINES
    assert MAX_READ_LINES > DEFAULT_READ_LINES


def test_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReadInput(path="a.txt", reason="读", limit=0)
