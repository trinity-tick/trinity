# -*- coding: utf-8 -*-
"""QA2b: session-level ingest (fast) + full-session context + stronger answer prompt."""
import json, os, sys, time, tempfile, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_s_qa2b_50.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=250, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_qa2b_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(42); data = random.sample(data, args.limit)
print('qa2b questions:', len(data), flush=True)

GEN_SYS = ('You are a meticulous assistant with access to full past conversation sessions. '
           'Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. '
           'Find it and answer with the exact fact (name, number, date, title). '
           'Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. '
           'Answer with just the fact, no preamble.')

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    agent = 'qa2b_' + str(qi)
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
                pass
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if c:
                ctx.append(c[:15000])
        ctx_text = '\n===SESSION===\n'.join(ctx) if ctx else '(no evidence retrieved)'
        try:
            answer = llm_chat(GEN_SYS, 'Conversation sessions:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=250)
        except Exception as exc:
            answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:500]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'records': records}, f, ensure_ascii=False)
print('qa2b saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
