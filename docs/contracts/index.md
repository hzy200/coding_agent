<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。改代码后重跑生成，CI 会校验一致性。 -->

# L0 可执行事实层 · 契约索引

这一层的共同特征：**能编译、能跑、能校验**。它们不是文档，
但它们是 AI 最可靠的文档。全部由源码生成，禁止手改。

| 契约 | 事实源 | 产出 | 校验方式 |
|---|---|---|---|
| 领域事件 | `events.py` | [`events.json`](events.json) / [`events.md`](events.md) | pydantic 模型 + 前端 AST 扫描 |
| 工具等级 | `approve.py::tool_level` | [`tools.yaml`](tools.yaml) | 未登记工具按 L3 fail-closed |
| 审计记录 | `audit/models.py` | [`audit.yaml`](audit.yaml) | JSONL + 哈希链 `agent audit --verify` |
| 配置项 | `config.py` | [`config.json`](config.json) / [`config.md`](config.md) | `config.json` 可校验配置项是否存在 |
| 依赖 | `pyproject.toml` + `uv.lock` | [`dependencies.md`](dependencies.md) | `pip install -e .` |
| 测试套件 | `tests/` | 见 `ACCEPTANCE.md` 第 1 节 | `pytest -m "not llm"` |
| 流水线 | `.github/workflows/` | [`../CI.md`](../CI.md) | GitHub Actions |

- 事件类型：**27** 个
- 配置项：**150** 个
- 静态分级工具：**25** 个（另有 `shell_exec` 按命令文本实时分类）

重跑生成：`python scripts/gen_contracts.py`。
CI 校验：`python scripts/gen_contracts.py --check`。
