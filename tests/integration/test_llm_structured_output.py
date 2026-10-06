"""真实 provider 上，结构化输出必须可用。

**这条测试是被一次真实事故加上的。** `with_structured_output(schema)` 默认走
OpenAI 的 `json_schema` response_format，而 DeepSeek 端点对它直接返回 400
（三种模型都一样）。planner 把异常静默吞掉、降级成「整条请求当作一步」——
于是一个**完全失效的规划，看起来和正常工作一模一样**：运行正常、事件正常、
审计正常，只是每次的计划都是请求原文。

单元测试发现不了它：那些用例注入的是假 LLM，直接返回 pydantic 对象，
**从不碰真实 provider**。所以必须有一条真的去调的测试。

标 `llm`（需要 DEEPSEEK_API_KEY），不进 CI —— 但改动 planner / replan 的
结构化输出方式后必须本地跑一遍：

    pytest -m llm tests/integration/test_llm_structured_output.py -q
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from coding_agent.config import Settings, get_settings
from coding_agent.graph.nodes.planner import TaskPlan
from coding_agent.graph.nodes.replan import ReplanDecision
from coding_agent.llm.deepseek import build_llm, with_json_output
from coding_agent.llm.prompts import (
    PLANNER_PERMISSION_WRITE,
    PLANNER_PROMPT,
    REPLAN_PROMPT,
)

pytestmark = pytest.mark.llm

# 一个**明确列出多个交付物**的请求：正常的 planner 应该给出不止一步。
MULTI_PART_REQUEST = "有三个测试失败了：test_a、test_b、test_c。这三个都要修好。"


@pytest.fixture
def settings() -> Settings:
    resolved = get_settings()
    if not resolved.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")
    return resolved


def test_planner_structured_output_returns_steps(settings: Settings) -> None:
    llm = build_llm(settings, streaming=False)
    structured = with_json_output(llm, TaskPlan)
    prompt = PLANNER_PROMPT.format(
        max_steps=5, permission_note=PLANNER_PERMISSION_WRITE
    )

    task_plan = structured.invoke(
        [SystemMessage(content=prompt), HumanMessage(content=MULTI_PART_REQUEST)]
    )

    assert task_plan.steps, (
        f"planner 的结构化输出没返回任何步骤（模型：{settings.deepseek_model}）。"
        f"若这里抛 400，说明当前 provider 不支持所选的结构化输出方式 —— "
        f"planner 会把它静默吞掉并降级成一步，运行看起来一切正常。"
    )


def test_planner_splits_a_multi_part_request(settings: Settings) -> None:
    """请求里明确列了三个交付物，不该被压成一步。

    压成一步的直接后果：`advance` 不执行 → `replan` 永远够不着 →
    「执行中修正计划」这个能力实际上是死代码。
    """
    llm = build_llm(settings, streaming=False)
    structured = with_json_output(llm, TaskPlan)
    prompt = PLANNER_PROMPT.format(
        max_steps=5, permission_note=PLANNER_PERMISSION_WRITE
    )

    task_plan = structured.invoke(
        [SystemMessage(content=prompt), HumanMessage(content=MULTI_PART_REQUEST)]
    )

    assert len(task_plan.steps) >= 2, (
        f"三个可分别验收的交付物被压成了 {len(task_plan.steps)} 步："
        f"{task_plan.steps}。这样 `advance` 不会执行，重规划无从触发。"
    )


def test_replan_structured_output_round_trips(settings: Settings) -> None:
    llm = build_llm(settings, streaming=False)
    structured = with_json_output(llm, ReplanDecision)
    prompt = REPLAN_PROMPT.format(max_steps=3)

    decision = structured.invoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(
                content=(
                    "用户请求：把三个模块里的日期格式统一成 ISO。\n\n"
                    "计划：\n1. 找出所有日期格式化代码（已完成）\n"
                    "2. 逐个模块修改（下一步）\n\n"
                    "刚做完这步的小结：三个模块都用了 utils.format_date。"
                )
            ),
        ]
    )

    assert isinstance(decision.revise, bool)
    assert isinstance(decision.steps, list)
