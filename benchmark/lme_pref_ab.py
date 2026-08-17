# -*- coding: utf-8 -*-
"""pref-only A/B: all 30 single-session-preference questions, base(dated) vs pref2."""
import json, os, sys, time, tempfile, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--mode', default='pref2', choices=['base', 'pref2'])
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_pref_ab.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=300, temp=0.0):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_pref_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
data = [q for q in data if q['question_type'] == 'single-session-preference']
print('preference questions:', len(data), '| mode:', args.mode, flush=True)

GEN_SYS_PLAIN = ('You are a meticulous assistant with access to full past conversation sessions. '
    'Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. '
    'Find it and answer with the exact fact (name, number, date, title). '
    'Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. '
    'Answer with just the fact, no preamble.')

def pref2(question, ctx_text):
    s1 = ('You are analyzing a user conversation archive. Extract the user preferences, tastes and '
          'likes that are evident from the excerpts, focusing on what is RELEVANT to this question: '
          + question + '. Output a compact bullet list of 3-8 specific preferences. If nothing is evident, output: NONE')
    summary = llm_chat(s1, 'Conversation excerpts:\n' + ctx_text[:12000], max_tokens=200)
    if 'NONE' in summary.upper() and len(summary) < 20:
        return 'UNKNOWN'
    s2 = ('You are a personal assistant who knows this user well. The user preferences are: ' + summary[:800] + '\n'
          'Answer the question by giving a personalized reply (a recommendation, a suggestion or a tailored answer) '
          'that clearly follows the user preferences. Answer the question directly; do not restate it.')
    return llm_chat(s2, 'Question: ' + question, max_tokens=250)

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    agent = 'pref_' + str(qi)
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
            d = dates[si] if si < len(dates) else ''
            if d:
                text = '[DATE: ' + str(d) + '] ' + text
            try:
                mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
            except Exception:
                pass
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = [(h.get('content') or '').strip()[:12000] for h in hit_list if (h.get('content') or '').strip()]
        ctx_text = '\n===SESSION===\n'.join(ctx) if ctx else '(no evidence retrieved)'
        if args.mode == 'pref2':
            try:
                answer = pref2(question, ctx_text)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        else:
            try:
                answer = llm_chat(GEN_SYS_PLAIN, 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=300)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': 'single-session-preference', 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': 'single-session-preference', 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 5 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'mode': args.mode, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
