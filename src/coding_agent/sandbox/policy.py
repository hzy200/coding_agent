"""命令安全分级。

判定权完全在宿主，不依赖 LLM 的自我申报。复合命令按段分类取最高级别；
解析失败或无法识别的一律降级为 MUTATE（需要人工确认），而不是放行。

除了「命令名」的风险，还要看「参数是否越界」：`cat` 是只读命令，但
`cat ~/.ssh/id_rsa` 会把工作区外的私钥读走。因此对本来会直接放行（L0/L1）
的命令再查一层，出现命令替换/变量展开、工作区外路径、或 `find -exec`
就升级到 L2，交人工确认。这是**词法级 best-effort 防线，不是内核级隔离** ——
要做真正的隔离得靠 mount namespace（bwrap），当前不在范围内。
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
# 会话策略：把风险等级翻译成「放行 / 询问 / 拒绝」
# --------------------------------------------------------------------------

AUTO = "auto"          # 直接执行
ASK = "ask"            # 挂起，等人工确认
DENY = "deny"          # 直接拒绝

APPROVED = "approved"  # 人工确认通过（approval_gate 写入）
DENIED = "denied"      # 人工拒绝

APPROVAL_ASK = "ask"
APPROVAL_APPROVE = "approve"
APPROVAL_DENY = "deny"


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """一次会话的授权范围。

    刻意不做成全局单例：策略必须是图的显式输入，否则「谁批的」就说不清楚，
    审计也无从追溯。
    """

    allow_write: bool = False
    approval_mode: str = APPROVAL_ASK

    def decide(self, level: CommandLevel) -> str:
        if level <= CommandLevel.READ:
            return AUTO

        if level == CommandLevel.LOW_WRITE:
            # L1 由 --write 一次性授权，不逐条询问
            return AUTO if self.allow_write else DENY

        # L2/L3
        if self.approval_mode == APPROVAL_APPROVE:
            return AUTO
        if self.approval_mode == APPROVAL_DENY:
            return DENY
        return ASK


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


# --------------------------------------------------------------------------
# 引号感知：危险模式与重定向检查只看「会被执行」的部分
#
# 原始全文匹配会把引号里的普通字符串当成命令 —— `grep -rn "rm -rf" docs/`
# 只是在搜索，却会被判 L3。这里把引号内的字面量屏蔽掉，但保留真正会执行的
# 命令替换（$()/反引号）：`echo "$(rm -rf /)"` 里的内容照样会被执行。
# --------------------------------------------------------------------------

# 把字符串当命令执行的包装（`sh -c "..."`）：其引号内容必须照常扫描
_SHELL_WRAPPER_RE = re.compile(r"\b(?:ba|z|da|k)?sh\b[^\n]*\s-[A-Za-z]*c\b")


def _match_paren(text: str, open_idx: int) -> int:
    """返回与 text[open_idx]（`(`）配对的 `)` 下标；找不到则返回末尾。"""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _keep_executables(inner: str) -> str:
    """双引号内只有 $()/反引号会被执行：保留它们，其余字面量换成空格。"""
    out: list[str] = []
    i, n = 0, len(inner)
    while i < n:
        if inner.startswith("$(", i):
            end = _match_paren(inner, i + 1)
            out.append(inner[i : end + 1])
            i = end + 1
        elif inner[i] == "`":
            j = inner.find("`", i + 1)
            j = n if j == -1 else j + 1
            out.append(inner[i:j])
            i = j
        elif inner[i] == "\\" and i + 1 < n:
            i += 2
        else:
            out.append(" ")
            i += 1
    return "".join(out)


def _mask_quoted(command: str) -> str:
    """屏蔽引号内的字面量，保留命令替换内容。

    单引号内一切皆字面量，整段屏蔽；双引号内只有 $()/反引号会执行，保留之。
    屏蔽后长度与下标仍大致对齐，便于复用既有的正则。
    """
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            out.append(command[i + 1])
            i += 2
        elif ch == "'":
            j = command.find("'", i + 1)
            j = n if j == -1 else j
            out.append(" " * (j - i + 1))
            i = j + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            out.append('"' + _keep_executables(command[i + 1 : j]) + '"')
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# 自动放行命令的越界/隐藏命令升级
#
# 只读白名单管的是「命令名」，管不了「参数」：cat/find/grep 都是只读命令，
# 却能读到工作区外。这里是词法级的 best-effort 兜底 —— 不做硬拦截，
# 而是升级到 L2 交人工确认，既挡住静默越权，又不误杀合法的一次性读取。
# --------------------------------------------------------------------------

_FIND_COMMANDS = frozenset({"find", "fd"})
# 这些动作会在工作区里外执行/删除文件，只读命令名兜不住
_FIND_EXEC_FLAGS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
)
# 设备/伪文件不是文件系统数据，读写无副作用
_SAFE_EXTERNAL_PATHS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

# agent 自己的工作目录（备份/审计）。shell 不该碰它，git 也不该把它纳入版本控制。
# 与 snapshots.AGENT_STATE_DIRNAME 一致；这里不跨模块 import，避免 policy 依赖存储层。
_AGENT_STATE_DIR = ".agent"
# `git add` 的整树暂存：会隐式把 .agent/ 一起加进索引
_GIT_ADD_ALL = frozenset({"-A", "--all", "-a", "."})

# 变量与命令替换：可能藏起真正的路径或命令（`cat $HOME/.ssh/id_rsa`）
_HIDDEN_EXPANSION_RE = re.compile(r"\$[A-Za-z_{(]|`")


def _is_external_path_token(token: str) -> bool:
    """看起来像「工作区外的路径」。

    相对路径按 cwd（= 工作区）解析，天然在界内，不在此列；只认绝对路径、
    家目录 `~` 与向上一级穿越 `..`。工作区内的绝对路径也会被算进来 ——
    这是有意的保守：让模型用相对路径或文件工具，代价只是一次确认。
    """
    if not token or token in _SAFE_EXTERNAL_PATHS:
        return False
    return (
        token.startswith("/")
        or token == "~"
        or token.startswith("~/")
        or token == ".."
        or token.startswith("../")
        or "/../" in token
        or token.endswith("/..")
    )


def _escalate_auto_command(command: str) -> Verdict | None:
    """对会直接放行的命令做越界检查；命中返回升级后的 Verdict，否则 None。"""
    if _HIDDEN_EXPANSION_RE.search(command):
        return Verdict(
            CommandLevel.MUTATE,
            "命令含变量或命令替换，可能隐藏工作区外的访问，需要人工确认",
        )
    for segment in split_segments(command):
        words = _tokens(segment)
        if not words:
            continue
        name = posixpath.basename(words[0])
        if name in _FIND_COMMANDS and any(w in _FIND_EXEC_FLAGS for w in words[1:]):
            return Verdict(
                CommandLevel.MUTATE,
                f"{name} 的 -exec/-delete 会在工作区外执行或删除，需要人工确认",
                segment,
            )
        # `git add -A` / `git add .` 会隐式把 .agent/ 纳入索引 ——
        # git_add 工具有过滤，但经 shell 走的是同一条 git，必须在这里补上
        if name == "git" and words[1:2] == ["add"] and any(w in _GIT_ADD_ALL for w in words[2:]):
            return Verdict(
                CommandLevel.MUTATE,
                f"git add 整树暂存可能把 {_AGENT_STATE_DIR}/ 纳入，需要人工确认",
                segment,
            )
        for word in words[1:]:
            # 选项值也可能带路径，例如 --file=/etc/shadow
            candidate = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
            if candidate == _AGENT_STATE_DIR or candidate.startswith(_AGENT_STATE_DIR + "/"):
                return Verdict(
                    CommandLevel.MUTATE,
                    f"引用了 agent 内部目录 {_AGENT_STATE_DIR}/（备份与审计），需要人工确认",
                    segment,
                )
            if _is_external_path_token(candidate):
                return Verdict(
                    CommandLevel.MUTATE,
                    f"只读命令引用了工作区外的路径：{candidate}（需要确认）",
                    segment,
                )
    return None


def classify(command: str) -> Verdict:
    """对整条命令给出风险判定，取所有片段中的最高级别。"""
    if not command or not command.strip():
        return Verdict(CommandLevel.READ, "空命令")

    # 危险模式只看会被执行的部分：引号里的普通字符串不算命令。
    # 但 `sh -c "..."` 会把引号内容当命令执行，这时退回原始全文扫描。
    masked = _mask_quoted(command)
    danger_target = command if _SHELL_WRAPPER_RE.search(masked) else masked
    for pattern, desc in _DANGER_RE:
        if pattern.search(danger_target):
            return Verdict(CommandLevel.DANGER, f"命中危险模式：{desc}")

    stripped = _NOOP_REDIRECT_RE.sub("", masked)
    if re.search(r">", stripped):
        return Verdict(CommandLevel.MUTATE, "输出重定向会写入文件，请改用文件工具")

    worst = Verdict(CommandLevel.READ, "无副作用")
    for segment in split_segments(command):
        verdict = _classify_segment(segment)
        if verdict.level > worst.level:
            worst = verdict
        if worst.level == CommandLevel.DANGER:
            return worst

    # 本来会直接放行的命令，再看参数是否越界（命令名只读 ≠ 参数安全）
    escalated = _escalate_auto_command(command)
    if escalated is not None and escalated.level > worst.level:
        return escalated
    return worst

