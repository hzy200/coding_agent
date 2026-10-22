# ADR-0006：评测判定固定用 stdlib unittest，不用 pytest

- **Status**：accepted
- **Date**：2026-10-05
- **Owner**：评测维护者
- **Tags**：评测 · 事故驱动

## Context

判定命令原本是 `python3 -m pytest`，而当时沙箱里**没有 pytest**。
结果是：所有任务都判失败，而种子自检看到的是「20/20 未解决」——
一个**看起来干净、实际什么都没测出来**的结果。

这是评测最关键的一类失效：**假阴性**。从结果看只是"智能体没做出来"，
实际是尺子本身坏了。后面每一句"提升了 X 个百分点"都会是假的。

## Decision

判定**零依赖、到哪都能跑**：

- 任务里的测试一律写成 stdlib `unittest` 的 `TestCase`；
- 判定命令固定 `python3 -m unittest`，且**用显式模块名**，不用 `discover`；
- 两道检查：`preflight()` 先在一个已知正确的项目上确认「判定能通过」，
  `--verify-seeds` 再确认「种子确实不通过」。

### 为什么用显式模块名而不是 `discover`

`unittest discover` 在多层包的大仓库上会踩一串问题：同名 basename 冲突、
子目录可导入性、收集顺序，以及最要命的——**智能体写在 `tests/` 里的临时测试会被一并收进来**
（`prepare_for_judging` 只删根级 `test_*.py`）。收窄到指定模块后，这一整类风险结构性消失。

## Consequences

### 好的

- 判定不受沙箱装了什么影响；
- 双向自检能拦住假阳性与假阴性两种相反的错误；
- 判定不可被篡改（判分前用纯净副本覆盖测试文件）。

### 坏的 / 代价

- 写测试不能用 pytest 的 fixture / parametrize，表达力略弱；
- 每加任务要同步维护显式模块名列表。

### 中性 / 需要留意

- **沙箱里有没有 pytest 是另一回事**：智能体自己的自动验证
  （`detect_test_command`）要求 `python3 -c 'import pytest'` 成功。
  沙箱没有 pytest 时，测到的是「没有修复循环的智能体」，
  同一份通过率含义完全不同——所以 runner 会拒绝把这种数字写成基线。

## 复核

- **Verified by**：`pytest -m "not llm" tests/unit/test_eval_harness.py`
- **Revisit when**：评测判定命令或沙箱镜像（pytest 可用性）变化时

除非 stdlib unittest 出现无法绕过的表达力瓶颈，否则不改。
**任何"改成 pytest"的改动都必须先跑 `--verify-seeds` 双向自检。**
