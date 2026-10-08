"""Guardian Chain 转口（T6 能力面收口，2026-10-06）。

## 历史（"同名类 3 份副本"中的第 2 份）

本文件曾**自己定义**一份 `GuardianChainV50`：

    self.shields = {f"L{i}": f"Shield_L{i}" for i in range(1, 51)}   # 名字是合成的
    def validate(self): return self.total == 50                      # 恒真式

它与运行副本 `engine_guardian_retrieval.GuardianChainV50` **同名不同体**：
本副本没有 `enforcing_count()`（也无 `DECLARED_NOOP` / `get_new_shields` 的真名册）。

而 `loader.py::SecondBrainLoader.diagnostics()` 正是从**本模块**导入并调用
`enforcing_count()` ⇒ 该诊断入口自诞生起就抛
`AttributeError: 'GuardianChainV50' object has no attribute 'enforcing_count'`
（"loader 调用了不存在的成员"这一项的**具体发生率**：100%，不是潜在风险）。

## 处置（T6）

只保留**一处定义** —— `engine_guardian_retrieval.GuardianChainV50`，
即 `engine_core` → `engine` 门面 → `SecondBrainV636.guardian_chain` 实际使用的那个 —— 
本模块改为**显式转口**（import alias），不再持有第二份实现。

判据（可失败，带负向实测）：
`tests/unit/test_capability_hygiene_20261006.py`

  * 全仓 `class GuardianChainV50` 定义点**恰好 1 处**；
  * `guardian.GuardianChainV50 is engine_guardian_retrieval.GuardianChainV50 is engine.GuardianChainV50`
    （三个导入点同一对象 ⇒ 不再是"同名不同体"）；
  * `SecondBrainLoader(lazy=True).diagnostics()` 可运行且数值与引擎口径一致。

运行时行为证据（MRO / 构造 / 活体调用路径）见
`D:\\DSH官网\\trinity-optimize-20261006\\evidence\\alias_equivalence.json`。
"""
# status: frozen (2026-09 EXECUTION 163)；2026-10-06 T6：由「重复实现」改为「显式转口」

from __future__ import annotations

from trinity.modules.second_brain.engine_guardian_retrieval import (  # noqa: F401
    GuardianChainV50,
)

__all__ = ["GuardianChainV50"]
