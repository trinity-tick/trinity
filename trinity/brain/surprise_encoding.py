# -*- coding: utf-8 -*-
"""trinity/brain/surprise_encoding.py — 预测误差编码（EXECUTION 237，大脑化）。

借鉴 surreal-memory prediction_error.py（surprise signal boosts memory
priority）——大脑记忆真实机制：意外/新奇事件记得更牢（多巴胺增强）。

Trinity 现在：
  encode_with_surprise(content, prior_belief): 新内容 vs 已有信念 →
    预测误差 → 记忆重要性提升（surprise 编码强化）
  surprise_boost(content): 快捷接口（内容新颖度 → 提升值）

与预测-行动环（调查 surprise）互补：那里=行动；这里=编码强化。
"""
import os
import sys
import json


def _prior_similarity(content: str) -> float:
    """内容与已有记忆的相似度（可预测性——0 全新 1 完全已知）。
    用 ILIKE 覆盖近似（词片段命中数）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2, re
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 2 字滑动窗口词
        _txt = str(content)[:60]
        words = set()
        for i in range(len(_txt) - 1):
            if "\u4e00" <= _txt[i] <= "\u9fff" and "\u4e00" <= _txt[i+1] <= "\u9fff":
                words.add(_txt[i:i+2])
        hits = 0
        for w in list(words)[:6]:
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s AND status='active'", (f"%{w}%",))
            if cur.fetchone()[0] > 0:
                hits += 1
        conn.close()
        if not words:
            return 0.5
        return min(1.0, hits / max(len(words), 1))
    except Exception:
        return 0.3


def encode_with_surprise(content: str, base_importance: float = 0.5,
                         write: bool = True) -> dict:
    """预测误差编码：内容新颖度 → surprise → 重要性提升。"""
    prior = _prior_similarity(content)
    surprise = 1.0 - prior  # 相似度低 = 意外大
    boost = surprise * 0.25  # 意外记忆 +0.25 最大
    importance = min(base_importance + boost, 0.95)
    if write:
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity import Trinity
            m = Trinity(adapter="postgresql")
            m.ingest(content[:280], category="surprise-encoded",
                     tags=["surprise", "novel"], importance=importance,
                     wait_backfill=True)
        except Exception as e:
            return {"encoded": False, "error": str(e)[:60]}
    return {"encoded": True, "prior_similarity": round(prior, 2),
            "surprise": round(surprise, 2), "boost": round(boost, 2),
            "importance": round(importance, 2),
            "note": "意外程度高 → 记忆更重要（surprise 编码）"}


def surprise_boost(content: str) -> dict:
    """快捷评估：内容的新颖度与建议提升。"""
    prior = _prior_similarity(content)
    return {"novelty": round(1.0 - prior, 2),
            "suggested_boost": round((1.0 - prior) * 0.25, 2),
            "interpretation": "意外" if prior < 0.5 else "熟悉"}
