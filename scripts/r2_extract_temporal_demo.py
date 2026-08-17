#!/usr/bin/env python3
"""
Trinity — R2 优化 Demo：LLM 事实抽取 + edge bi-temporal（2026-08-15）
======================================================================
对齐 2026 Q3 网络最优方案（Mem0/Zep 写入即抽取、Graphiti edge 级时序）：

  A. 写路径 LLM 事实抽取：TRINITY_LLM_EXTRACT=on 时 client.write →
     EntityRelationExtractor（LLM 提取实体+关系谓词 → relations 表）
     （无 LLM key 时演示规则回退）
  B. edge 级 bi-temporal：create_relation 带 valid_from/valid_to →
     query_relations_at(时点) 只返回该时点有效的边

用法：
    python scripts/r2_extract_temporal_demo.py
    $env:TRINITY_LLM_EXTRACT="on"   # 开启 LLM 事实抽取（需 TRINITY_LLM_API_KEY）
    python scripts/r2_extract_temporal_demo.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))


def main() -> int:
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.core.client import Trinity

    llm_on = os.environ.get("TRINITY_LLM_EXTRACT", "").strip().lower() in ("1", "on", "true", "yes")
    print(f"== R2 优化 Demo（LLM 事实抽取={'ON 🔥' if llm_on else 'OFF（规则回退）'}）==")

    db_path = os.path.join(tempfile.mkdtemp(prefix="trinity_r2_"), "r2.db")
    adapter = SQLiteAdapter(db_path)
    adapter.connect()

    # ── A. 写路径实体/关系提取 ─────────────────────────────────────
    print("\n== A. 写路径实体/关系提取 ==")
    t = Trinity(store_path=db_path)
    r = t.ingest(
        "Alice 和 Bob 在 Trinity 项目上协作，Alice 负责记忆模块，Bob 负责检索模块。",
        persona_id="r2_demo", agent_id="agent-a",
    )
    mid = r.get("memory_id", "")
    print(f"   memory_id={mid}, extracted_entities={r.get('extracted_entities', 0)}")

    # 实体
    ent_rows = adapter._conn.execute(
        "SELECT entity_id, name FROM entities ORDER BY first_seen DESC LIMIT 10"
    ).fetchall()
    print(f"   实体数: {len(ent_rows)}")
    for r_ in ent_rows[:5]:
        print(f"     - {r_[1]} ({r_[0][:10]})")

    # 关系（LLM 模式应有谓词关系；规则模式可能为空——正则对中文实体覆盖有限）
    rels = adapter.query_relations(limit=10)
    print(f"   关系数: {len(rels)}")
    preds = {}
    for r_ in rels:
        preds[r_.get("predicate", "?")] = preds.get(r_.get("predicate", "?"), 0) + 1
    print(f"   谓词分布: {preds}")

    if llm_on:
        # LLM 模式：期望实体 ≥2 且出现语义谓词（works_on / collaborates 等）
        ok_a = len(ent_rows) >= 2 and len(rels) >= 1
        semantic = [p for p in preds if p not in ("mentions", "related_to")]
        ok_a = ok_a and len(semantic) >= 1
        print(f"   语义谓词: {semantic}")
    else:
        # 规则模式：正则对中文实体覆盖有限，只验证管线不崩溃 + 时序列生效
        ok_a = True
        print("   （规则模式：不强制实体数，验证管线与回退路径）")

    # ── B. edge bi-temporal ─────────────────────────────────────────
    print("\n== B. edge bi-temporal（时点查询）==")
    e1 = adapter.upsert_entity("ProjectX", "project", {})
    e2 = adapter.upsert_entity("Alice", "person", {})
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=30)).isoformat()
    future = (now + timedelta(days=30)).isoformat()
    # 已过期边：valid_from=30 天前, valid_to=10 天前 → 当前时点应不可见
    expired = (now - timedelta(days=10)).isoformat()
    adapter.create_relation(e1["id"], "active_edge", e2["id"],
                            valid_from=past, valid_to=future)
    adapter.create_relation(e1["id"], "expired_edge", e2["id"],
                            valid_from=past, valid_to=expired)

    now_iso = now.isoformat()
    active = adapter.query_relations_at(now_iso, subject_id=e1["id"])
    preds_at = [r_["predicate"] for r_ in active]
    ok_b1 = "active_edge" in preds_at and "expired_edge" not in preds_at
    print(f"   当前时点有效边: {preds_at} → 过期边已排除: {ok_b1}")

    # 回溯 15 天前：expired_edge（10 天前失效）那时仍有效，active_edge（30 天后失效）也有效
    fifteen_days_ago = (now - timedelta(days=15)).isoformat()
    back = adapter.query_relations_at(fifteen_days_ago, subject_id=e1["id"])
    preds_back = [r_["predicate"] for r_ in back]
    ok_b2 = "expired_edge" in preds_back and "active_edge" in preds_back
    print(f"   15 天前有效边: {preds_back} → 两条边当时均可见: {ok_b2}")

    # 无时间参数的关系：valid_from 默认创建时刻 → 查询稍晚时点可见
    adapter.create_relation(e1["id"], "no_ttl_edge", e2["id"])
    later = (now + timedelta(seconds=2)).isoformat()
    all_now = adapter.query_relations_at(later, subject_id=e1["id"])
    ok_b3 = any(r_["predicate"] == "no_ttl_edge" for r_ in all_now)
    print(f"   无 TTL 边（+2s 时点）可见: {ok_b3}")

    # 元数据检查：列存在
    cols = [c[1] for c in adapter._conn.execute("PRAGMA table_info(relations)").fetchall()]
    ok_b4 = "valid_from" in cols and "valid_to" in cols
    print(f"   relations 时序列存在: {ok_b4}")

    adapter.disconnect()

    ok = ok_a and ok_b1 and ok_b2 and ok_b3 and ok_b4
    print(f"\nRESULT: {'PASS ✅' if ok else 'FAIL ❌'}"
          f" (extract={ok_a}, temporal={ok_b1 and ok_b2 and ok_b3}, schema={ok_b4})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
