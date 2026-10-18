"""图包。

`build_graph` 用惰性导入：orchestrator / graph.state 等子模块在导入期不应
反向拉起整个 build（会与 `graph.build -> orchestrator -> graph.state` 形成环）。
"""

from coding_agent.graph.state import AgentState

__all__ = ["AgentState", "build_graph"]


def __getattr__(name: str):
    if name == "build_graph":
        from coding_agent.graph.build import build_graph

        return build_graph
    raise AttributeError(name)
