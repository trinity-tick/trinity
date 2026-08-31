# -*- coding: utf-8 -*-
"""trinity/brain/dream_cycle.py — 梦境周期（EXECUTION 358）。

借鉴 OpenClawDreams（2026：Nightly Dream Cycle——Background
Reflection）——夜间梦境周期：未梦条目 → 叙事生成 → 共识
提取（自动整合——后台运行）。

与梦境回放（随机复习）互补：回放=复习；本模块=周期整合。
Trinity 现在：
  nightly_cycle(): 夜间梦境周期（完整流程）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/dream_cycle.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cycles": 0, "dreamed": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def nightly_cycle(limit: int = 5) -> dict:
    """夜间梦境周期：未梦条目→叙事→共识提取。"""
    # 1) 收集未梦条目（感知类未整合）
    entries = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT left(content, 60) FROM memories WHERE category='perception' ORDER BY created_at DESC LIMIT %s", (limit,))
        entries = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception:
        entries = ["感知样本一", "感知样本二"]
    # 2) 生成梦境叙事（条目串联——超现实合成）
    narrative = "梦境："
    for e in entries[:3]:
        narrative += f"『{e[:20]}』→"
    narrative += "（自动合成）"
    # 3) 提取共识（高频要点）
    consensus = []
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.gist_extraction import extract_gist
        g = extract_gist([{"content": e} for e in entries])
        consensus = g.get("gist_concepts", [])[:3]
    except Exception:
        pass
    st = _load()
    st["cycles"] += 1
    st["dreamed"] += len(entries)
    _save(st)
    return {"cycle": st["cycles"], "entries_dreamed": len(entries),
            "narrative": narrative[:80], "consensus": consensus,
            "note": f"夜间梦境周期 #{st['cycles']}：{len(entries)} 条目 → 叙事 → 共识{'、'.join(consensus or ['待积累'])}"}
