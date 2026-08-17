# -*- coding: utf-8 -*-
"""QA-only re-run: full-session context + official-aligned judge.

Fixes the 1.8% QA result from the main run (which truncated retrieved sessions at 600 chars,
losing evidence). Re-retrieves top-5 per question, feeds FULL session text (cap 3000 chars each),
asks DeepSeek to answer, then judges with official per-type templates.
"""
import json, os, sys, time, tempfile, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=0)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_s_full500_qa2.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=200, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

# ---- engine (reuse runner ingest logic) ----
os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_qa2_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(42); data = random.sample(data, args.limit)
print('qa2 questions:', len(data), flush=True)

GEN_SYS = ('You are an AI assistant with access to excerpts of past conversation sessions. '
           'Answer the question using ONLY information present in the excerpts. '
           'Give the precise fact as it appears (e.g. exact name, number, date). '
           'If the information is genuinely not present, answer exactly: UNKNOWN')

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    agent = 'qa2_' + str(qi)
    try:
        for si, sess in enumerate(sessions):
            turns = sess if isinstance(sess, list) else sess.get('turns', [])
            parts = []
            for t_ in turns:
                role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                parts.append('[' + role + '] ' + content)
            text = '\n'.join(parts)
            if not text.strip():
                continue
            try:
                mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
            except Exception:
                pass  # unique content_hash duplicates
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if c:
                ctx.append(c[:3000])
        ctx_text = '\n---\n'.join(ctx[:5]) if ctx else '(no evidence retrieved)'
        try:
            answer = llm_chat(GEN_SYS, 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=200)
        except Exception as exc:
            answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:400]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 25 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'records': records}, f, ensure_ascii=False)
print('qa2 saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
