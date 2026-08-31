# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, r"D:\trinity-code")
sys.path.insert(0, r"D:\trinity-code\scripts")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
import psycopg2
c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
cur = c.cursor()
# 清残留
cur.execute("DELETE FROM memories WHERE memory_id LIKE 'dream-t-%'")
cur.execute("UPDATE memories SET status='active' WHERE content LIKE 'enc:%'")
c.commit()
# 插明文
cur.execute("INSERT INTO memories (memory_id, content, category, status, importance) VALUES (%s,%s,%s,%s,%s)",
            ("dream-t-001", "[dream-test] 量子计算的最新进展", "tech-news", "active", 0.6))
cur.execute("INSERT INTO memories (memory_id, content, category, status, importance) VALUES (%s,%s,%s,%s,%s)",
            ("dream-t-002", "[dream-test] 咖啡种植的季节规律", "life-notes", "active", 0.6))
c.commit()
cur.execute("UPDATE memories SET status='inactive' WHERE content LIKE 'enc:%'")
c.commit()
# 组合
cur.execute("SELECT category, left(content,60), memory_id FROM memories WHERE status='active' AND category NOT IN ('perception','self-identity','dcpm-core') ORDER BY RANDOM() LIMIT 6")
rows = cur.fetchall()
combos = []
for i in range(0, len(rows)-1, 2):
    if i+1 >= len(rows): break
    c1, t1, id1 = rows[i]
    c2, t2, id2 = rows[i+1]
    if c1 != c2:
        combos.append(f"『{t1}』({c1}) × 『{t2}』({c2})")
print("组合数:", len(combos))
for x in combos[:3]:
    print(" -", x[:70])
cur.execute("UPDATE memories SET status='active' WHERE content LIKE 'enc:%'")
cur.execute("DELETE FROM memories WHERE memory_id LIKE 'dream-t-%'")
c.commit(); c.close()
print("清理完成")
