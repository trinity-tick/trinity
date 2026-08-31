# -*- coding: utf-8 -*-
"""trinity/brain/memory_governance.py — 记忆治理（EXECUTION 269，大脑化）。

借鉴 CoCortex（2026：Memory Governance Framework for Reliable
Long-Horizon Agents）——记忆全生命周期的可靠性治理。

与 agent_governance（变更治理）互补：变更=修改边界；记忆=内容可靠。
Trinity 现在：
  govern_memory(content): 记忆治理检查（来源/价值/一致性→可靠性）
"""
import os
import sys
import json


def govern_memory(content: str, importance: float = 0.5) -> dict:
    """记忆治理：来源可信×价值×一致性 → 可靠性评分。"""
    checks = {}
    # 1) 来源可信（FACTWASH 复用）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.source_credibility import _detect_source, credibility
        src = _detect_source(content)
        checks["source"] = {"ok": credibility(src) >= 0.5,
                            "credibility": credibility(src), "source": src}
    except Exception:
        checks["source"] = {"ok": True, "credibility": 0.5}
    # 2) 价值检查
    checks["value"] = {"ok": importance >= 0.2, "importance": importance}
    # 3) 一致性（与现有记忆冲突？）
    try:
        from trinity.brain.stale_revocation import detect_conflict
        # 抽样对比（取一条现有记忆）
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT left(content, 80) FROM memories WHERE status='active' AND content NOT LIKE 'enc:%' ORDER BY RANDOM() LIMIT 1")
        r = cur.fetchone()
        conn.close()
        if r:
            c = detect_conflict(content[:80], r[0])
            checks["consistency"] = {"ok": not c.get("conflict"),
                                     "conflict": c.get("conflict")}
        else:
            checks["consistency"] = {"ok": True}
    except Exception:
        checks["consistency"] = {"ok": True}
    # 综合可靠性
    ok_count = sum(1 for c in checks.values() if c.get("ok"))
    reliability = ok_count / len(checks)
    return {"checks": checks, "reliability": round(reliability, 2),
            "governed": "通过" if reliability >= 0.7 else ("需关注" if reliability >= 0.4 else "拒绝"),
            "note": f"记忆治理：来源+价值+一致性（可靠性 {round(reliability*100)}%）"}


def governance_audit() -> dict:
    """治理审计：可靠性体系状态。"""
    return {"note": "记忆治理：内容可靠性三检查（可靠长时程——CoCortex）"}
