"""委派工具：把一次只读探索交给子智能体。

工具本身**不执行**任何逻辑 —— 它只是给模型一个结构化入口。真正的执行在
`graph/nodes/tools.py` 里按名字拦截，因为子智能体需要 LLM、独立消息历史与
工具白名单，这些都不属于一个普通 `BaseTool` 的职责。

这让 `tools` 节点仍然是所有工具执行的唯一咽喉：delegate 调用同样要经过
approval_gate + tools 节点，只是节点内部把它转交给宿主侧的探索 runner。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.tools.code_intel import CODE_INTEL
from coding_agent.tools.files import READ_TOOL_NAME
from coding_agent.tools.memory import MEMORY_SEARCH
from coding_agent.tools.search import FIND_FILES, SEARCH_CODE
from coding_agent.tools.symbols import SYMBOL_SEARCH

DELEGATE_EXPLORE = "delegate_explore"

# 子智能体唯一允许的工具集合。**只读、零副作用**：
# - file_read：读文件
# - search_code / find_files：检索
# - symbol_search：AST 符号定位（只读）
# - memory_search：项目长期记忆检索（只读）
# 写工具、shell、run_tests 一律不放开 —— 特别是 run_tests 会执行工作区里的代码，
# 而子智能体的输入包含仓库内容，不能让它获得执行/写入能力。
EXPLORE_TOOL_NAMES = frozenset(
    {
        READ_TOOL_NAME,
        SEARCH_CODE,
        FIND_FILES,
        SYMBOL_SEARCH,
        CODE_INTEL,
        MEMORY_SEARCH,
    }
)


class DelegateInput(BaseModel):
    """委派探索的输入。"""

    goal: str = Field(
        description="要探索的具体问题或目标，一句话；例如「找出折扣逻辑跨了哪些文件」"
    )
    paths: list[str] = Field(
        default_factory=list,
        description="优先查看的文件或目录（工作区内相对路径），可以为空",
    )
    reason: str = Field(description="为什么要委派这次探索，一句话说明")


def _host_only(goal: str, reason: str, paths: list[str] | None = None) -> str:
    """永远不该被直接调用：宿主在 tools 节点里拦截 delegate。"""
    raise RuntimeError(
        f"{DELEGATE_EXPLORE} 由宿主在 tools 节点内执行，不能直接 invoke"
    )


def build_delegate_tool() -> BaseTool:
    """构造暴露给主模型的委派工具。

    描述要写清适用边界：它解决的是「不熟悉仓库 / 多个独立区域 / 动手前定位」，
    不是「读一个已知文件」的替代品。否则模型会把每一次读取都包成一次子智能体调用，
    成本翻倍而收益为零。
    """
    return StructuredTool.from_function(
        func=_host_only,
        name=DELEGATE_EXPLORE,
        description=(
            "把一个**只读探索**任务交给独立上下文的子智能体，返回结构化摘要。"
            "子智能体只能读文件/搜索，不能修改文件或执行命令；它的中间过程不会进入"
            "你的对话历史。适用于：不熟悉仓库、需要同时查多个独立区域、动手前定位。"
            "不要用它代替一次简单的 file_read。"
        ),
        args_schema=DelegateInput,
    )
