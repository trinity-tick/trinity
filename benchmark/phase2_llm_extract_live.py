# -*- coding: utf-8 -*-
"""Phase 2 live test: real-LLM write-time extraction via TRINITY_LLM_EXTRACT=on."""
import json, os, sys, time, tempfile
sys.path.insert(0, r"C:\Users\Administrator\trinity")

# 读凭证（不打印 key）
cred_path = os.path.expanduser("~/.dsh/.credentials.yaml")
api_key = None
with open(cred_path, "r", encoding="utf-8-sig") as f:
    for line in f:
        if line.strip().startswith("DEEPSEEK_API_KEY"):
            api_key = line.split(":", 1)[1].strip().strip('"').strip("'")
            break
assert api_key, "DEEPSEEK_API_KEY not found"

# 隔离：临时 store（不碰生产大库）
tmpdir = tempfile.mkdtemp(prefix="trinity_llmtest_")
os.environ["TRINITY_STORE"] = tmpdir
os.environ["TRINITY_LLM_API_KEY"] = api_key
os.environ["TRINITY_LLM_BASE_URL"] = "https://api.deepseek.com/v1"
os.environ["TRINITY_LLM_MODEL"] = "deepseek-chat"
os.environ["TRINITY_LLM_EXTRACT"] = "on"
os.environ["TRINITY_LLM_EXTRACT_ASYNC"] = "off"
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

from trinity import Trinity
from trinity.core.client import _find_trinity_store

mem = Trinity()
print("store:", _find_trinity_store())

test_memories = [
    ("Alice 是深蓝科技的后端工程师，负责支付系统，2026 年 6 月晋升为技术总监", ["person", "project"]),
    ("公司决定把数据库从 MySQL 迁移到 PostgreSQL，迁移负责人是 Bob", ["decision", "project"]),
    ("Charlie 喜欢极简风格的 UI 设计，是产品团队的交互设计师", ["person", "concept"]),
    ("支付系统 QPS 峰值达到 12000，P99 延迟 85ms，部署在 AWS us-east-1", ["project", "concept"]),
]
t0 = time.time()
for i, (content, tags) in enumerate(test_memories):
    r = mem.ingest(content, tags=tags, category="llmtest")
    print(f"ingest[{i}] id={r.get('memory_id')} entities={r.get('extracted_entities')} postprocess={r.get('postprocess')}")
dt = time.time() - t0
print(f"ingest+extract total: {dt:.1f}s  avg {dt/len(test_memories)*1000:.0f}ms/mem")

# 验证实体/关系已落库
import sqlite3
conn = sqlite3.connect(os.path.join(tmpdir, "trinity_store.db"))
conn.row_factory = sqlite3.Row
ents = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
rels = conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
memories = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
print(f"DB check: memories={memories} entities={ents} relations={rels}")
print("sample entities:")
for row in conn.execute("SELECT name, etype FROM entities LIMIT 12"):
    print("  ", row["name"], "|", row["etype"])
print("sample relations:")
for row in conn.execute("SELECT * FROM relations LIMIT 8"):
    print("  ", dict(row) if isinstance(row, sqlite3.Row) else row)
conn.close()
