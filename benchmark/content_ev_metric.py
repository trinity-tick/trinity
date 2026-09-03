#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""content_ev_metric.py — 内容级证据指标（EXECUTION 467）

在会话级 recall 之上度量内容级：
  loc@k：gold 文本覆盖率最高的候选消息在检索结果中的排位（评测专用 oracle 伪标签）
  cov@5 / cov5>=0.5：top-5 上下文能覆盖 gold 词元的比例
用法: python benchmark/content_ev_metric.py   （全量 500，无 LLM，约 5 分钟）
"""
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os, json, tempfile, time, re
sys.path.insert(0, r"C:/Users/Administrator/trinity")
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
from trinity.adapters.sqlite import SQLiteAdapter
data = json.load(open(r"C:/Users/Administrator/trinity/benchmark/data/longmemeval_oracle.json", encoding="utf-8"))

def toks(s):
    return set(re.findall(r"[a-z0-9']{3,}", str(s or "").lower()))

stats = {}
t0 = time.time()
for qi, q in enumerate(data):
    cat = q.get("question_type", "?")
    gold = str(q.get("answer") or "")
    gtok = toks(gold)
    if len(gtok) < 2:
        continue
    tmp = tempfile.mkdtemp(prefix="ce_")
    ad = SQLiteAdapter(db_path=os.path.join(tmp, "s.db"))
    ad.connect()
    sid_list = q.get("haystack_session_ids") or []
    all_msgs = []
    try:
        records = []
        for idx, msgs in enumerate(q.get("haystack_sessions") or []):
            real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
            for mm in msgs:
                content = str(mm.get("content") or "") if isinstance(mm, dict) else str(mm)
                if content.strip():
                    all_msgs.append((real_sid, content[:2000]))
                    records.append({"content": content[:2000], "persona_id": "u1", "session_id": real_sid, "agent_id": "u1", "importance": 0.5})
        ad.ingest_batch(records)
        results = ad.search_memories(query=str(q.get("question") or ""), top_k=30)
    finally:
        ad.disconnect()
    # 全部候选消息里 gold 覆盖率最高的（oracle 伪标签）
    best_cov = 0.0
    for sid, c in all_msgs:
        mt = toks(c)
        cov = len(gtok & mt) / float(len(gtok))
        if cov > best_cov:
            best_cov = cov
    # 检索结果里的覆盖率与定位
    loc = None
    cov_k5 = 0.0
    for i, h in enumerate(results[:30]):
        mt = toks(h.get("content"))
        cov = len(gtok & mt) / float(len(gtok))
        if i < 5 and cov > cov_k5:
            cov_k5 = cov
        if loc is None and cov >= max(0.5, best_cov - 0.01):
            loc = i + 1
    st = stats.setdefault(cat, {"n": 0, "loc1": 0, "loc5": 0, "loc14": 0, "loc30": 0, "best_cov_sum": 0.0, "cov5_sum": 0.0, "cov5_ge50": 0})
    st["n"] += 1
    if loc is not None:
        if loc <= 1: st["loc1"] += 1
        if loc <= 5: st["loc5"] += 1
        if loc <= 14: st["loc14"] += 1
        if loc <= 30: st["loc30"] += 1
    st["best_cov_sum"] += best_cov
    st["cov5_sum"] += cov_k5
    if cov_k5 >= 0.5: st["cov5_ge50"] += 1
    if qi % 50 == 49:
        print("  %d/500 ..." % (qi + 1), flush=True)
out = {}
for c, st in stats.items():
    n = max(1, st["n"])
    out[c] = {"n": st["n"], "loc@1": round(st["loc1"] / n, 3), "loc@5": round(st["loc5"] / n, 3),
               "loc@14": round(st["loc14"] / n, 3), "loc@30": round(st["loc30"] / n, 3),
               "best_cov": round(st["best_cov_sum"] / n, 3), "cov@5": round(st["cov5_sum"] / n, 3),
               "cov5>=0.5": round(st["cov5_ge50"] / n, 3)}
print("ALL:", json.dumps(out))
print("elapsed_s:", round(time.time() - t0, 1))
sys.stdout.flush()