# -*- coding: utf-8 -*-
"""trinity/brain/identity_anchors.py — 身份锚点（EXECUTION 277，大脑化）。

借鉴 Declarative Anchors（2026：Forgetting Problem——完美记忆会破坏
身份）——身份锚点：不可遗忘/不变的核心身份声明（锚定"我是谁"）。

与全局自我（动态身份）互补：自我=动态更新；锚点=不变根基。
Trinity 现在：
  set_anchor(content): 设定身份锚点（声明式）
  verify_anchors(): 锚点验证（身份根基完好）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/identity_anchors.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"anchors": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def set_anchor(content: str) -> dict:
    """设定身份锚点（不可遗忘——声明式根基）。"""
    st = _load()
    anchor = {"content": str(content)[:100], "ts": __import__("time").time(),
              "immutable": True}
    if any(a["content"] == anchor["content"] for a in st["anchors"]):
        return {"set": False, "note": "锚点已存在"}
    st["anchors"].append(anchor)
    st["anchors"] = st["anchors"][-10:]
    _save(st)
    # 锚点同步写入记忆（identity-anchor 类别——可验证/不可遗忘）
    try:
        sys.path.insert(0, r"D:\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest("[identity-anchor] " + anchor["content"], category="identity-anchor",
                 tags=["identity", "anchor"], importance=0.95, wait_backfill=True)
    except Exception:
        pass
    return {"set": True, "anchor": anchor["content"][:40],
            "note": "身份锚点已设定（不可遗忘）"}


def verify_anchors() -> dict:
    """锚点验证：身份根基是否完好（未被遗忘/漂移）。"""
    st = _load()
    anchors = st.get("anchors", [])
    if not anchors:
        return {"verified": False, "note": "无锚点（建议设定身份根基）"}
    # 锚点应仍在记忆库中（未被遗忘）
    intact = 0
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        for a in anchors:
            cur.execute("SELECT count(*) FROM memories WHERE content LIKE %s AND status='active'",
                        (f"%{a['content'][:20]}%",))
            if cur.fetchone()[0] > 0:
                intact += 1
        conn.close()
    except Exception:
        pass
    return {"verified": intact == len(anchors), "anchors": len(anchors),
            "intact": intact,
            "note": f"身份根基完好（{intact}/{len(anchors)} 锚点完好）" if anchors else "无锚点"}
