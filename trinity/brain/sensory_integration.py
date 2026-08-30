# -*- coding: utf-8 -*-
"""trinity/brain/sensory_integration.py — 多通道感知整合（EXECUTION 188，大脑化）。

感觉整合（multisensory integration）：大脑将多感官输入融合为
统一"统觉"（如看到+听到+闻到同一事件）。Trinity 的 4 感官
（日志/文件/视觉/网络）目前各自独立感知——本模块：
  1. 聚合各通道最近感知（统一视场）
  2. 关联检测：同一时间窗口的多通道信号 → 关联事件（统觉）
  3. 整合感知写入记忆（sensory-integration 类别）

通道权重（整合显著性）：网络 0.6 / 日志 0.7 / 文件 0.6 / 视觉 0.5
"""
import os
import sys
import json
import time
from datetime import datetime


def _recent_signals(conn, hours: float = 24) -> dict:
    """各通道最近感知信号（聚合）。"""
    channels = {}
    try:
        cur = conn.cursor()
        # 网络感知（web）
        cur.execute("""
            SELECT count(*), max(created_at) FROM memories
            WHERE category='perception' AND content LIKE '%%[web%%'
              AND created_at::timestamp > NOW() - make_interval(hours => %s)
        """, (hours,))
        r = cur.fetchone()
        channels["web"] = {"count": r[0] or 0, "last": str(r[1] or "")[:19]}
        # 日志感知（log）
        cur.execute("""
            SELECT count(*), max(created_at) FROM memories
            WHERE category='perception' AND content LIKE '%%[log%%'
              AND created_at::timestamp > NOW() - make_interval(hours => %s)
        """, (hours,))
        r = cur.fetchone()
        channels["log"] = {"count": r[0] or 0, "last": str(r[1] or "")[:19]}
        # 文件感知（filesystem）
        cur.execute("""
            SELECT count(*), max(created_at) FROM memories
            WHERE category='perception' AND content LIKE '%%[filesystem%%'
              AND created_at::timestamp > NOW() - make_interval(hours => %s)
        """, (hours,))
        r = cur.fetchone()
        channels["filesystem"] = {"count": r[0] or 0, "last": str(r[1] or "")[:19]}
    except Exception:
        pass
    return channels


def integrate_senses(hours: float = 24) -> dict:
    """聚合各通道 → 统一感知状态 + 关联检测。"""
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    try:
        ch = _recent_signals(conn, hours)
        # 关联检测：24h 内多通道同时活跃 → 关联事件
        active = [k for k, v in ch.items() if v["count"] > 0]
        correlations = []
        if len(active) >= 2:
            correlations.append({
                "channels": active,
                "note": "多通道同步感知（统觉）",
                "ts": datetime.now().isoformat(),
            })
        # 整合显著性（加权）
        weights = {"web": 0.6, "log": 0.7, "filesystem": 0.6}
        salience = sum(w * ch.get(k, {}).get("count", 0) for k, w in weights.items())
        return {"channels": ch, "active_channels": active,
                "correlations": correlations,
                "integrated_salience": round(salience, 2),
                "ts": datetime.now().isoformat()}
    finally:
        conn.close()


def integrate_to_memory(hours: float = 24) -> bool:
    """整合感知写入记忆（统觉）。"""
    try:
        r = integrate_senses(hours)
        active = r["active_channels"]
        text = ("[sensory-integration] 我的感知整合："
                + "、".join(f"{k} {ch['count']}信号" for k, ch in r["channels"].items() if ch["count"])
                + f"；活跃通道 {len(active)} 个"
                + ("；多通道同步感知（统觉）" if r["correlations"] else "")
                + f"；整合显著性 {r['integrated_salience']}")
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest(text[:300], category="sensory-integration",
                 tags=["senses", "integration"], importance=0.7, wait_backfill=True)
        return True
    except Exception:
        return False
