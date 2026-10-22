<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# tools/ · 能力层

宿主实现的工具集合。模型只能产出 `{tool, args}`，这里负责真正执行并返回结构化产物。

## 加一个工具的完整顺序

1. 在对应模块写 builder，返回 `BaseTool`；
2. `registry.py` 注册；
3. **`graph/nodes/approve.py::tool_level` 登记等级**——
   未登记一律按 L3 危险处理（故意 fail-closed）。漏了这步工具会永远要确认；
4. 重跑 `python scripts/gen_contracts.py`（`docs/contracts/tools.yaml` 会更新）；
5. 补 `tests/unit/` 与（涉沙箱的）`tests/integration/` 用例。

## 约定

- **文件修改只走文件工具**（`file_write`/`file_edit`/`file_edit_many`/`file_restore`/
  `file_move`/`file_copy`/`file_delete`/`file_mkdir`），内容经 base64 在沙箱内落盘，
  不经命令行解释。不要用 `shell_exec` 改文件。
- `file_edit` 的 `old_string` 必须唯一，0 次或多次都拒绝并说明原因。
- 落盘用「同目录临时文件 + rename」原子替换；`EBUSY/ETXTBSY` 短退避重试。
- 有副作用的工具（写文件、shell 写命令、git、依赖、测试）记 `started/finished` 日志；
  崩溃后只有 `started` 的调用**不自动重放**，而是明确告知"结果未知"。
- 统一包装 `duration_ms`；跳过要显式标 `skipped=True`（如未配置 LSP 的 `code_intel`），
  不要让"没跑"看起来像"跑了但失败"。
- 工具返回 `CallArtifact` 结构化产物，**不要让调用方去解析 stdout 文本**。
- `.agent/` 禁止进入版本控制：`git_add` 拒绝暂存，`git_commit` 提交前检查；
  经 shell 的 `git add -A` 在策略层升级为需确认。

## 环境降级要明确，不要静默

`deps_list` 在 `pip` 缺失时回退 `python3 -m pip`，仍不可用则返回**可操作的环境错误**，
而不是把 `command not found (127)` 的 shell 输出丢给模型。
