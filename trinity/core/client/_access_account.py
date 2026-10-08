# -*- coding: utf-8 -*-
"""检索出口的**记账**（`access_count`）—— 唯一一处实现（2026-10-06 t18）。

完整口径、取舍、非 API 调用方清点与现场读数见 `ACCESS-COUNT-SINGLE-COUNT.md` §12；此处只留不变量：

* **I1 至多 +1**：一次检索对同一行最多累加 1 次（t17）。
* **I2 恰好 +1**：**被返回**的每一行都恰好 +1（t18；不是 0，也不是 2）。

**为什么记账在出口**（本模块，由 `_hybrid_search.search_hybrid` 的两条出口各调一次），
而不是通道层或路由层：

* 通道层（`search_memories`）已改 `touch=False` —— 通道结果会被融合**截断**（记了没返回的行）、
  同一行会被**多通道**命中（叠加成 2 次）、且仅向量/语料通道命中的行不吃适配器检索
  （漏记 = t17 的 R-1 回退）；
* 路由层只是 `search_hybrid` 的调用方**之一**（实测还有 `/memory/search`、`/memory/recall`、
  graphql、`_routers_brain`、`_routers_explain`、`engine_worker`、`trinity/brain/**` 十余个模块），
  只在那儿记账会让上述入口**永久不再记账**；
* 出口是**所有调用方都必经**、且知道"最终返回了什么"的那一层。

**分块**：`access_touch.touch_results` 单次只处理 **5** 条（`if len(clean) >= 5: break`），
而实测生产返回集 **54.6%（437/800）> 5 条** ⇒ 不分块则第 6 条起**一条都不记**。
"""
from __future__ import annotations

from typing import Any

from trinity.brain.access_touch import touch_results

#: `touch_results` 的单次上限是 5，分块不得大于它（判据把两个数字绑在一起）。
_ACCOUNT_CHUNK = 5


def account_returned_hits(res: Any, adapter: Any = None, background: bool = True,
                          enabled: bool = True) -> int:
    """对本次**返回的行集合**记账（每行恰好 +1），返回已入队的条数。

    入参三种形态都支持（t28：R-8 的功能判据当场抓到我只支持第一种就上线了）：
      · 结果字典 `{"results": [...]}` —— `search_hybrid` 的出口传的就是它；
      · **行字典列表** `[{...}, ...]` —— `_search_with_vector` 的出口传的是它；
      · id 列表 `["mem_…", ...]`。
    失败一律 fail-open：**记账绝不影响检索**。

    `enabled=False`（t32）：**调用方声明"这次检索本身不该记账"**（内部/派生/自碰检索）——
    目前两类：① `search_hybrid(..., account=False)`（写入后自检 / 健康自检 / 派生候选池）；
    ② 预热路径（`_deps.py` 的 `_vector_search(..., account=False)`，见 t30）。
    """
    if not enabled:
        return 0
    if isinstance(res, dict):
        res = res.get("results") or []
    ids = []
    for r in (res or []):
        if isinstance(r, dict):
            mid = r.get("memory_id")
            if mid:
                ids.append(str(mid))
        elif r:
            ids.append(str(r))
    n = 0
    for i in range(0, len(ids), _ACCOUNT_CHUNK):
        try:
            n += int(touch_results(ids[i:i + _ACCOUNT_CHUNK], adapter=adapter,
                                   background=background) or 0)
        except Exception:  # noqa: BLE001 — 记账绝不影响检索
            continue
    return n
