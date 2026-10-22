<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# security/ · 内容级防护

**不负责命令分级与沙箱执行**——那是 `sandbox/`。这里只管三件事：
模型能看到什么、能写哪些文件、日志里会留下什么。

## 职责与入口

| 文件 | 入口 | 作用 |
|---|---|---|
| `secrets.py` | `redact_secrets` / `secret_kinds` | 审计落盘、错误详情与工具结果回灌前的密钥脱敏 |
| `read_policy.py` | `protected_read_reason` / `is_protected_read_path` / `might_reference_secret` | 拒绝把密钥读进模型上下文 |
| `write_policy.py` | `protected_write_reason` | 即使有 `write_scope`，也拒绝直接写密钥、`.env`、`.git` 内部 |

## 禁区

- 这是 **best-effort 模式匹配**，不是熵检测器，也不是内核隔离。
  不要在评审里把它当成 T2（防主动越狱）的保证——那需要 OS 隔离兜底。
- 默认 deny globs 只能收紧，不能放宽；用户追加规则走配置，不要改默认值。
- 脱敏只影响“模型看到的那一份”；源文件编辑仍用**真实内容**匹配。
  不要为了脱敏破坏 `file_edit` 的精确替换语义。
- `.env.example` / `.env.sample` 必须放行：它们本来就是给人看的模板。
- 新增 secret pattern 必须同时有正向与负向用例，避免把正常代码误报。

## 依赖与测试

- 只依赖 `config` 与标准库；不要 import `sandbox` / `tools` / `graph`。
- 回归：`pytest -m "not llm" tests/unit/test_secrets.py tests/unit/test_read_protection.py tests/unit/test_write_policy.py`
- 安全边界与残余风险见 `docs/THREAT_MODEL.md`；改这里之前先对照威胁模型。
