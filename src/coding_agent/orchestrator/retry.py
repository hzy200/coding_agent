"""失败重试时注入给 worker 的上下文。

旧实现只是把 failed 改回 pending 再原样跑一遍；同样的提示、同样的工具路径
通常只会得到同样的失败。这里把上次的状态/摘要/验证问题压成一段短提示，
让重试至少知道「上一次错在哪」。
"""

from __future__ import annotations

from typing import Any


def format_retry_context(task: dict[str, Any]) -> str:
    context = task.get("retry_context") or {}
    if not isinstance(context, dict) or not context:
        return ""

    lines = [
        "",
        f"这是第 {int(context.get('attempt', 1)) + 1} 次尝试。",
        "上一次尝试没有成功，请先判断根因，不要重复同样的动作：",
    ]
    status = str(context.get("status", "")).strip()
    if status:
        lines.append(f"- 上次状态：{status}")
    summary = str(context.get("summary", "")).strip()
    if summary:
        lines.append(f"- 上次摘要：{summary[:600]}")
    error = str(context.get("error", "")).strip()
    if error:
        lines.append(f"- 上次错误：{error[:600]}")
    findings = [str(item) for item in (context.get("findings") or [])[:6]]
    if findings:
        lines.append("- 上次发现：")
        lines.extend(f"  - {item[:300]}" for item in findings)
    verification = context.get("verification") or {}
    issues = verification.get("issues") or []
    if issues:
        lines.append("- 上次验证问题：")
        for issue in issues[:8]:
            where = str(issue.get("location", "")).strip()
            message = str(issue.get("message", "")).strip()
            lines.append(f"  - {where} {message}".strip())
    return "\n".join(lines)
