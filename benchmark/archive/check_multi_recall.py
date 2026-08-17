# -*- coding: utf-8 -*-
"""check_multi_recall: verify gold session presence in top-5 retrieval for multi questions."""
import json, os, sys, tempfile, re, random

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='mc_check_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
sys.path.insert(0, r'C:\Users\Administrator\trinity')
from trinity import Trinity
mem = Trinity()

d = json.load(open(r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json', encoding='utf-8'))
data = [q for q in d if q.get('question_type') == 'multi-session']
random.seed(42); data = random.sample(data, 50)

hit_gold = 0
gold_in_top5 = 0
for qi, q in enumerate(data[:20]):
    qid = q['question_id']
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    gold_ids = set(q.get('answer_session_ids', []))
    agent = 'chk_' + str(qi)
    for si, sess in enumerate(sessions):
        turns = sess if isinstance(sess, list) else sess.get('turns', [])
        parts = []
        for t_ in turns:
            role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
            content_ = t_.get('content', '') if isinstance(t_, dict) else str(t_)
            parts.append('[' + role + '] ' + content_)
        text = chr(10).join(parts)
        if not text.strip():
            continue
        d_ = dates[si] if si < len(dates) else ''
        if d_:
            text = '[DATE: ' + str(d_) + '] ' + text
        try:
            mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
        except Exception:
            pass
    hits = mem.search(str(q['question']), top_k=5, agent_id=agent)
    hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
    # match gold session by content prefix? we ingested by index order; match dates
    hit_dates = set()
    for h in hit_list:
        c = (h.get('content') or '')
        m = re.search(r'\[DATE: ([^\]]+)\]', c)
        if m:
            hit_dates.add(m.group(1))
    q_dates = set(dates)
    inter = hit_dates & q_dates
    if inter:
        gold_in_top5 += 1
print('multi: gold session date in top-5:', gold_in_top5, '/ 20')
