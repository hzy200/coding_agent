# 项目复杂度分析

> 面向维护：用静态度量给出规模、复杂度分布与热点，并区分**本质复杂度**与**偶然复杂度**。
> 度量口径：Python `ast` 静态分析；圈复杂度为 McCabe 近似（判定点 + 布尔项）；
> 嵌套深度按块计（注意 `elif` 在 AST 里是嵌套 `orelse`，会虚高长分发链的深度，正文已据此校正）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | 52 |
| 源码行数 | 7621 |
| 函数/方法 | 309 |
| 测试文件 / 行数 | 47 / 8887 |
| 测试:源码 | **1.17 : 1** |
| 测试用例 | **992**（不含 LLM），覆盖率 **90.5%** |

测试体量与源码相当，属于**健康的测试密度**；90.5% 覆盖率说明度量到的分支大多数有验证。

> **度量口径需重算**：上表中「源码行数 / 函数方法数 / 测试行数 / 测试:源码」四项
> 是 `replan` 节点落地**之前**测的，之后又新增了 `graph/nodes/replan.py`（181 行）
> 与若干用例，这四项已偏小。本次只更正了可实测的「测试用例」「覆盖率」两格；
> 其余四项待用脚本重算（当前无配套脚本，见 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) 的工程债一节）。

## 2. 复杂度分布（309 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **11 行** | 18.3 | >40 行：**27** 个（8.7%） |
| 圈复杂度 | **3** | 4.7 | >10：**28** 个（**9.1%**），最大 48 |

**结论：复杂度高度集中**。约 91% 的函数圈复杂度 ≤10、一半以上 ≤11 行——绝大多数代码是"一眼能读完"的小函数。复杂度集中在约 9% 的函数里。

## 3. 复杂度热点

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `tools/files.py::build_file_tools` | 269 | **48** | 装配（4 个闭包工具） | CC 高但**线性**：每个工具一段，认知负担低 |
| `runtime.py::_stream` | 214 | **39** | **真实逻辑** | **唯一"重型"函数**：事件翻译+审计+预算+错误+用量 |
| `tools/git.py::build_git_tools` | 152 | 19 | 装配 | 同上 |
| `web/app.py::create_app` | 106 | 12 | 装配（路由注册） | 线性注册 |
| `tools/search.py::build_search_tools` | 102 | 15 | 装配 | |
| `cli/app.py::doctor` | 94 | 14 | 顺序检查清单 | 扁平 |
| `cli/app.py::render` / `_make_renderer` | 80/89 | 29/29 | **if/elif 分发** | 事件→渲染映射，认知负担低 |
| `tui/app.py::_apply` | 81 | 28 | **if/elif 分发** | 同上 |
| `graph/nodes/tools.py::run_tools` | 66 | 17 | 真实逻辑 | 三条拒绝分支+异常处理，必要 |
| `sandbox/policy.py::_escalate_auto_command` | 42 | 17 | 真实逻辑 | 规则固有分支 |
| `tools/testrun.py::parse_issues` | 57 | 18 | 真实逻辑 | 多输出格式解析，固有 |

**两类高 CC 要区分**：
- **分发/装配型**（`render`/`_apply`/`build_*`/`create_app`）：CC 高源于"分支多但彼此独立"，属**偶然复杂度**，可表驱动化。
- **逻辑型**（`_stream`、`run_tools`、`_escalate_auto_command`、`parse_issues`）：CC 源于规则本身，属**本质复杂度**，难降也不该硬降。

## 4. 最大文件与嵌套

| 文件 | 行 | 函数 | 备注 |
| --- | --- | --- | --- |
| `runtime.py` | 839 | 38 | 编排中枢，复杂度天然集中 |
| `cli/app.py` | 770 | 26 | 命令 + 渲染，多为扁平注册 |
| `tui/app.py` | 651 | 34 | Textual 界面 |
| `sandbox/policy.py` | 453 | 13 | 安全规则（含大段模式常量） |
| `tools/files.py` | 375 | 8 | 文件工具 |

**深度**：报告里 `render`/`_apply` 深度 14/12 是**假象**——`elif` 在 AST 里是嵌套 `orelse`，长分发链被算成深嵌套。扣掉这一项，**真正的深嵌套几乎没有**；`_stream` 的"深 10"也主要来自 `for`+`if/elif` 的组合，仍在可读范围。

## 5. 耦合与依赖方向

包级依赖（谁 import 谁）：

```
runtime -> audit config diffing events graph memory messages sandbox tools   (中枢，合理)
graph   -> config llm messages sandbox tools
tools   -> config diffing sandbox
sandbox -> config diffing
memory  -> audit sandbox
llm     -> config
config  -> llm                    (property 里的延迟导入，注释已说明)
cli     -> ... runtime sandbox tui web   (doctor/sandbox-init 的既定例外)
tui     -> audit config events runtime
web     -> config events llm memory runtime
```

**关键结论**：
- **分层约束成立**：`tui`/`web` **没有**指向 `tools`/`sandbox` 的边——架构不变量 #5 在依赖图上得到独立印证。
- `runtime` 扇出广（10 个包）是**编排层本职**，不是坏味道。
- 无环、方向单一，没有"双向依赖"或"循环依赖"。

## 6. 总体评价

| 维度 | 评价 |
| --- | --- |
| 规模控制 | **好**：单文件最大 839 行（且是中枢），无"上帝对象" |
| 函数粒度 | **好**：中位 11 行 / CC 3 |
| 复杂度集中度 | **可接受**：9% 函数承担主要复杂度，多可解释 |
| 真·复杂点 | `runtime._stream` 是唯一值得关注的"重型"函数 |
| 耦合 | **好**：单向、分层清晰、前端无旁路 |
| 可测性 | **好**：1.17:1 测试比 + 90.5% 覆盖 |

**一句话**：整体复杂度**低且集中在可解释的位置**；真正需要克制的是 `_stream`，其余高 CC 多是"分支多但不深"的装配/分发代码。

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `runtime._stream` 的 per-node 分支拆成小 `_emit_*` 函数，用 `{node: handler}` 分派 | **最高**：把 cc39 / 214 行的中枢降到若干 cc<10 的小函数；已有事件流测试兜底 | 中（编排核心，须行为不变） |
| 2 | `cli.render` / `tui._apply` 改表驱动 `{event_type: handler}` | 高：消掉最长的 if/elif 链；两种前端结构相同，可共享形状 | 低 |
| 3 | `build_file_tools` / `build_git_tools` 按工具拆成独立 builder | 中：装配代码更短 | 低 |

三项都**不影响外部契约**，现有 805 个用例足以保证行为不变。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
