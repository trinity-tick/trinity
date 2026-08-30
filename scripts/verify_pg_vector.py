# -*- coding: utf-8 -*-
"""PG 融合验证：pgvector 直查通道端到端。"""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")

from trinity.adapters.postgresql import PostgreSQLAdapter
import requests

a = PostgreSQLAdapter(auto_connect=True)
a.connect()
try:
    n = a.count_embeddings()
    total = a.get_memory_stats().get("total_memories") if hasattr(a, "get_memory_stats") else "?"
    print(f"embeddings={n} total={total}")
    # vector search roundtrip via ollama bge-m3
    for q in ["Trinity 记忆系统融合测试", "PostgreSQL 主存储切换", "Windows 服务注册"]:
        v = requests.post("http://127.0.0.1:11434/api/embed", json={"model": "bge-m3", "input": [q]}, timeout=120).json()["embeddings"][0]
        t0 = time.time()
        r = a.vector_search(list(v), top_k=3)
        dt = time.time() - t0
        print(f"query={q[:20]!r} hits={len(r)} in {dt*1000:.0f}ms top1={(r[0]['content'][:40] if r else 'NONE')}")
    print("VERIFY OK")
finally:
    a.disconnect()
