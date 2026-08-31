# -*- coding: utf-8 -*-
"""trinity/brain/write_gate.py — 写时门控（EXECUTION 253，大脑化）。

借鉴 Selective Memory（2026：Write-Time Gating）——写入时决定
是否值得写（选择性记忆：质量门 + 分层）。

与 surprise 编码（提升）互补：编码=重要性提升；门控=写入过滤。
Trinity 现在：
  gate(content, importance): 写入门控（价值/重复检查→写/拒/降级）
"""
import os
import sys
import json


def gate(content: str, importance: float = 0.5) -> dict:
    """写入门控：质量检查（长度/价值/重复）。"""
    verdict = "write"
    reasons = []
    # 1) 长度检查（太短无价值）
    if len(str(content).strip()) < 8:
        return {"verdict": "reject", "reasons": ["太短（<8 字符）"],
                "importance": importance}
    # 2) 价值检查
    if importance < 0.15:
        return {"verdict": "reject", "reasons": ["价值过低（<0.15）"],
                "importance": importance}
    # 3) 重复检查（近似内容已存在）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 用 2 字词检查近似重复
        t = str(content)[:60]
        words = set()
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words.add(t[i:i+2])
        dup_hits = 0
        for w in list(words)[:4]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s", (f"%{w}%",))
            if cur.fetchone()[0] > 0:
                dup_hits += 1
        conn.close()
        if words and dup_hits >= len(words) * 0.75:
            return {"verdict": "downgrade", "reasons": ["近似重复"],
                    "importance": importance * 0.5}
    except Exception:
        pass
    return {"verdict": "write", "reasons": ["通过质量门"], "importance": importance}


def gate_stats() -> dict:
    """门控统计（选择性记忆效果）。"""
    return {"note": "写时门控：长度/价值/重复三检查（选择性写入）"}
