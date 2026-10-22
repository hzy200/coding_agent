"""秘密文件读保护。

写保护（write_policy）解决“不要覆盖密钥”；这里解决“不要把密钥读进模型
上下文”。默认拒绝 `.env`、私钥与证书；`.env.example` 等模板放行，因为
它们本来就是给人看的。

这是模式匹配的 best-effort 防线，不是内核级隔离。shell 的越界/变量升级
仍由 `sandbox/policy.py` 负责。
"""

from __future__ import annotations

import fnmatch
import posixpath

from coding_agent.config import DEFAULT_READ_DENY_GLOBS

_ALLOWED_OVERRIDES = (
    ".env.example",
    ".env.sample",
    ".env.template",
)


def _normalize(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return posixpath.normpath(text).lstrip("/")


def _matches(path: str, glob: str) -> bool:
    # fnmatch 的 `*` 本身就能跨 `/`；`**/x` 与 `x/**` 因此都能直接匹配。
    # 不再对 `/**` 做字面前缀特判 —— 那会让 `**/.aws/**` 这类规则永远失效。
    if "/" in glob:
        return fnmatch.fnmatch(path, glob)
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], glob)


def _globs(extra_globs: str = "") -> list[str]:
    values = [
        item.strip()
        for item in str(DEFAULT_READ_DENY_GLOBS).split(",")
        if item.strip()
    ]
    values.extend(
        item.strip()
        for item in str(extra_globs or "").split(",")
        if item.strip()
    )
    return values


def protected_read_reason(
    path: str, extra_globs: str = ""
) -> str | None:
    normalized = _normalize(path)
    if not normalized:
        return None
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _ALLOWED_OVERRIDES:
        return None
    for glob in _globs(extra_globs):
        if _matches(normalized, glob):
            return (
                f"{normalized} 命中秘密文件读保护（{glob}）。"
                "如需检查配置，请读取对应的 .example/.sample 模板，"
                "或由管理员调整 AGENT_READ_DENY_GLOBS。"
            )
    return None


def is_protected_read_path(path: str, extra_globs: str = "") -> bool:
    return protected_read_reason(path, extra_globs) is not None


# 用词法提示兜住「glob 形状」的读取：`cat .env*` / `cat *.key` 的参数没有
# 精确命中某个受保护 glob（因为 `*` 是 shell 展开前的字面量），但显然在
# 指向秘密文件。这里只做保守提示，命中后由策略升级为人工确认，不直接拒绝。
_SECRET_HINTS = (
    ".env",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "credentials",
    "service-account",
    "service_account",
    ".tfstate",
    ".jks",
    ".keystore",
    ".gpg",
    ".aws",
    ".kube",
    "gcloud",
)


def might_reference_secret(path: str) -> bool:
    """字面量或通配符形状是否**可能**指向秘密文件。

    仅用于 shell 命令参数的升级判断：`cat .env*`、`cat *.key` 这类写法
    在 shell 展开前无法精确匹配，但前缀/扩展名已经暴露了意图。
    模板文件（`.env.example` 等）仍然放行。
    """
    normalized = _normalize(path)
    if not normalized:
        return False
    if normalized.rsplit("/", 1)[-1] in _ALLOWED_OVERRIDES:
        return False
    return any(hint in normalized for hint in _SECRET_HINTS)
