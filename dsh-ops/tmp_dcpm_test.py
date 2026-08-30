# -*- coding: utf-8 -*-
import sys, time
sys.path.insert(0, r"D:\trinity-code")
os_env = __import__("os").environ
os_env.setdefault("PGHOST", "127.0.0.1"); os_env.setdefault("PGPORT", "5432")
os_env.setdefault("PGDATABASE", "trinity"); os_env.setdefault("PGUSER", "trinity")
os_env.setdefault("PGPASSWORD", "trinity")
os_env.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")
from trinity import Trinity
m = Trinity(adapter="postgresql")
for q in ["用户偏好 咖啡", "PostgreSQL 主存储", "完全不存在的查询xyz123"]:
    r = m.search_hybrid(q, top_k=5)
    meta = r.get("metacognition", {})
    print(q, "=> hits:", len(r.get("results", [])), "| conf:", meta.get("confidence"), "| level:", meta.get("level"))
# check System1 beliefs recorded
eng = m.dcpm
if eng:
    n = len(eng["system1"]._beliefs)
    print("System1 beliefs recorded:", n)
    for bid, node in list(eng["system1"]._beliefs.items())[:2]:
        print("  belief:", node.subject, "->", node.predicate, "->", node.object[:40])
