# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, r"D:\trinity-code")
sys.path.insert(0, r"D:\trinity-code\scripts")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
import psycopg2
c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
cur = c.cursor()
cur.execute("INSERT INTO memories (memory_id, content, category, status, importance) VALUES (%s, %s, %s, %s, %s)",
            ("dream-t-001", "[dream-test] 量子计算的最新进展", "tech-news", "active", 0.6))
cur.execute("INSERT INTO memories (memory_id, content, category, status, importance) VALUES (%s, %s, %s, %s, %s)",
            ("dream-t-002", "[dream-test] 咖啡种植的季节规律", "life-notes", "active", 0.6))
c.commit()
cur.execute("UPDATE memories SET status='inactive' WHERE content LIKE 'enc:%'")
c.commit()
from dream_replay import dream_recombine
r = dream_recombine(4)
print("组合:", r.get("combos"))
for d in r.get("dreams", [])[:3]:
    print(" -", d[:70])
cur.execute("UPDATE memories SET status='active' WHERE content LIKE 'enc:%'")
cur.execute("DELETE FROM memories WHERE memory_id IN ('dream-t-001','dream-t-002')")
c.commit(); c.close()
print("清理完成")
