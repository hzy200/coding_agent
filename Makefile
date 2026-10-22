# 先激活项目环境：conda activate agent（或 .venv）
# 需要先装开发依赖：pip install -e ".[dev]"
# （pytest 的 addopts 带 `-n auto`，缺 pytest-xdist 会直接报错；
#   `make cov` 另外需要 pytest-cov）
.PHONY: check lint test test-all cov docs check-compat

check: lint test  ## 提交前跑这个：lint + 快速测试

docs:  ## 文档一致性 + 漂移 + 环境快照（需要在 agent 环境）
	python scripts/check_docs.py --strict
	python scripts/check_drift.py
	python scripts/snapshot_env.py --check

check-compat:  ## 与 HEAD 比较破坏性契约变更（提交前跑）
	python scripts/gen_contracts.py --check-compat --base HEAD

lint:
	ruff check src tests

test:  ## 快反馈：单元 + TUI + Web（不需要 WSL / API Key）
	pytest -m "not wsl and not llm"

test-all:  ## 全量：含真实 WSL 沙箱
	pytest

cov:  ## 与 CI 同口径：快反馈子集 + 覆盖率门槛（76，见 pyproject）
	pytest -m "not wsl and not llm" --cov --cov-report=term-missing
