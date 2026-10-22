# ADR-0002：命令经 stdin 传给 `bash -l -s`，不用 `-lc` + argv

- **Status**：accepted · 事后补录
- **Date**：2026-10-22
- **Owner**：项目作者
- **Tags**：沙箱 · 与原始需求的刻意偏离

## Context

原始需求写的是「启动 `/bin/bash -lc` 解释执行」。照做之后发现问题：
Windows → `wsl.exe` → Linux 之间有一层命令行转义。把命令放进 argv 时，
引号、换行、`$` 会被各层重新解释——同一条命令换个引号写法就变成另一条命令，
而且这种失败是静默的（命令跑错了但不报错）。

备选方案：

1. **照需求用 `-lc` + argv**：实现最直白，但转义问题无法根治；
2. **把脚本写进临时文件再执行**：能绕开转义，但多一次 I/O 与清理负担，
   且文件落在工作区里会被 git 脏检查看到；
3. **经 stdin 传**：`wsl.exe -d <distro> -- /bin/bash -l -s`，脚本从 stdin 读。

## Decision

选方案 3：`bash -l -s`，脚本经 stdin 传入。行为等价（同一份脚本、同一套 shell 语义），
但完全绕开命令行转义那层。

## Consequences

### 好的

- 命令里的引号、换行、`$` 不再被任何一层重新解释；
- 不需要临时文件，也就没有清理与脏检查问题；
- 与 `-lc` 行为等价，shell 语义不变（`-l` 仍是 login shell）。

### 坏的 / 代价

- **偏离原始需求文本**，需要在验收时明确说明（已记在 `ACCEPTANCE.md §1`「一处刻意的偏离」）；
- 命令文本不再出现在进程 argv 里，`ps` 看不到——对人工排查略有损失；
- 依赖宿主正确关闭 stdin 流，否则命令会挂住（已有超时兜底）。

### 中性 / 需要留意

- 常驻 shell 会话池（`AGENT_PERSISTENT_SHELL`）复用的是同一套 stdin 协议，
  异常时自动退回一次性 `wsl.exe`。

## 复核

- **Verified by**：`pytest -m "not llm" tests/integration/test_shell_tool.py tests/unit/test_wsl_output.py`
- **Revisit when**：WSL 调用方式、参数传递或登录 shell 初始化变化时

若未来支持非 WSL 后端（如直接 Linux 部署、容器 exec），
"经 stdin 传"仍然成立且更安全，本决策不需推翻。
