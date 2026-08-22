"""multi-session 检索召回诊断（2026-08-21，PlugMem 对照）"""
import json
import os
import random
import tempfile
import time

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='recall_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity

data = json.load(open(r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json', encoding='utf-8'))
random.seed(42)
sample = random.sample(data, 50)
multi = [q for q in sample if q.get('question_type') == 'multi-session']
print(f'multi 题数: {len(multi)}', flush=True)

mem = Trinity()
t0 = time.time()
r12 = r20 = 0
sessions_in_top12 = 0
for qi, q in enumerate(multi):
    qid = q['question_id']
    gold_sids = set(q.get('answer_session_ids') or [])
    sessions = q.get('haystack_sessions', [])
    sids = q.get('haystack_session_ids') or []
    dates = q.get('haystack_dates') or []
    agent = f'r_{qi}'
    for si, sess in enumerate(sessions):
        turns = sess if isinstance(sess, list) else sess.get('turns', [])
        sid = sids[si] if si < len(sids) else f's{si}'
        d = dates[si] if si < len(dates) else ''
        for t_ in turns:
            role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
            content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
            if not content.strip():
                continue
            text = content.strip()
            if d:
                text = '[DATE: ' + str(d) + '] [' + role + '] ' + text
            try:
                mem.ingest(text, agent_id=agent, category='lme', tags=['lme', 'sid-' + str(sid)], postprocess=False)
            except Exception:
                pass
    for k in (12, 20):
        hits = mem.search(str(q['question']), top_k=k, agent_id=agent)
        hl = hits.get('results', []) if isinstance(hits, dict) else hits
        hit_sids = set()
        for h in hl:
            for tag in (h.get('tags') or []):
                if str(tag).startswith('sid-'):
                    hit_sids.add(str(tag)[4:])
        if gold_sids & hit_sids:
            if k == 12:
                r12 += 1
            else:
                r20 += 1
        if k == 12:
            sessions_in_top12 += len(hit_sids)
    if (qi + 1) % 5 == 0:
        print(f'[{qi+1}/{len(multi)}] elapsed={int(time.time()-t0)}s', flush=True)

print(f'\nmulti 检索召回: top_k=12 gold命中 {r12}/{len(multi)} = {100*r12/len(multi):.0f}%', flush=True)
print(f'multi 检索召回: top_k=20 gold命中 {r20}/{len(multi)} = {100*r20/len(multi):.0f}%', flush=True)
print(f'平均 top12 覆盖会话数: {sessions_in_top12/len(multi):.1f}', flush=True)
