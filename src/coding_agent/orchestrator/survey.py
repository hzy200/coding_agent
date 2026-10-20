"""编排前的仓库画像：顶层目录/文件、测试命令、git 隔离条件。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.codeintel.python_index import (
    build_index,
    build_repo_map,
    collect_python_sources,
)
from coding_agent.graph.state import AgentState
from coding_agent.orchestrator.implement import stage_args
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.testrun import detect_test_command


def make_survey_node(
    sandbox: WslSandbox, root: str, settings: Any = None
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def survey(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        summary = dict(state.get("repo_profile") or {})
        top_entries: dict[str, list[str]] = {"dirs": [], "files": []}
        is_git = False
        git_root = False
        git_clean = False
        # 一次 wsl.exe 拿完顶层目录/文件与 git 状态；原先这里是 6 次进程启动，
        # 4 个 worker 并发时这部分开销会被放大。
        script = f"""\
echo "AGENT_DIRS"
find . -maxdepth 1 -mindepth 1 -type d -not -name '.*' | sort | head -20
echo "AGENT_FILES"
find . -maxdepth 1 -mindepth 1 -type f -not -name '.*' | sort | head -20
echo "AGENT_GIT"
if git rev-parse --is-inside-work-tree 2>/dev/null | grep -q true; then
  echo "INSIDE=1"
  top=$(git rev-parse --show-toplevel 2>/dev/null)
  physical=$(pwd -P)
  if [ "$top" = "$physical" ]; then echo "ROOT=1"; else echo "ROOT=0"; fi
  dirty=$(git status --porcelain -- . {stage_args()} 2>/dev/null)
  if [ -z "$dirty" ]; then echo "CLEAN=1"; else echo "CLEAN=0"; fi
else
  echo "INSIDE=0"; echo "ROOT=0"; echo "CLEAN=0"
fi
"""
        try:
            result = sandbox.run(script, cwd=root)
            section = ""
            flags: dict[str, str] = {}
            for line in result.stdout.splitlines():
                text = line.strip()
                if text in {"AGENT_DIRS", "AGENT_FILES", "AGENT_GIT"}:
                    section = text
                    continue
                if section == "AGENT_DIRS" and text:
                    top_entries["dirs"].append(text)
                elif section == "AGENT_FILES" and text:
                    top_entries["files"].append(text)
                elif section == "AGENT_GIT" and "=" in text:
                    key, _, value = text.partition("=")
                    flags[key] = value
            is_git = flags.get("INSIDE") == "1"
            git_root = flags.get("ROOT") == "1"
            git_clean = flags.get("CLEAN") == "1"
        except Exception:  # noqa: BLE001 - survey 失败不能拖垮 run
            pass

        try:
            test_command = detect_test_command(sandbox, root, allow_manifest=False)
        except Exception:  # noqa: BLE001
            test_command = ""

        summary["top_dirs"] = top_entries["dirs"]
        summary["top_files"] = top_entries["files"]
        summary["test_command"] = test_command
        summary["git"] = is_git
        summary["git_root"] = git_root
        summary["git_clean"] = git_clean

        budget = int(getattr(settings, "repo_map_chars", 4_000) or 0)
        if budget > 0:
            try:
                sources = collect_python_sources(sandbox, root)
                if sources:
                    index = build_index(sources)
                    summary["python_files"] = len(sources)
                    summary["repo_map"] = build_repo_map(
                        index, max_chars=budget
                    )
            except Exception:  # noqa: BLE001 - 索引失败不影响 survey
                pass
        return {"repo_summary": summary}

    return survey
