"""单元测试包。

这个 `__init__.py` **不是可有可无的**：它让 pytest 用 `unit.test_x` 而不是裸
`test_x` 作为模块名。没有它，`tests/unit/test_snapshots.py` 与
`tests/integration/test_snapshots.py` 会撞成同一个模块名，pytest 报
`import file mismatch` —— 而后果不是"跑红"，是**其中一个文件整个不被收集**：
`tests/unit/test_snapshots.py` 的用例曾长期一条都没执行，全量跑分看上去仍然正常。
"""
