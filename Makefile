# 先激活项目环境：conda activate agent（或 .venv）
# 需要先装开发依赖：pip install -e ".[dev]"
# （pytest 的 addopts 带 `-n auto`，缺 pytest-xdist 会直接报错；
#   `make cov` 另外需要 pytest-cov）
.PHONY: check lint test test-all cov

check: lint test  ## 提交前跑这个：lint + 快速测试

lint:
	ruff check src tests

test:  ## 快反馈：单元 + TUI + Web（不需要 WSL / API Key）
	pytest -m "not wsl and not llm"

test-all:  ## 全量：含真实 WSL 沙箱
	pytest

cov:  ## 与 CI 同口径：快反馈子集 + 覆盖率门槛（76，见 pyproject）
	pytest -m "not wsl and not llm" --cov --cov-report=term-missing
