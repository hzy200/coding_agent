<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# llm/ · 模型接入

模型是外部依赖：它的失败、限流、成本和上下文都需要宿主兜底。
这一层只做接入与预算，**不决定任务怎么编排**。

## 职责与入口

| 文件 | 入口 | 作用 |
|---|---|---|
| `deepseek.py` | `build_llm` / `with_json_output` | DeepSeek（OpenAI 兼容）客户端与结构化输出 |
| `router.py` | `ProviderRouter` | 多 provider 选择、fallback、冷却 |
| `ratelimit.py` | `RateLimitCoordinator` / `GLOBAL_LIMITER` | 跨进程 429 冷却，文件锁 + 原子替换 |
| `context.py` | `trim_messages` / `estimate_tokens` | 字符 + 估算 token 双预算裁剪 |
| `billing.py` | `fetch_billing` | provider 账单对账 |
| `prompts.py` | `compose_system_prompt` | 系统提示与反馈格式 |

## 禁区

- `trim_messages` **只截断内容，绝不丢弃消息**。丢掉消息会破坏
  `tool_calls` ↔ `tool_call_id` 配对，下一轮 OpenAI 兼容接口直接报错。
- 429 冷却要写入共享状态（文件锁 + 原子替换），否则多个 `agent` 进程会互相撞；
  读写失败退化为进程内状态，但不能阻塞主流程。
- 密钥、账单响应、原始 provider 错误不要进入模型上下文；错误分类走 `recovery.py`。
- `bind_tools` / `with_structured_output` 必须同时作用到每个 provider，
  否则 fallback 后工具面会悄悄消失。
- 不要引入 tokenizer 依赖：上下文预算是字符 + 估算 token，刻意保持零额外依赖。
- 改提示词会影响评测基线；至少跑 `tests/unit/test_nodes_llm.py`，真实模型用例另有 `llm` 标记。

## 依赖与测试

- 依赖 `config`、`concurrency`、`recovery`、`filelock`；
  不允许 import `graph` / `orchestrator` / `tools` / `sandbox`。
- 回归：`pytest -m "not llm" tests/unit/test_llm_router.py tests/unit/test_ratelimit.py tests/unit/test_context.py tests/unit/test_billing.py`
- 真实模型用例：`pytest -m llm`（需要 `DEEPSEEK_API_KEY`，默认排除）。
