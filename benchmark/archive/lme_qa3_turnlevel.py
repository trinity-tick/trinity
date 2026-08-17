# -*- coding: utf-8 -*-
"""QA3: turn-level granularity (each message = one memory).

Fixes QA truncation: sessions are long (5-10K chars), the evidence fact sits mid-session.
Storing per-turn makes retrieval find the exact evidence turn, and the context fed to the
LLM is compact and complete.
"""
import json, os, sys, time, tempfile, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=10)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_s_qa3_50.json')
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

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_qa3_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(42); data = random.sample(data, args.limit)
print('qa3 questions:', len(data), flush=True)

GEN_SYS = ('You are answering a question based ONLY on the provided conversation excerpts. '
           'Each excerpt is one message from a past conversation with the user. '
           'Give the exact fact (name, number, date) as the answer. '
           'If the information is not present in the excerpts, answer exactly: UNKNOWN')

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    agent = 'qa3_' + str(qi)
    try:
        for si, sess in enumerate(sessions):
            turns = sess if isinstance(sess, list) else sess.get('turns', [])
            for ti, t_ in enumerate(turns):
                role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                if not content or not content.strip():
                    continue
                text = '[' + role + '] ' + content.strip()
                try:
                    mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
                except Exception:
                    pass
        hits = mem.search(question, top_k=args.top_k, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if c:
                ctx.append(c[:1500])
        ctx_text = '\n'.join(ctx) if ctx else '(no evidence retrieved)'
        try:
            answer = llm_chat(GEN_SYS, 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=200)
        except Exception as exc:
            answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:400]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'records': records}, f, ensure_ascii=False)
print('qa3 saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
