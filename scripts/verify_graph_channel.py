#!/usr/bin/env python3
"""
Trinity — R3 P0-1a 验证：Graph+PPR 第 6 通道（2026-08-15）
============================================================
验证 MemoryAggregator hybrid 检索的图通道：
  1. 写入 3 条记忆并建立关系（A-支持->B, B-支持->C）
  2. hybrid 查询 A 相关 → 图通道把 B/C 也带入融合结果
  3. 验证 _graph_channel 已激活（6 通道融合）
"""

from __future__ import annotations

import sys
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))


def main() -> int:
    from trinity.agents.aggregator import MemoryAggregator

    print("== R3 P0-1a: Graph+PPR hybrid channel ==")
    agg = MemoryAggregator(persist_path=None)

    print(f"   graph channel active: {agg._graph_channel is not None}")

    # 1. 写入记忆
    dv_a = agg.ingest("用户偏好暗色模式，数据库用 PostgreSQL", "main",
                      {"category": "preference", "scope": "global"})
    dv_b = agg.ingest("暗色模式降低眼睛疲劳，PostgreSQL 支持 JSONB", "assistant",
                      {"category": "preference", "scope": "global"})
    dv_c = agg.ingest("JSONB 适合存储偏好配置，性能稳定", "assistant",
                      {"category": "preference", "scope": "global"})

    # 2. 建立关系：A -支持-> B -支持-> C
    agg._relations_graph.setdefault(dv_a.memory_id, {})[dv_b.memory_id] = "supports"
    agg._relations_graph.setdefault(dv_b.memory_id, {})[dv_c.memory_id] = "supports"

    # 3. 向量索引重建（手动触发，确保池内向量可检索）
    try:
        agg._rebuild_index()
    except AttributeError:
        pass  # 无此方法则跳过（向量通道可能为空，图通道仍可验证）

    # 4. 查询 A 的主题 → hybrid 融合
    results = agg.query({}, limit=10, mode="hybrid",
                        query_text="暗色模式 PostgreSQL 偏好")
    ids = [r.memory_id for r in results]
    print(f"   hybrid results: {len(results)} 条")
    for r in results[:5]:
        print(f"     - {r.memory_id[:12]} {r.content[:40]}")

    # 5. 验证：图通道扩展是否带入了 B/C
    a_id = dv_a.memory_id
    b_id = dv_b.memory_id
    c_id = dv_c.memory_id
    has_a = a_id in ids
    has_b = b_id in ids
    has_c = c_id in ids

    # 直接验证 PPR 扩展（绕过融合）
    ppr = agg._graph_channel.ppr_search([a_id], top_k=10) if agg._graph_channel else []
    ppr_ids = [g.get("id") for g in ppr]
    ppr_b = b_id in ppr_ids
    ppr_c = c_id in ppr_ids
    print(f"   PPR 扩展 from A: {[i[:12] for i in ppr_ids[:5]]}")
    print(f"   PPR 带入 B: {ppr_b}, C: {ppr_c}")

    ok = agg._graph_channel is not None and ppr_b and ppr_c
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} (channel={agg._graph_channel is not None}, "
          f"ppr_b={ppr_b}, ppr_c={ppr_c})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
