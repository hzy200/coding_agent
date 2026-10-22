# ADR-0005：自动验证不执行 manifest 派生的命令

- **Status**：accepted · 事后补录
- **Date**：2026-10-22
- **Owner**：项目作者
- **Tags**：安全 · 验证

## Context

自动验证（`verify` 节点）需要知道"跑什么"。测试命令可以从项目 manifest 推导：
`Makefile` 的 `test` 目标、`package.json` 的 `scripts.test` 等。

问题在于：**自动 verify 不走审批**。如果它执行 manifest 派生的命令，
那么在工作区里写一个恶意 `Makefile` 就等于拿到了绕过审批的任意代码执行路径——
而智能体本身、以及它读到的任何第三方仓库内容，都可能促成这件事。

## Decision

- 自动 `verify` 传 `allow_manifest=False`：只认**命令文本完全由宿主确定**的那几条
  （Cargo / go / npm / pytest / make 的固定形态）。
- 只有**走审批的 `run_tests` 工具**才允许 `allow_manifest=True`
  （即 manifest 派生的 `make test` / `npm test`）。
- 自动 verify 独立于审批；模型主动调 `run_tests` 才走 L2。

代码位置：`tools/testrun.py::_detect(..., allow_manifest=)`，
`graph/nodes/verify.py` 固定传 `False`。

## Consequences

### 好的

- 堵掉"写恶意 Makefile → 绕过审批执行任意命令"这条路径；
- 自动验证的命令不受项目内容影响，模型影响不了跑什么。

### 坏的 / 代价

- **覆盖面变窄**：只靠 manifest 定义测试命令的项目（尤其 Makefile-only），
  自动验证会识别不出命令，降级为 `kind=syntax`（语法解析级）；
- 缓存键要带上 `allow_manifest`（两种口径答案可能不同），略微增加复杂度。

### 中性 / 需要留意

- 识别不出测试命令但有源码时降级为语法解析级验证；什么都没跑时 `kind=none`，
  事件与审计的 `ok` 是**三态**（True/False/None）——不要用 `None` 当 `False`。

## 复核

- **Verified by**：`pytest -m "not llm" tests/unit/test_verify_node.py tests/unit/test_testrun_parse.py`
- **Revisit when**：测试命令识别或自动验证范围变化时

若出现"大量项目只能用 manifest 定义测试"的实际需求，
正确的做法是让那条路径**显式走一次审批**，而不是放开自动 verify。
