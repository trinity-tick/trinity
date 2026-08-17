#!/usr/bin/env python3
"""RouteReasoner 端到端冒烟（2026-08-17, 产品化验证）。

官方 LongMemEval_S 上选 multi/temporal/pref/plain 各 1 题，摄入临时库后
用真实 DEEPSEEK_API_KEY 跑 RouteReasoner.answer，打印答案与策略。
"""
import json, os, sys, tempfile
sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

DATA = os.path.expanduser(r"~/.trinity/bench-official/longmemeval_s_cleaned.json")
data = json.load(open(DATA, encoding="utf-8"))

want = {"multi-session-reasoning": "multi-session", "temporal-reasoning": None,
        "single-session-preference": None, "knowledge-update": None}
picked = []
for q in data:
    qt = q.get("question_type", "")
    if qt in want and want[qt] is None:
        picked.append(q)
        want[qt] = True
    if sum(1 for v in want.values() if v is True) >= 4:
        break
print(f"picked {len(picked)} questions")

tmpdir = tempfile.mkdtemp(prefix="rr_smoke_")
from trinity import Trinity
mem = Trinity(adapter="sqlite", store_path=tmpdir)

from trinity.qa.route_reasoner import RouteReasoner
rr = RouteReasoner(search_fn=mem.search)
print("api available:", rr.available)

for i, q in enumerate(picked):
    qid = q["question_id"]; qt = q["question_type"]; question = str(q["question"])
    sessions = q.get("haystack_sessions", []); dates = q.get("haystack_dates", []) or []
    agent = f"rr_{i}"
    for si, sess in enumerate(sessions):
        turns = sess if isinstance(sess, list) else sess.get("turns", [])
        parts = []
        for t_ in turns:
            role = t_.get("role", "user") if isinstance(t_, dict) else "user"
            c = t_.get("content", "") if isinstance(t_, dict) else str(t_)
            parts.append(f"[{role}] {c}")
        text = "\n".join(parts)
        if not text.strip():
            continue
        d = dates[si] if si < len(dates) else ""
        if d:
            text = f"[DATE: {d}] " + text
        try:
            mem.ingest(text, agent_id=agent, category="lme", tags=["lme"], postprocess=False)
        except Exception:
            pass
    out = rr.answer(question, qtype=qt, question_date=q.get("question_date"), agent_id=agent)
    print(f"--- {qid} [{qt}] strategy={out.get('strategy')} ---")
    print("Q:", question[:100])
    print("A:", (out.get("answer") or "")[:160])
    print("ev:", out.get("n_evidence"), "lat:", out.get("latency_s"), "err:", out.get("error"))
print("smoke done")
