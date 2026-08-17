# -*- coding: utf-8 -*-
"""lme_multi_con: official LongMemEval 'con' reading method A/B for multi-session.

Official con (from run_generation.py):
  1. For each retrieved session, call LLM once: given session content + question,
     write READING NOTES of user information relevant to answering the question.
     Output "empty" if nothing relevant.
  2. Replace each session with its notes (denoise), sort by date.
  3. Single final call with cot prompt: "first extract all relevant info, then reason" 
     over the notes to produce the answer.

Variants (--variant):
  baseline : dated + inner2 (existing multi baseline, nl text)
  con      : official con method (per-session focused notes + single cot call)
  conjson  : con but history formatted as official JSON (session_date + content)

Usage: python benchmark/lme_multi_con.py --variant baseline|con|conjson --limit 50
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--variant', default='con', choices=['baseline', 'con', 'conjson'])
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=5)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\multi_con.json')
parser.add_argument('--seed', type=int, default=42)
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=500, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

def parse_date(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    from datetime import datetime
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='multi_con_')
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

# Official templates (from LongMemEval run_generation.py)
CON_PROMPT = ('I will give you a chat history between you and a user, as well as a question from the user. '
              'Write reading notes to extract all the relevant user information relevant to answering the answer. '
              'If no relevant information is found, just output "empty". ' + chr(10) + chr(10) +
              'Chat History:' + chr(10) + 'Session Date: {}' + chr(10) + 'Session Content:' + chr(10) + '{}' + chr(10) + chr(10) +
              'Question Date: {}' + chr(10) + 'Question: {}' + chr(10) +
              'Extracted note (information relevant to answering the question):')
COT_TEMPLATE = ('I will give you several history chats between you and a user. '
                'Please answer the question based on the relevant chat history. '
                'Answer the question step by step: first extract all the relevant information, and then reason over the information to get the answer.' + chr(10) + chr(10) +
                'History Chats:' + chr(10) + '{}' + chr(10) + chr(10) +
                'Current Date: {}' + chr(10) + 'Question: {}' + chr(10) + 'Answer (step by step):')
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
    qdate = q.get('question_date', '')
    agent = 'mc_' + args.variant[:3] + '_' + str(qi)
    try:
        for si, sess in enumerate(sessions):
            turns = sess if isinstance(sess, list) else sess.get('turns', [])
            parts = []
            for t_ in turns:
                role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                parts.append('[' + role + '] ' + content)
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
        hits = mem.search(question, top_k=args.top_k, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if not c:
                continue
            ctx.append(c[:12000])
        if args.variant == 'baseline':
            # inner2-style filtering (proven for multi baseline at 40%)
            ctx2 = []
            q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
            for c in ctx:
                turns = re.split(chr(10) + r'(?=\[(?:user|assistant|system)\])', c)
                kept = []
                for t_ in turns[:40]:
                    tl = t_.lower()
                    if any(term in tl for term in q_terms) or len(kept) < 2:
                        kept.append(t_[:1000])
                ctx2.append(chr(10).join(kept[:8]) if kept else c[:12000])
            ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx2)
            answer = llm_chat(GEN_SYS_MULTI, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        else:
            # official con: per-session focused notes
            notes = []
            for c in ctx:
                m = re.search(r'\[DATE: ([^\]]+)\]', c)
                sess_date = m.group(1) if m else 'unknown'
                try:
                    note = llm_chat('', CON_PROMPT.format(sess_date, c[:6000], qdate, question), max_tokens=400)
                except Exception:
                    note = 'empty'
                if note.strip() and 'empty' not in note.strip().lower()[:6]:
                    notes.append((sess_date, note.strip()))
                elif note.strip():
                    notes.append((sess_date, ''))  # keep empty note as placeholder
            notes = [(d, n) for d, n in notes]  # official: keep ALL sessions (empty notes included)
            if args.variant == 'conjson':
                history = ''
                for i, (d, n) in enumerate(notes):
                    history += chr(10) + '### Session ' + str(i + 1) + ':' + chr(10) + 'Session Date: ' + d + chr(10) + 'Session Content:' + chr(10) + json.dumps({'session_summary': n}) + chr(10)
            else:
                history = ''
                for i, (d, n) in enumerate(notes):
                    history += chr(10) + '### Session ' + str(i + 1) + ':' + chr(10) + 'Session Date: ' + d + chr(10) + 'Session Content:' + chr(10) + n + chr(10)
            if not notes:
                history = '(no relevant sessions found)'
            answer = llm_chat('', COT_TEMPLATE.format(history, qdate, question), max_tokens=400)
        records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'variant': args.variant, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
