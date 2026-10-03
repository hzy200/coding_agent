"""repair 节点：验证失败后，给模型一次「带着错误信息重来」的机会。

它本身不做诊断，只负责两件事：

1. **递增重试计数**并给修复一次完整的工具预算 —— 上一步的轮次可能已经用光，
   不给新预算的话模型还没来得及改就被 act 的预算保护掐掉了。
2. **保持 `dirty`**，让修完之后必然再跑一次验证，而不是改完就宣称成功。

诊断信息的传递不在这里：结构化错误一直在 `state["verification"]` 里，
由 act 合成进系统提示。这样消息历史保持干净 —— 往里塞伪造成用户或助手口吻的
「反馈消息」会污染后续所有轮次的上下文。
"""

from __future__ import annotations

from typing import Any

from coding_agent.graph.state import AgentState


def repair(state: AgentState) -> dict[str, Any]:
    return {
        "retry": state.get("retry", 0) + 1,
        "tool_rounds": 0,
        "budget_exhausted": False,
        # 修完必须重新验证，否则「改坏了但没人发现」
        "dirty": True,
    }
