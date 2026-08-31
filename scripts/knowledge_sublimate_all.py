# -*- coding: utf-8 -*-
"""knowledge_sublimate_all.py — 全量感知升华（EXECUTION 387）
处理所有未升华感知（按时间分批——每批提炼）
"""
import sys, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity")
os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")

import psycopg2
from collections import Counter

conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                        user="trinity", password="trinity", connect_timeout=10)
cur = conn.cursor()

# 所有未升华感知（全部——不只 7 天）
cur.execute("""
    SELECT memory_id, left(content, 150) FROM memories
    WHERE category='perception' AND status='active'
    ORDER BY created_at DESC
""")
rows = cur.fetchall()
print("全部感知:", len(rows))

BATCH = 20
generated = 0
batches = 0
for i in range(0, len(rows), BATCH):
    batch_rows = rows[i:i+BATCH]
    texts = [r[1] for r in batch_rows]
    words = Counter()
    for t in texts:
        t = str(t or "")
        for j in range(len(t) - 1):
            if "\u4e00" <= t[j] <= "\u9fff" and "\u4e00" <= t[j+1] <= "\u9fff":
                words[t[j:j+2]] += 1
    stop = {"系统", "状态", "我的", "进行", "可以", "需要", "这个", "相关", "我们", "已经",
            "信息", "内容", "数据", "一个", "用户"}
    topics = [w for w, cnt in words.most_common(10) if cnt >= 2 and w not in stop][:3]
    if len(topics) >= 2:
        knowledge = "提炼要点：" + "、".join(topics) + f"（来自 {len(batch_rows)} 条感知）"
        cur.execute("""
            INSERT INTO memories (memory_id, content, category, status, importance, created_at)
            VALUES (gen_random_uuid()::text, %s, 'semantic', 'active', 0.7, NOW())
        """, (knowledge,))
        generated += 1
    batches += 1
conn.commit()
conn.close()
print(f"全量升华完成：{len(rows)} 条感知 / {batches} 批 → 生成 {generated} 条语义知识")
