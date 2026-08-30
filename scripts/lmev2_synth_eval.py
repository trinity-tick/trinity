# -*- coding: utf-8 -*-
"""lmev2_synth 评测 v6：正确提取 steps[].observation（答案线索）"""
import sys, os, json, re, psycopg2
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
from trinity.core.client._helpers import _get_embedding_engine

conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
conn.autocommit = True
cur = conn.cursor()
eng = _get_embedding_engine()

DATA = r"C:\Users\Administrator\.trinity\bench-official\lmev2_synth"
# 清理旧测试数据
cur.execute("DELETE FROM memories WHERE category='lmev2'")
print("cleaned old lmev2")

# 1) 正确提取 steps
trajs = [json.loads(l) for l in open(os.path.join(DATA, "trajectories.jsonl"), encoding="utf-8") if l.strip()]
for t in trajs:
    goal = t.get("goal", "")
    if goal:
        cur.execute("INSERT INTO memories (memory_id, session_id, persona_id, tenant_id, agent_id, content, importance, importance_score, status, category, modality, content_hash, created_at, updated_at) SELECT uuid_generate_v4(), 'lmev2', 'default', 'default', 'lmev2-eval-iso', %s, 0.6, 0.6, 'active', 'lmev2', 'text', encode(sha256(%s::bytea),'hex'), NOW(), NOW()", ("[goal] " + goal, "[goal] " + goal))
    for step in (t.get("steps") or []):
        obs = (step.get("observation") or "") if isinstance(step, dict) else ""
        act = (step.get("action") or "") if isinstance(step, dict) else ""
        thought = (step.get("thought") or "") if isinstance(step, dict) else ""
        for txt in (obs, act, thought):
            if txt and str(txt).strip():
                cur.execute("INSERT INTO memories (memory_id, session_id, persona_id, tenant_id, agent_id, content, importance, importance_score, status, category, modality, content_hash, created_at, updated_at) SELECT uuid_generate_v4(), 'lmev2', 'default', 'default', 'lmev2-eval-iso', %s, 0.6, 0.6, 'active', 'lmev2', 'text', encode(sha256(%s::bytea),'hex'), NOW(), NOW()", (str(txt)[:300], str(txt)[:300]))
print("steps ingested")

# 2) 回填向量
cur.execute("SELECT memory_id, content FROM memories WHERE category='lmev2' AND embedding IS NULL")
rows = cur.fetchall()
for mid, content in rows:
    v = eng.embed(str(content))
    cur.execute("UPDATE memories SET embedding = %s WHERE memory_id = %s", ([float(x) for x in v], mid))
print("vectors backfilled:", len(rows))

# 3) 评测
qs = [json.loads(l) for l in open(os.path.join(DATA, "questions.jsonl"), encoding="utf-8") if l.strip()]
strict_hits, loose_hits = 0, 0
per_cat = {}
for q in qs:
    qtext = q.get("question", "")
    answer = q.get("answer", "")
    m = re.search(r"\\boxed{(.+?)}", answer)
    ans_text = m.group(1).strip().lower() if m else answer.lower()
    qv = eng.embed(qtext)
    vec_str = "[" + ",".join(f"{float(x):.6f}" for x in qv) + "]"
    cur.execute("""
        SELECT content, 1-(embedding <=> %s::vector) as sim
        FROM memories
        WHERE agent_id = 'lmev2-eval-iso' AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector LIMIT 5
    """, (vec_str, vec_str))
    rows = cur.fetchall()
    recall_text = " ".join((r[0] or "") for r in rows).lower()
    strict = ans_text in recall_text
    words = [w for w in re.split(r"[^a-z0-9\u4e00-\u9fff]+", ans_text) if len(w) > 2]
    loose = any(w in recall_text for w in words) if words else strict
    if strict: strict_hits += 1
    if loose: loose_hits += 1
    cat = q.get("category", "?")
    per_cat.setdefault(cat, [0, 0])
    per_cat[cat][0] += loose
    per_cat[cat][1] += 1
    print(f"Q: {qtext[:25]} | ans={ans_text[:18]} | sim={rows[0][1]:.2f} if rows else 0 | strict={strict}")

n = len(qs)
print()
print("== lmev2_synth 评测（正确 steps 提取）==")
print(f"strict recall: {strict_hits}/{n} = {strict_hits/n:.2%}")
print(f"loose recall:  {loose_hits}/{n} = {loose_hits/n:.2%}")
for cat, (h, t) in sorted(per_cat.items()):
    print(f"  {cat:28s} {h}/{t} = {h/t:.2%}")
