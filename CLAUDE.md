# CLAUDE.md

本项目的 AI 指令集中在 [`AGENTS.md`](AGENTS.md)。

`instructions.py` 会同时发现 `AGENTS.md` 与 `CLAUDE.md` 并合并注入，
为避免同一份规则被读取两次、白白挤占上下文，这里只保留这一行指针。

子目录另有分层指令：`src/coding_agent/{graph,orchestrator,sandbox,tools}/AGENTS.md`。
契约类事实见 `docs/contracts/`（代码生成），术语见 `docs/glossary.md`。
