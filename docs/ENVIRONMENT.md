# 环境配置

从零把项目跑起来所需的**完整流程**，附当前环境的实测版本快照，供复现。

> 命令速查见 [README 快速开始](../README.md#快速开始)；这份文档讲**为什么这么做、装了什么、版本是多少**。

## 1. 环境概览

| 项 | 值 |
|---|---|
| Python 环境 | conda 环境 **`agent`**，Python **3.11.15** |
| 环境路径（本机） | `D:\anaconda3\envs\agent` |
| pip | 26.1.2 |
| 已装包数 | 239（含传递依赖） |
| 执行沙箱 | WSL2 + Ubuntu（非 root 用户），见 [USAGE.md 沙箱](USAGE.md#沙箱) |

依赖的下限与范围由 `pyproject.toml` 声明；本文第 4 节的版本是本机的**实测值**，仅作复现参考。

## 2. 前置条件

1. **Anaconda / Miniconda**（提供 `conda`）。
2. **WSL2 + Ubuntu 发行版**（agent 所有命令在沙箱里执行）：
   ```powershell
   wsl --install -d Ubuntu
   ```
   确保发行版内是**非 root** 普通用户（`agent doctor` 会检查）。
3. 一个 DeepSeek API Key（`https://platform.deepseek.com`）。
4. （可选）**bubblewrap**，用于内核级隔离（`AGENT_SHELL_SANDBOX=bwrap`）：
   ```bash
   wsl -d Ubuntu -- sudo apt-get install -y bubblewrap
   ```

## 3. 从零配置（完整流程）

```bash
# 1) 创建并激活环境
conda create -n agent python=3.11 -y
conda activate agent

# 2) 在项目根目录，可编辑安装 + 各组依赖
pip install -e ".[dev,ui,web]"

# 3) 配置密钥/参数
cp .env.example .env          # 填入 DEEPSEEK_API_KEY

# 4) 初始化沙箱工作区（会创建 $HOME/agent-ws 并打印实际路径）
agent sandbox-init

# 5) 环境自检（不需要 API Key）
agent doctor

# 6) 验证开发链
make check                    # ruff + 快反馈测试
```

`pip install -e` 是**可编辑安装**：源码改动立即生效，`agent` 命令随之更新。

### extra 组

| extra | 装了它才能用 |
|---|---|
| （无） | `agent doctor` / `sandbox-init` / `chat` / `run` 等运行时命令 |
| `dev` | `pytest` / `ruff` / `pytest-xdist` / `pytest-cov` —— 跑测试与 lint |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |

> ⚠️ 激活环境是**必须**的：`agent` 命令装在环境自己的 `Scripts/` 下
> （`D:\anaconda3\envs\agent\Scripts`），未激活时不在 PATH，直接敲 `agent` 会「找不到命令」。

## 4. 实测版本快照

当前环境（2026-10-04）直接依赖的 install 版本：

| 分组 | 包 | 版本 |
|---|---|---|
| 运行时 | `langgraph` | 1.2.9 |
| 运行时 | `langgraph-checkpoint-sqlite` | 3.1.1 |
| 运行时 | `aiosqlite` | 0.22.1 |
| 运行时 | `langchain-core` | 1.5.1 |
| 运行时 | `langchain-openai` | 1.4.1 |
| 运行时 | `langsmith` | 0.10.10 |
| 运行时 | `pydantic` | 2.13.4 |
| 运行时 | `pydantic-settings` | 2.14.2 |
| 运行时 | `typer` | 0.27.0 |
| 运行时 | `rich` | 15.0.0 |
| 运行时 | `python-dotenv` | 1.2.2 |
| 开发 | `pytest` | 9.1.1 |
| 开发 | `pytest-xdist` | 3.8.0 |
| 开发 | `pytest-cov` | 7.1.0 |
| 开发 | `ruff` | 0.16.10 |
| UI | `textual` | 8.2.8 |
| Web | `fastapi` | 0.141.1 |
| Web | `uvicorn` | 0.52.1 |

Python 3.11.15 · pip 26.1.2 · 环境内共 239 个包。

## 5. 复现与导出

`pyproject.toml` 只约束版本**范围**；要精确复现，导出实测版本：

```bash
# 全环境精确版本（含传递依赖）
pip freeze > requirements.lock.txt

# 或只导出显式安装的包（可用 conda 重建）
conda env export --from-history -n agent > environment.yml
```

重建：

```bash
conda env create -f environment.yml -n agent   # 或 requirements.lock.txt 方式
conda activate agent
pip install -e ".[dev,ui,web]"                 # 再装回本项目的可编辑安装
```

> 本仓库**未提交** `requirements.lock.txt` / `environment.yml` —— 它们把本机全部传递依赖
> 一起冻结，跨平台恢复性一般。需要时按上面命令自行导出即可。

## 6. 用普通 venv 替代 conda

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,ui,web]"
```

后续安装与运行命令与 conda 完全一致。`scripts/demo.py` 会依次在当前环境 / PATH / `.venv`
中定位 `agent` 入口，两种布局都支持（用 `sysconfig` 定位环境自己的 `Scripts/`）。

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `agent: command not found` | 未激活环境 —— `conda activate agent`（或 `.venv` 激活） |
| `pytest: error: unrecognized arguments: -n` | 缺 `pytest-xdist`（`-n auto` 依赖它）—— `pip install -e ".[dev]"` |
| `pip install` 很慢 / 超时 | 换国内镜像源，或 `pip install -e . -i <index-url>` |
| `AGENT_SHELL_SANDBOX=bwrap` 报「不可用」 | 沙箱内没装 bubblewrap —— `wsl -d Ubuntu -- sudo apt-get install -y bubblewrap`；或改回 `off` |
| `agent doctor` 报沙箱用户是 root | 该 WSL 发行版默认用户为 root，隔离形同虚设 —— 改用普通用户 |
| 沙箱内命令找不到工具（如 `rg`） | 可选安装：`ripgrep` 装了检索更快（`doctor` 会显示当前后端） |

## 8. 相关文档

- [README.md](../README.md) —— 项目概览与快速开始
- [USAGE.md](USAGE.md) —— 配置来源、工作流、故障排查
- [THREAT_MODEL.md](THREAT_MODEL.md) —— 沙箱隔离的威胁模型与 `bwrap` 边界
