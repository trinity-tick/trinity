"""GuardianChainV50 / RetrievalSystemV47 转口（T6 能力面收口，2026-10-06）。

本模块原名 "Trinity Second Brain — GuardianChainV50, RetrievalSystemV47"，
并**自己定义**了这两个类 —— 它们与运行副本
（`engine_guardian_retrieval.py`，经 `engine_core` → `engine` 门面 →
`SecondBrainV636.guardian_chain` / `.retrieval` 使用）**同名不同体**：

============================  ==========================  ==============================
项                            `engine_guardian_retrieval`  本文件（旧影子副本）
============================  ==========================  ==============================
`RetrievalSystemV47.channels` 键 `channel_1..channel_47`  键 `ch01..ch47`（+ 伪 `latency_ms`）
`total`                       有（= len(channels)）        **不存在**
`CONTRIBUTES` / `DATA_SOURCE` 显式登记 `False` / `None`     同样登记（值相同）
`contributing_count()`        有                            **不存在**
`contributes()`               有                            **不存在**
`search()`                    有（返回 `[]`，契约）           **不存在**
`validate()`                  可失败合取式（T6 前一轮修）     恒真式 `len(...) == 47`
============================  ==========================  ==============================

（上表由 `git show HEAD:` 取回改前源码、`exec()` 出影子类后**逐个 hasattr 实测**得到，
不是读代码推断；见 evidence/loader_ghost_enum.json 与 shadow_copy_diff.json。）

具体后果（不是"潜在风险"，是已发生的）：

* `loader.py` 从本模块导入 `RetrievalSystemV47` 后调用 `contributing_count()`
  与 `.total` ⇒ 两处 `AttributeError`（连同 `guardian.py` 那处，共 3 个幽灵成员）；
* `scripts/channel_census.py:85` 从本模块读数并把它标为「引擎注册面」的**来源**，
  于是"引擎 47 通道"这个数字实际上读的是**另一个对象**（另一套键名、
  另一套 `validate()`）—— 声明与现实不对应。

## 处置（T6）

只保留**一处定义**（`engine_guardian_retrieval`），本模块改为**显式转口**；
`discover_latest_version` 的 re-export 保持不变（那是 2026-08-15 既有的去重结果）。

**已知的、写域外的残留**：`scripts/channel_census.py` 的两处文案写着
`键 ch01..ch47`（docstring `:9` / `SENSES[0]["meaning"] :64`）。转口之后该脚本读到的是
运行副本，键名变成 `channel_1..channel_47`，**计数仍为 47**（所以它的 headline 数字不变，
且从此与引擎读同一对象），但上述两处文案需要 scripts 写域的人同步。详见
`D:\\DSH官网\\trinity-optimize-20261006\\CAPABILITY-HYGIENE.md` 的残留项 R1。

判据见 `tests/unit/test_capability_hygiene_20261006.py`。
"""
# status: frozen (2026-09 EXECUTION 163)；2026-10-06 T6：由「重复实现」改为「显式转口」

from __future__ import annotations

from trinity.modules.second_brain.engine_guardian_retrieval import (  # noqa: F401
    GuardianChainV50,
    RetrievalSystemV47,
)

# 2026-08-15 (P2 dedup): 统一实现到 engine_core（含完整版本链），此处 re-export，
# 消除多处双实现。T6 保留该转口不变。
from trinity.modules.second_brain.engine_core import (  # noqa: F401,E402
    discover_latest_version,
)

__all__ = ["GuardianChainV50", "RetrievalSystemV47", "discover_latest_version"]
