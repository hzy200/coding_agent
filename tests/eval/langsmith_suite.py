"""LangSmith 全功能评测套件。

两类用例：

- `capability`：复用 tests/eval 的真实 WSL + 模型任务，评判「能不能做完」；
- `mechanism`：不调用模型的确定性探针，覆盖安全/路由/持久化/代码索引等
  不靠“通过率”体现的功能。

用法：

    python tests/eval/langsmith_suite.py --upload
    python tests/eval/langsmith_suite.py --run --only mechanism
    python tests/eval/langsmith_suite.py --run --only capability --limit 3
    python tests/eval/langsmith_suite.py --run --only all --kinds all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (_REPO_ROOT / "src", Path(__file__).resolve().parent):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from harness import (  # noqa: E402
    EvalTask,
    eval_root,
    run_task,
    task_root,
)
from tasks import TASKS  # noqa: E402
from tasks_full import TASKS_FULL  # noqa: E402
from tasks_long import LONG_TASKS  # noqa: E402

from coding_agent.codeintel.python_index import (  # noqa: E402
    build_index,
    related_test_files,
)
from coding_agent.codeintel.symbols import search_symbols  # noqa: E402
from coding_agent.config import Settings, get_settings  # noqa: E402
from coding_agent.llm.billing import billing_targets  # noqa: E402
from coding_agent.llm.ratelimit import RateLimitCoordinator  # noqa: E402
from coding_agent.llm.router import ProviderRouter  # noqa: E402
from coding_agent.sandbox.policy import (  # noqa: E402
    AUTO,
    CommandLevel,
    SessionPolicy,
    classify,
    parse_allow_rules,
)
from coding_agent.sandbox.wsl_exec import WslSandbox  # noqa: E402
from coding_agent.security.write_policy import protected_write_reason  # noqa: E402
from coding_agent.steering import SteeringCommand, SteeringController  # noqa: E402

DEFAULT_DATASET = "coding-agent-full-v1"

# 项目里已经存在的 capability 任务：默认全部纳入数据集。
CAPABILITY_IDS: tuple[str, ...] = tuple(
    task.id for task in (*TASKS, *LONG_TASKS, *TASKS_FULL)
)

# 一次覆盖主要回路的抽样：direct、跨文件、排障、隐藏测试、并发读、并发写/合并。
SMOKE_IDS: tuple[str, ...] = (
    "single_file/off_by_one",
    "cross_file/signature_change",
    "debug/circular_import",
    "deep/spec_pagination",
    "long/discount_and_audit",
    "long/parallel_modules",
)


class _FakeProvider:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text
        self.model_name = name

    async def ainvoke(self, messages, config=None, **kwargs):
        return self.text

    async def astream(self, messages, config=None, **kwargs):
        yield self.text

    def invoke(self, messages, config=None, **kwargs):
        return self.text

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    def with_retry(self, **kwargs):
        return self


def _ok(checks: dict[str, bool], detail: str = "") -> dict[str, Any]:
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "detail": detail,
    }


def _mechanism_secret_redaction() -> dict[str, Any]:
    from coding_agent.security.secrets import redact_secrets, secret_kinds

    text = "sk-" + "a" * 32 + " password=hunter2hunter2"
    redacted = redact_secrets(text)
    return _ok(
        {
            "openai_redacted": "sk-" not in redacted,
            "password_redacted": "hunter2hunter2" not in redacted,
            "kind_detected": "openai_key" in secret_kinds(text),
        },
        redacted,
    )


def _mechanism_write_policy() -> dict[str, Any]:
    return _ok(
        {
            "env_denied": protected_write_reason(".env") is not None,
            "template_allowed": protected_write_reason(".env.example") is None,
            "git_denied": protected_write_reason(".git/config") is not None,
            "normal_allowed": protected_write_reason("app/main.py") is None,
        }
    )


def _mechanism_allow_rules() -> dict[str, Any]:
    policy = SessionPolicy(
        allow_rules=parse_allow_rules("shell_exec:git status*")
    )
    allowed = policy.decide_call(
        "shell_exec", {"command": "git status --short"}, CommandLevel.MUTATE
    )
    denied = policy.decide_call(
        "shell_exec", {"command": "rm -rf /"}, CommandLevel.DANGER
    )
    return _ok(
        {
            "l2_rule_allowed": allowed == AUTO,
            "danger_not_bypassed": denied != AUTO,
        }
    )


def _mechanism_steering_persistence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        first = SteeringController(directory)
        first.submit("t", SteeringCommand("add", "检查边界", "review"))
        first.mark_paused("t")
        second = SteeringController(directory)
        commands = second.peek("t")
        paused = second.is_paused("t")
        consumed = second.consume("t")
        third = SteeringController(directory)
        return _ok(
            {
                "command_survived": [item.action for item in commands]
                == ["add"],
                "pause_survived": paused is True,
                "consumed_once": [item.action for item in consumed]
                == ["add"],
                "not_replayed": third.peek("t") == [],
            }
        )


def _mechanism_router_cost_and_capability() -> dict[str, Any]:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [_FakeProvider("expensive", "expensive"), _FakeProvider("cheap", "cheap")],
        ["expensive", "cheap"],
        ["k-expensive", "k-cheap"],
        limiter=limiter,
        costs=[(100.0, 100.0), (1.0, 1.0)],
        strategy="cost",
    )
    chosen = asyncio.run(router.ainvoke([]))

    typed = ProviderRouter(
        [_FakeProvider("agent", "agent"), _FakeProvider("worker", "worker")],
        ["agent", "worker"],
        ["k-agent", "k-worker"],
        limiter=RateLimitCoordinator(),
        task_type="worker",
        task_types=[{"agent"}, {"worker"}],
        strategy="priority",
    )
    typed_choice = asyncio.run(typed.ainvoke([]))
    return _ok(
        {
            "cost_route": chosen == "cheap",
            "task_type_route": typed_choice == "worker",
        }
    )


def _mechanism_codeintel() -> dict[str, Any]:
    sources = {
        "src/app/core.py": "def compute():\n    return 1\n",
        "src/app/api.py": (
            "from app.core import compute\n\ndef handler():\n"
            "    return compute()\n"
        ),
        "tests/test_api.py": (
            "from app.api import handler\n\ndef test_handler():\n"
            "    assert handler()\n"
        ),
    }
    index = build_index(sources)
    related = related_test_files(index, ["src/app/core.py"])
    symbols = search_symbols(sources, "compute")
    return _ok(
        {
            "import_graph": related == ["tests/test_api.py"],
            "symbol_search": any(item.name == "compute" for item in symbols),
        }
    )


def _mechanism_billing_config() -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        llm_providers=json.dumps(
            [
                {
                    "name": "primary",
                    "api_key": "llm-key",
                    "model": "m1",
                    "billing": {
                        "kind": "openai_costs",
                        "base_url": "https://billing.invalid/v1",
                        "api_key": "billing-key",
                    },
                }
            ]
        ),
    )
    targets = billing_targets(settings)
    return _ok(
        {
            "target_parsed": len(targets) == 1,
            "endpoint": bool(
                targets and targets[0].url.endswith("/organization/costs")
            ),
            "key_separate": bool(
                targets and targets[0].api_key == "billing-key"
            ),
        }
    )


def _mechanism_shell_policy() -> dict[str, Any]:
    return _ok(
        {
            "read_allowed": classify("ls").level == CommandLevel.READ,
            "rm_danger": classify("rm -rf /").level
            == CommandLevel.DANGER,
            "outside_path_escalated": classify("cat /etc/shadow").level
            == CommandLevel.MUTATE,
        }
    )


def _mechanism_lsp_framing() -> dict[str, Any]:
    from coding_agent.sandbox.wsl_exec import _parse_session_output

    raw = (
        b"noise\n"
        b"\n__AGENT_OUT_tok__\nhello\n"
        b"\n__AGENT_ERR_tok__\noops\n"
        b"\n__AGENT_END_tok__:3\n"
    )
    return _ok(
        {"framing_parsed": _parse_session_output(raw, "tok") == ("hello\n", "oops\n", 3)}
    )


def _mechanism_persistent_shell() -> dict[str, Any]:
    settings = get_settings().model_copy(
        update={
            "persistent_shell": True,
            "persistent_shell_pool": 1,
            "rate_limit_state_path": ":memory:",
        }
    )
    sandbox = WslSandbox(settings)
    try:
        first = sandbox.run("printf hello")
        second = sandbox.run("printf bye")
        return _ok(
            {
                "first_ok": first.stdout == "hello",
                "second_ok": second.stdout == "bye",
                "reused": sandbox.command_count == 1,
            }
        )
    finally:
        sandbox.close()


def _mechanism_run_usage() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        first = RateLimitCoordinator(path=Path(directory) / "limits.json")
        second = RateLimitCoordinator(path=Path(directory) / "limits.json")
        first.record_usage(
            "p1", input_tokens=10, output_tokens=5, cost_usd=0.01, run_id="r"
        )
        second.record_usage(
            "p2", input_tokens=20, output_tokens=10, cost_usd=0.02, run_id="r"
        )
        usage = first.run_usage("r")
        return _ok(
            {
                "tokens": usage["input_tokens"] == 30
                and usage["output_tokens"] == 15,
                "cost": abs(usage["cost_usd"] - 0.03) < 1e-9,
                "providers": set(usage["providers"]) == {"p1", "p2"},
            }
        )


MECHANISMS: dict[str, Any] = {
    "security/secret_redaction": _mechanism_secret_redaction,
    "security/write_policy": _mechanism_write_policy,
    "security/allow_rules": _mechanism_allow_rules,
    "steering/persistence": _mechanism_steering_persistence,
    "router/cost_and_capability": _mechanism_router_cost_and_capability,
    "codeintel/import_graph": _mechanism_codeintel,
    "billing/config": _mechanism_billing_config,
    "shell/policy": _mechanism_shell_policy,
    "lsp/framing": _mechanism_lsp_framing,
    "shell/persistent": _mechanism_persistent_shell,
    "cost/run_usage": _mechanism_run_usage,
}


def _tasks_by_id() -> dict[str, EvalTask]:
    return {
        task.id: task for task in (*TASKS, *LONG_TASKS, *TASKS_FULL)
    }


def _case_ids(kind: str) -> list[str]:
    if kind == "mechanism":
        return list(MECHANISMS)
    if kind == "capability":
        return list(CAPABILITY_IDS)
    return [*MECHANISMS, *CAPABILITY_IDS]


def _case_input(case_id: str) -> dict[str, Any]:
    if case_id in MECHANISMS:
        return {
            "case_id": case_id,
            "kind": "mechanism",
            "category": "mechanism",
        }
    task = _tasks_by_id()[case_id]
    return {
        "case_id": case_id,
        "kind": "capability",
        "category": task.category,
    }


def _prepare_env(settings: Settings) -> tuple[WslSandbox, Any, str]:
    if not WslSandbox.available(settings.wsl_distro):
        raise RuntimeError(f"WSL 发行版 {settings.wsl_distro} 不可用")
    sandbox = WslSandbox(settings)
    from coding_agent.sandbox.fs import SandboxFs
    from coding_agent.sandbox.wsl_exec import resolve_workspace

    fs = SandboxFs(sandbox, resolve_workspace(settings, sandbox))
    return sandbox, fs, eval_root(settings, sandbox)


def _run_capability(
    task_id: str,
    settings: Settings,
    sandbox: WslSandbox,
    fs: Any,
    base: str,
) -> dict[str, Any]:
    task = _tasks_by_id()[task_id]
    result = asyncio.run(
        run_task(
            task,
            settings,
            sandbox,
            fs,
            task_root(settings, sandbox, task.id),
        )
    )
    return {
        "passed": result.verdict == "passed",
        "verdict": result.verdict,
        "detail": str(result.detail)[:2_000],
        "metrics": {
            "tier": result.tier,
            "agent_seconds": result.agent_seconds,
            "judge_seconds": result.judge_seconds,
            "tool_calls": result.tool_calls,
            "writes": result.writes,
            "steps": result.steps,
            "replans": result.replans,
            "verifications": result.verifications,
            "repairs": result.repairs,
            "workflow": result.workflow,
            "tasks_planned": result.tasks_planned,
            "workers": result.workers,
            "worker_statuses": result.worker_statuses,
            "max_worker_batch": result.max_worker_batch,
            "worker_retries": result.worker_retries,
            "worker_merged": result.worker_merged,
            "worker_tool_calls": result.worker_tool_calls,
            "worker_input_tokens": result.worker_input_tokens,
            "worker_output_tokens": result.worker_output_tokens,
            "worker_seconds": result.worker_seconds,
            "worker_roles": result.worker_roles,
            "worker_merge_attempts": result.worker_merge_attempts,
            "worker_conflicts": result.worker_conflicts,
            "subagents": result.subagents,
            "subagent_statuses": result.subagent_statuses,
            "max_delegate_batch": result.max_delegate_batch,
            "subagent_input_tokens": result.subagent_input_tokens,
            "subagent_output_tokens": result.subagent_output_tokens,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "budget": result.budget,
        },
    }


def upload_dataset(client: Any, dataset: str, case_ids: list[str]) -> None:
    try:
        existing = client.read_dataset(dataset_name=dataset)
        dataset_id = existing.id
    except Exception:  # noqa: BLE001 - 不存在则创建
        created = client.create_dataset(
            dataset_name=dataset,
            description="CodingAgent 全功能评测：能力任务 + 机制探针",
        )
        dataset_id = created.id
    existing_ids = {
        str((example.inputs or {}).get("case_id", ""))
        for example in client.list_examples(dataset_id=dataset_id)
    }
    new_examples = [
        {
            "inputs": _case_input(case_id),
            "outputs": {"passed": True},
        }
        for case_id in case_ids
        if case_id not in existing_ids
    ]
    if new_examples:
        client.create_examples(dataset_id=dataset_id, examples=new_examples)
    print(f"LangSmith 数据集 {dataset}: {len(existing_ids) + len(new_examples)} 条")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangSmith 全功能评测")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--upload", action="store_true", help="上传/补齐数据集")
    parser.add_argument("--run", action="store_true", help="运行实验")
    parser.add_argument(
        "--kinds",
        default="all",
        choices=["all", "mechanism", "capability"],
    )
    parser.add_argument("--only", default="", help="只跑某个 case_id")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="跑覆盖主要能力回路的 6 个 capability 抽样",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="LangSmith 并发任务数；过高会触发模型/WSL 限流",
    )
    parser.add_argument("--experiment-prefix", default="coding-agent-full")
    args = parser.parse_args(argv)

    if not args.upload and not args.run:
        parser.print_help()
        return 2

    settings = get_settings()
    if not settings.langsmith_api_key:
        print("未配置 LANGSMITH_API_KEY")
        return 2
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project

    from langsmith import Client, evaluate

    client = Client()
    if args.smoke:
        args.kinds = "capability"
        case_ids = list(SMOKE_IDS)
    else:
        case_ids = _case_ids(args.kinds)
    if args.only:
        case_ids = [case_id for case_id in case_ids if case_id == args.only]
    if args.run and args.kinds in {"capability", "all"}:
        # capability 需要 WSL；机制用例不依赖它。
        try:
            sandbox, fs, base = _prepare_env(settings)
        except RuntimeError as exc:
            print(exc)
            return 2
    else:
        sandbox = fs = base = None

    if args.upload:
        upload_dataset(client, args.dataset, case_ids)

    if not args.run:
        return 0

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case_id = str(inputs.get("case_id", ""))
        if case_id in MECHANISMS:
            return MECHANISMS[case_id]()
        if sandbox is None:
            return {
                "passed": False,
                "detail": "capability case 需要 WSL 环境",
            }
        return _run_capability(case_id, settings, sandbox, fs, base)

    # LangSmith 的 evaluate(data=...) 需要 dataset/example 对象，不接受裸 dict。
    # 子集运行时建一个派生 dataset，而不是把全部 46 条都跑一遍。
    data: Any = args.dataset
    if args.limit > 0 or args.only or args.kinds != "all":
        chosen = case_ids[: args.limit] if args.limit > 0 else case_ids
        subset_name = (
            "smoke" if args.smoke else f"{args.kinds}-{args.only or ''}"
        )
        suffix = re.sub(
            r"[^A-Za-z0-9_.-]", "_", subset_name
        ).strip("_-")
        data = f"{args.dataset}-{suffix}" if suffix else args.dataset
        upload_dataset(client, data, chosen)
    results = evaluate(
        target,
        data=data,
        evaluators=[_evaluator],
        experiment_prefix=args.experiment_prefix,
        max_concurrency=max(1, args.concurrency),
    )
    print(results)
    return 0


def _evaluator(run: Any, example: Any) -> list[dict[str, Any]]:
    outputs = run.outputs or {}
    checks = outputs.get("checks") or {}
    passed = bool(outputs.get("passed"))
    metrics = outputs.get("metrics") or {}
    result = [
        {"key": "passed", "score": 1.0 if passed else 0.0},
        {
            "key": "mechanism_checks",
            "score": (
                1.0
                if all(bool(value) for value in checks.values())
                else 0.0
            )
            if checks
            else 1.0,
        },
    ]
    if metrics:
        result.append(
            {
                "key": "tool_calls",
                "score": float(metrics.get("tool_calls", 0) or 0),
            }
        )
        result.append(
            {
                "key": "writes",
                "score": float(metrics.get("writes", 0) or 0),
            }
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
