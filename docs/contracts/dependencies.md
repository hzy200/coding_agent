<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。改代码后重跑生成，CI 会校验一致性。 -->

# 依赖契约

事实源：`pyproject.toml`（声明范围）+ `uv.lock`（精确锁定）。

> **实测版本快照不在这里**：那是环境属性，会随机器变化。
> 精确复现请跑 `pip freeze > requirements.lock.txt`，见 `ENVIRONMENT.md`。

## 运行时依赖（必装）

- `langgraph>=0.2.60`
- `langgraph-checkpoint-sqlite>=2.0`
- `aiosqlite>=0.20`
- `langchain-core>=0.3.0`
- `langchain-openai>=0.2.0`
- `langsmith>=0.1.100`
- `pydantic>=2.8`
- `pydantic-settings>=2.4`
- `typer>=0.12`
- `rich>=13.7`
- `python-dotenv>=1.0`

## 可选 extras

| extra | 装了它才能用 | 依赖 |
|---|---|---|
| `dev` | pytest / ruff / pytest-xdist / pytest-cov —— 跑测试与 lint | `pytest>=8.2`, `ruff>=0.5`, `pytest-xdist>=3.6`, `pytest-cov>=5.0` |
| `ui` | `agent tui`（Textual） | `textual>=0.80` |
| `web` | `agent web`（FastAPI + uvicorn） | `fastapi>=0.115`, `uvicorn>=0.30` |

