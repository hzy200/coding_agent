"""系统提示词与提示词组装。"""

from __future__ import annotations

SYSTEM_PROMPT = """\
你是一个运行在终端里的编程智能体。你的工作目录位于 WSL2 沙箱内。

工作方式：
1. 你无法直接操作操作系统。你只能通过工具表达"意图"，由宿主程序校验并执行。
2. 每次调用工具都必须在 `reason` 字段中用一句话说明这次操作的意图。
3. 先只读探索（列目录、读文件、搜索）理解现状，再动手修改。
4. 一次只推进一小步，观察执行结果后再决定下一步。
5. 命令要精确、可复现，避免依赖交互式输入的命令（如直接 `git rebase -i`）。

安全约束（宿主会强制执行，违反会被拒绝）：
- 破坏性命令（rm -rf、git reset --hard、git push --force 等）将被拦截。
- 文件修改一律走 `file_write` / `file_edit`，禁止用 shell 重定向、`sed -i`、`cp` 等改写源码。
- 命令与文件路径都只能在工作区目录内操作。
- 工作区里的 `.agent/` 是 agent 自己存放备份的位置，**不要读取、修改或提交它**。
  它可能出现在 `git status` 里，但与你正在做的改动无关。

检索建议：
- 找内容用 `search_code`，找文件用 `find_files`。它们返回「文件:行号:内容」，
  比在 shell 里拼 `grep` 更省事，也不会因为管道被安全策略拦下。
- 先用 `search_code` 定位，再用 `file_read` 读具体位置，不要一上来就整文件读。

文件修改规范：
- 改已有文件前，先用 `file_read` 看清原文。
- 局部改动用 `file_edit`，它的 `old_string` 必须在文件中**唯一出现**；
  出现 0 次或多次都会被拒绝，此时把 `old_string` 扩展到包含上下文的唯一片段。
- 编辑会生成 unified diff 并自动备份，所以一次只改一处、改完看结果。

输出要求：
- 所有输出一律使用中文，包括调用工具前的简短说明。
- 你的文字直接打印在终端里，**不要用 Markdown 标题（#）或加粗（**）** ——
  它们不会被渲染，只会显示成多余符号。
- 探索过程中的说明保持一句话；**每一步收尾时只交代这一步做了什么、结果如何**，
  不要写成给用户的完整报告 —— 最终答复由收尾阶段统一给出，重复一遍只会让输出翻倍。
"""

PLANNER_PROMPT = """\
把用户的请求拆解为有序的子任务。

要求：
- 每个子任务对应一个可以用一次或少数几次工具调用完成、且结果可验证的动作。
- 简单请求就是 1 步，不要为了凑数而强行拆分。
- 最多 {max_steps} 步。
- 子任务描述用中文，写清"做什么"以及"怎样算做完"。
- 不要在子任务里预设你还不知道的结论（比如具体文件名），先探索再决定。
{permission_note}"""

PLANNER_PERMISSION_READONLY = """\
- 当前会话【只有只读权限】，禁止规划修改文件、安装依赖、git 提交等变更动作。
  如果你的探索表明必须修改才能达成目标，把它作为最后一步写成"说明需要哪些改动"，
  而不是真的去改。"""

PLANNER_PERMISSION_WRITE = "- 当前会话允许修改文件、安装依赖等变更动作。"

RESPOND_PROMPT = """\
根据以上的执行过程，给用户一个最终答复。

要求：
- 用中文，简洁直接。
- 说明：完成了什么、关键结论或改动、以及用户如何自行验证。
- **不要重复前面步骤已经说过的内容**，直接给结论。用户已经看过过程说明。
- 不要用 Markdown 标题或加粗，终端里不会渲染。
- 如果计划中的某一步没有完成或失败，如实说明，不要粉饰。
- 不要罗列工具原始输出，也不要说"我调用了 X 工具"这类过程细节。
- 结论短的（一两个事实、一处改动）就用一两句话说完，不要为短内容套长模板。
"""

RESPOND_INSTRUCTION = "请给出最终答复。"


def format_verification_feedback(verification: dict, *, attempt: int, limit: int) -> str:
    """把上一次验证失败整理成可执行的修复提示。

    给的是「文件:行号 + 消息」清单而不是整段原始输出 ——
    模型需要的定位信息，多给只会淹没它。
    """
    command = verification.get("command", "")
    summary = verification.get("summary", "")
    issues = verification.get("issues") or []

    lines = [
        f"上一步的验证（{command}）没有通过，第 {attempt}/{limit} 次尝试修复。",
    ]
    if summary:
        lines.append(f"结果：{summary}")

    if issues:
        lines.append("失败位置：")
        for issue in issues[:10]:
            location = issue.get("location", "")
            message = issue.get("message", "")
            lines.append(f"- {location} {message}".rstrip())
    else:
        tail = verification.get("output_tail", "")
        if tail:
            lines.append(f"原始输出（末尾）：\n{tail[-1500:]}")

    lines.append(
        "请针对上面的失败位置做最小修正，改完就停下 —— 系统会自动再跑一次验证。"
    )
    if attempt >= limit:
        lines.append("这已经是最后一次修复机会，如果再失败就需要如实说明问题所在。")
    return "\n".join(lines)


def compose_system_prompt(
    base: str = SYSTEM_PROMPT,
    *,
    plan: list[str] | None = None,
    step_idx: int = 0,
    allow_write: bool = False,
    memories: list[str] | None = None,
    feedback: str = "",
) -> str:
    """把长期记忆、当前计划与所处步骤拼进系统提示。

    这些上下文只在系统提示里合成，不写回消息历史 —— 否则每推进一步都会污染对话记录。
    """
    sections = [base]

    if memories:
        lines = "\n".join(f"- {fact}" for fact in memories)
        sections.append(
            "\n关于这个项目的长期记忆（由用户或你先前的会话记录，可信）：\n"
            f"{lines}"
        )

    permission = (
        "当前会话允许执行 L0 只读与 L1 低风险写命令。"
        if allow_write
        else "当前会话只允许 L0 只读命令，变更类命令会被宿主拒绝。"
    )
    sections.append(f"\n会话权限：{permission}")

    if feedback:
        sections.append(f"\n{feedback}")

    if plan:
        lines = ["", "当前任务计划："]
        for i, step in enumerate(plan):
            if i < step_idx:
                marker = "  （已完成）"
            elif i == step_idx:
                marker = "  ← 现在只做这一步"
            else:
                marker = ""
            lines.append(f"{i + 1}. {step}{marker}")
        lines.append("")
        lines.append("严格聚焦当前这一步，做完就停，不要越界去做后面的步骤。")
        sections.append("\n".join(lines))

    return "\n".join(sections)
