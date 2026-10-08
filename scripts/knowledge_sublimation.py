# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _pg_std import pg_connect  # noqa

"""knowledge_sublimation.py — 知识升华（EXECUTION 384）。

从感知输入批量提炼生成知识（感知 → 要点 → 语义知识写入）。
用法：python knowledge_sublimation.py [limit] [batch]
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", pg_creds()["dbname"])
os.environ.setdefault("PGUSER", pg_creds()["user"])
os.environ.setdefault("PGPASSWORD", pg_creds()["password"])

import psycopg2
from collections import Counter


def sublimate(limit=200, batch=10):
    """从感知输入批量提炼知识。"""
    conn = pg_connect(connect_timeout=10)
    cur = conn.cursor()
    # 1) 取近期未升华的感知输入
    cur.execute("""
        SELECT memory_id, content FROM memories
        WHERE category='perception' AND status='active'
          AND created_at::timestamp > NOW() - interval '7 days'
        ORDER BY created_at DESC LIMIT %s
    """, (limit,))
    # 2026-09-20（§926）：感知行落库即密文 ⇒ 先解密再截断（否则把 base64 喂给提炼逻辑）
    from trinity.security.crypto import decrypt_content as _dec
    rows = [(r[0], str(_dec(r[1]))[:150]) for r in cur.fetchall()]
    print("感知样本:", len(rows))
    # 2) 批量提炼（每批取要点）
    generated = 0
    for i in range(0, len(rows), batch):
        batch_rows = rows[i:i+batch]
        texts = [r[1] for r in batch_rows]
        # 2 字词统计（跨样本——主题提炼）
        words = Counter()
        for t in texts:
            t = str(t or "")
            for j in range(len(t) - 1):
                if "\u4e00" <= t[j] <= "\u9fff" and "\u4e00" <= t[j+1] <= "\u9fff":
                    words[t[j:j+2]] += 1
        stop = {"系统", "状态", "我的", "进行", "可以", "需要", "这个", "相关", "我们", "已经"}
        topics = [w for w, cnt in words.most_common(8) if cnt >= 2 and w not in stop][:3]
        if len(topics) >= 2:
            # 生成知识（语义提炼）
            knowledge = "提炼要点：" + "、".join(topics) + f"（来自 {len(batch_rows)} 条感知）"
            cur.execute("""
                INSERT INTO memories (memory_id, content, category, status, importance, created_at, agent_id, version)
                VALUES (gen_random_uuid()::text, %s, 'semantic', 'active', 0.7, NOW(), 'default', 1)
            """, (knowledge,))
            generated += 1
            print(f"  生成: {topics}")
    conn.commit()
    conn.close()
    return generated


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    batch = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    n = sublimate(limit, batch)
    print(f"知识升华完成：生成 {n} 条语义知识")


