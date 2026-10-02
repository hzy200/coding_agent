"""命令安全分级。

判定权完全在宿主，不依赖 LLM 的自我申报。复合命令按段分类取最高级别；
解析失败或无法识别的一律降级为 MUTATE（需要人工确认），而不是放行。
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from enum import IntEnum


class CommandLevel(IntEnum):
    """命令风险等级，数值越大越危险。"""

    READ = 0        # 只读，自动执行
    LOW_WRITE = 1   # 低风险写，自动执行 + 审计
    MUTATE = 2      # 变更性操作，需要用户确认
    DANGER = 3      # 危险操作，拒绝或强制二次确认

    @property
    def label(self) -> str:
        return {
            CommandLevel.READ: "L0 只读",
            CommandLevel.LOW_WRITE: "L1 低风险写",
            CommandLevel.MUTATE: "L2 变更性",
            CommandLevel.DANGER: "L3 危险",
        }[self]


@dataclass(frozen=True, slots=True)
class Verdict:
    level: CommandLevel
    reason: str
    segment: str = ""

    @property
    def auto_allowed(self) -> bool:
        """是否可跳过人工确认。"""
        return self.level <= CommandLevel.LOW_WRITE


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------

READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "pwd", "echo", "wc", "find", "grep", "rg",
    "file", "stat", "du", "df", "tree", "which", "whoami", "id", "date",
    "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath", "readlink",
    "diff", "cmp", "comm", "jq", "yq", "xxd", "od", "md5sum", "sha256sum",
    "printenv", "uname", "hostname", "nproc", "free", "type", "command",
})

# 故意不含 cp / ln / mv：它们能覆盖或替换文件，会绕过文件工具的
# 精确替换、diff 与写前备份。这类操作走 file_* 工具，或降级为 L2 等人工确认。
LOW_WRITE_COMMANDS = frozenset({
    "mkdir", "touch", "sed",
})

# git 子命令分级
GIT_READ_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "config", "rev-parse",
    "ls-files", "blame", "describe", "tag", "shortlog", "whatchanged", "stash",
})
GIT_LOW_WRITE_SUBCOMMANDS = frozenset({"add", "init", "fetch", "restore"})
GIT_DANGER_SUBCOMMANDS = frozenset({"clean"})

# --------------------------------------------------------------------------
# 危险模式（对原始命令全文匹配，可捕获命令替换 $(...) 与反引号内的内容）
# --------------------------------------------------------------------------

_RM = r"\brm\b"
DANGER_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"{_RM}\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f", "递归强制删除"),
    (rf"{_RM}\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*f[a-zA-Z]*[rR]", "递归强制删除"),
    (rf"{_RM}\s+(-\S+\s+)*/(\s|$)", "删除根目录"),
    (rf"{_RM}\s+(-\S+\s+)*(~|\$HOME)", "删除家目录"),
    (rf"{_RM}\s+(-\S+\s+)*\*", "通配符删除"),
    (r"\b(mkfs|mke2fs|fdisk|parted)\b", "磁盘格式化/分区操作"),
    (r"\bdd\b[^\n]*\bof\s*=\s*/dev/", "向块设备直写"),
    (r">\s*/dev/(sd|nvme|hd|vd)", "覆写块设备"),
    (r"\bsudo\b|\bsu\s+-|\bdoas\b", "提权"),
    (r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b", "关机/重启"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*(777|a\+rwx)\s+/(\s|$)", "放开根目录权限"),
    (r"\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\s+\S+\s+/(\s|$)", "递归改根目录属主"),
    (r"\bpasswd\b|\buseradd\b|\buserdel\b|\busermod\b|\bgroupadd\b", "账户管理"),
    (r"\biptables\b|\bnft\b|\bufw\b|\bfirewall-cmd\b", "防火墙规则"),
    (r"\(\s*\)\s*\{.*\|.*&\s*\}\s*;?\s*:", "fork 炸弹"),
    (r"\bgit\s+push\b[^\n]*(--force|-f)\b", "强制推送"),
    (r"\bgit\s+reset\s+--hard\b", "硬重置丢弃改动"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]", "清理未跟踪文件"),
    (r"\bgit\s+branch\s+-D\b", "强制删除分支"),
    (r"\bgit\s+filter-branch\b|\bgit\s+reflog\s+delete\b", "重写历史"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", "管道执行远程脚本"),
    (r"\bbase64\s+(-d|--decode)\b[^\n|]*\|\s*(sh|bash)", "解码后执行"),
    (r"\beval\b|\bsource\s+/dev/stdin\b", "动态求值执行"),
    (r"\bnc\b[^\n]*\s-[a-zA-Z]*e\b|\bncat\b[^\n]*--exec\b", "反弹 shell"),
    (r"\bhistory\s+-c\b", "清空命令历史"),
    (r"\bcrontab\b|\bsystemctl\s+(stop|disable|mask)\b", "持久化/停服务"),
    (r"\bfind\b[^\n]*\s-(delete|exec\s+rm)\b", "批量删除"),
    (r"\bxargs\b[^\n]*\brm\b", "批量删除"),
    (r"\b(pkill|killall)\b[^\n]*\s(-9\s+)?(1|init)\b", "杀关键进程"),
    (r"\btruncate\s+-s\s*0\b", "清空文件内容"),
)

_DANGER_RE = tuple((re.compile(p), d) for p, d in DANGER_PATTERNS)

# 无副作用的黑洞重定向，忽略之
_NOOP_REDIRECT_RE = re.compile(r"\d?>>?\s*(/dev/null|&\d)")

_OPERATORS = ("&&", "||", ";", "|", "&", "\n")


def split_segments(command: str) -> list[str]:
    """按 && || ; | & 拆分复合命令，忽略引号内的分隔符。"""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|&\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return []
    # 跳过 VAR=value 形式的环境变量前缀
    idx = 0
    while idx < len(words) and "=" in words[idx] and not words[idx].startswith("-"):
        idx += 1
    return words[idx:]


def _classify_segment(segment: str) -> Verdict:
    words = _tokens(segment)
    if not words:
        return Verdict(CommandLevel.MUTATE, "无法解析的命令片段", segment)

    name = posixpath.basename(words[0])
    sub = words[1] if len(words) > 1 else ""

    if name == "git":
        return _classify_git(sub, segment)

    if name in READ_ONLY_COMMANDS:
        return Verdict(CommandLevel.READ, f"{name} 属于只读命令", segment)
    if name in LOW_WRITE_COMMANDS:
        if name == "sed" and any(w.startswith("-i") for w in words[1:]):
            return Verdict(CommandLevel.MUTATE, "sed -i 会原地改写文件", segment)
        return Verdict(CommandLevel.LOW_WRITE, f"{name} 属于低风险写命令", segment)

    return Verdict(CommandLevel.MUTATE, f"未在白名单中的命令：{name}", segment)


def _classify_git(sub: str, segment: str) -> Verdict:
    if sub in GIT_DANGER_SUBCOMMANDS:
        return Verdict(CommandLevel.DANGER, f"git {sub} 具有破坏性", segment)
    if sub in GIT_READ_SUBCOMMANDS:
        return Verdict(CommandLevel.READ, f"git {sub} 只读", segment)
    if sub in GIT_LOW_WRITE_SUBCOMMANDS:
        return Verdict(CommandLevel.LOW_WRITE, f"git {sub} 低风险写", segment)
    if sub:
        return Verdict(CommandLevel.MUTATE, f"git {sub} 可能改变仓库状态", segment)
    return Verdict(CommandLevel.READ, "git 无子命令", segment)


def classify(command: str) -> Verdict:
    """对整条命令给出风险判定，取所有片段中的最高级别。"""
    if not command or not command.strip():
        return Verdict(CommandLevel.READ, "空命令")

    for pattern, desc in _DANGER_RE:
        if pattern.search(command):
            return Verdict(CommandLevel.DANGER, f"命中危险模式：{desc}")

    stripped = _NOOP_REDIRECT_RE.sub("", command)
    if re.search(r">", stripped):
        return Verdict(CommandLevel.MUTATE, "输出重定向会写入文件，请改用文件工具")

    worst = Verdict(CommandLevel.READ, "无副作用")
    for segment in split_segments(command):
        verdict = _classify_segment(segment)
        if verdict.level > worst.level:
            worst = verdict
        if worst.level == CommandLevel.DANGER:
            return worst
    return worst
