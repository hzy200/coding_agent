"""文件写入的内容级策略。

`write_scope` 解决“能改哪些目录”，这里补一层“哪些文件即使给了 scope 也不该
直接写”。默认只拦明显的高风险目标（密钥、`.env`、`.git` 内部），避免误伤
正常业务代码；用户可通过 `AGENT_WRITE_DENY_GLOBS` 追加规则。
"""

from __future__ import annotations

import fnmatch
import posixpath

DEFAULT_WRITE_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".ssh/**",
    ".git/**",
    ".agent/**",
    # 常见凭据/基础设施状态文件：写坏它们比读走更难恢复
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "**/credentials",
    "credentials.json",
    "**/credentials.json",
    "*service-account*.json",
    "*service_account*.json",
    ".aws/**",
    "**/.aws/**",
    ".kube/**",
    "**/.kube/**",
    ".config/gcloud/**",
    "**/.config/gcloud/**",
    ".docker/config.json",
    "**/.docker/config.json",
    "*.tfstate",
    "*.tfstate.backup",
    "*.jks",
    "*.keystore",
)

# 这些是“给人看的模板”，命中 `.env.*` 时放行。
_ALLOWED_OVERRIDES: tuple[str, ...] = (
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
    # 与 read_policy 保持一致：fnmatch 的 `*` 可跨 `/`，`**/x` 直接可用。
    if "/" in glob:
        return fnmatch.fnmatch(path, glob)
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], glob)


def protected_write_reason(
    path: str, extra_globs: str = ""
) -> str | None:
    """命中保护规则时返回可读原因；否则返回 None。"""
    normalized = _normalize(path)
    if not normalized:
        return None
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _ALLOWED_OVERRIDES:
        return None

    globs = list(DEFAULT_WRITE_DENY_GLOBS)
    globs.extend(
        item.strip()
        for item in str(extra_globs or "").split(",")
        if item.strip()
    )
    for glob in globs:
        if _matches(normalized, glob):
            return (
                f"{normalized} 命中敏感写保护（{glob}）。"
                "如确需修改，请调整 AGENT_WRITE_DENY_GLOBS 或由用户手工处理。"
            )
    return None
