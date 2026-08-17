# -*- coding: utf-8 -*-
"""lme_multi_turn: turn-granularity retrieval A/B for multi-session (official GRANULARITY=turn).

Ingests each TURN as its own memory unit (not whole session). Retrieval returns
top-k turns. Context assembled from retrieved turns (dedup, sorted by date).
Official reader expands each retrieved turn into a round (turn + next turn).

Variants:
  baseline : session-granularity ingest (existing multi baseline, inner2 off)
  turn     : turn-granularity ingest, top-k turns, expand to round

Usage: python benchmark/lme_multi_turn.py --variant baseline|turn --limit 50
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--variant', default='turn', choices=['baseline', 'turn'])
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=12)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\multi_turn.json')
parser.add_argument('--seed', type=int, default=42)
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=350, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='multi_turn_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
data = [q for q in data if q.get('question_type') == 'multi-session']
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('variant=' + args.variant, 'n=' + str(len(data)), flush=True)

GEN_SYS_MULTI = ('You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. '
    'The excerpts come from different sessions at different times, prefixed with [DATE: ...]. '
    'Read ALL of them carefully; the answer IS somewhere in these excerpts, possibly split across sessions. '
    'Combine the relevant facts across sessions and answer with the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    agent = 'mt_' + args.variant[:2] + '_' + str(qi)
    try:
        if args.variant == 'turn':
            # ingest per-turn: each turn is a memory unit with [DATE:] + [ROLE:] + turn content
            for si, sess in enumerate(sessions):
                turns = sess if isinstance(sess, list) else sess.get('turns', [])
                d = dates[si] if si < len(dates) else ''
                for ti, t_ in enumerate(turns):
                    role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                    content_ = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                    if not content_.strip():
                        continue
                    text = content_.strip()
                    if d:
                        text = '[DATE: ' + str(d) + '] [' + role + '] ' + text
                    try:
                        mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
                    except Exception:
                        pass
            hits = mem.search(question, top_k=args.top_k, agent_id=agent)
        else:
            # session granularity (baseline)
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
                d = dates[si] if si < len(dates) else ''
                if d:
                    text = '[DATE: ' + str(d) + '] ' + text
                try:
                    mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
                except Exception:
                    pass
            hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = []
        seen = set()
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            ctx.append(c[:1200])
        ctx_text = chr(10) + '===TURN===' + chr(10).join(ctx[:16]) if ctx else '(no evidence retrieved)'
        answer = llm_chat(GEN_SYS_MULTI, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'variant': args.variant, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
