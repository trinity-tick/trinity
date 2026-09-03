#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os, json, tempfile, time, random, re
sys.path.insert(0, r"C:/Users/Administrator/trinity")
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
from trinity.adapters.sqlite import SQLiteAdapter
data = json.load(open(r"C:/Users/Administrator/trinity/benchmark/data/longmemeval_oracle.json", encoding="utf-8"))
pool = [q for q in data if q.get("question_type") == "temporal-reasoning"]
rng = random.Random(11)
rng.shuffle(pool)
train_q = pool[:90]
hold_q = pool[90:130]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

def toks(s):
    return set(w for w in re.findall(r"[a-z0-9]{3,}", str(s or "").lower()))

def feats_of(rank, msg, qtok, c):
    mt = toks(c)
    q_over = len(qtok & mt) / max(1.0, len(qtok))
    low = c.lower()
    year = any(w[:2] == "20" and len(w) == 4 and w.isdigit() for w in toks(c))
    has_month = any(m in low for m in MONTHS)
    return [rank, round(q_over, 4), 1 if (year or has_month) else 0, round(min(1.0, len(c) / 2000.0), 4)]

def case(q):
    gold = str(q.get("answer") or "")
    gtok = toks(gold)
    qtok = toks(q.get("question"))
    sid_list = q.get("haystack_session_ids") or []
    tmp = tempfile.mkdtemp(prefix="trloc_")
    ad = SQLiteAdapter(db_path=os.path.join(tmp, "s.db"))
    ad.connect()
    allm = []
    try:
        records = []
        for idx, msgs in enumerate(q.get("haystack_sessions") or []):
            real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
            for mm in msgs:
                content = str(mm.get("content") or "") if isinstance(mm, dict) else str(mm)
                if content.strip():
                    c = content[:2000]
                    allm.append((real_sid, c))
                    records.append({"content": c, "persona_id": "u1", "session_id": real_sid, "agent_id": "u1", "importance": 0.5})
        ad.ingest_batch(records)
        results = ad.search_memories(query=str(q.get("question") or ""), top_k=30)
    finally:
        ad.disconnect()
    best = 0.0
    for sid, c in allm:
        cov = len(gtok & toks(c)) / max(1.0, float(len(gtok)))
        if cov > best:
            best = cov
    rows = []
    for i, h in enumerate(results):
        c = str(h.get("content") or "")
        cov = len(gtok & toks(c)) / max(1.0, float(len(gtok)))
        label = 1 if cov >= max(0.5, best - 0.01) else 0
        feat = feats_of(i + 1, h, qtok, c)
        rows.append((feat, label, i + 1, cov))
    return rows

def build_all(qs):
    X = []; Y = []; ORDER = []; COV = []; bounds = []; acc = 0
    for q in qs:
        rows = case(q)
        for feat, label, order, cov in rows:
            X.append(feat); Y.append(label); ORDER.append(order); COV.append(cov)
        acc += len(rows)
        bounds.append(acc)
    return X, Y, ORDER, COV, bounds

print("building train...", flush=True)
Xt, Yt, Ot, Ct, Bt = build_all(train_q)
print("train rows:", len(Xt), flush=True)
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
clf = GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.08, random_state=11)
clf.fit(np.array(Xt), np.array(Yt))
import pickle
pickle.dump(clf, open(os.path.expanduser("~/.trinity/state/tr_locator.pkl"), "wb"))
print("model saved", flush=True)

def loc_metrics(probs, labels, bounds, base_order):
    nq = 0; eng1 = 0; eng5 = 0; mod1 = 0; mod5 = 0
    lo = 0
    for b in bounds:
        seg_p = probs[lo:b]
        seg_y = labels[lo:b]
        seg_o = base_order[lo:b]
        lo = b
        nq += 1
        best_e = min((o for i, o in enumerate(seg_o) if seg_y[i] == 1), default=None)
        best_m = min((i + 1 for i in range(len(seg_p)) if seg_y[i] == 1 and seg_p[i] >= 0), default=None)
        order_m = sorted(range(len(seg_p)), key=lambda j: -seg_p[j])
        pos_m = None
        for r, j in enumerate(order_m):
            if seg_y[j] == 1:
                pos_m = r + 1
                break
        if best_e == 1: eng1 += 1
        if best_e is not None and best_e <= 5: eng5 += 1
        if pos_m == 1: mod1 += 1
        if pos_m is not None and pos_m <= 5: mod5 += 1
    return nq, eng1, eng5, mod1, mod5

print("building holdout...", flush=True)
Xh, Yh, Oh, Ch, Bh = build_all(hold_q)
Ph = clf.predict_proba(np.array(Xh))[:, 1]
nq, e1, e5, m1, m5 = loc_metrics(Ph, Yh, Bh, Oh)
print("HOLDOUT nq=%d engine loc@1=%.3f loc@5=%.3f | rerank loc@1=%.3f loc@5=%.3f" % (nq, e1 / nq, e5 / nq, m1 / nq, m5 / nq), flush=True)
sys.stdout.flush()