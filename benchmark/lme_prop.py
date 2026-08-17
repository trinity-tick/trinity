# -*- coding: utf-8 -*-
"""lme_prop: proposition-based memory (PlugMem-style) A/B for LongMemEval_S."""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--mode', default='verbatim', choices=['verbatim', 'prop', 'proptime', 'proppref'])
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_prop.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=450, temp=0.0):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_prop_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()
import importlib
ppro = importlib.import_module('trinity.modules.second_brain.ppro_profile_retrieval')

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('mode=' + args.mode, '| n=' + str(len(data)), flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to memory propositions extracted from past conversations. "
    "Read ALL propositions carefully. The answer to the question IS somewhere in them. "
    "Find it and answer with the exact fact such as name, number, date or title. "
    "Do not say UNKNOWN unless you have read every proposition and the information is truly absent. "
    "Answer with just the fact, no preamble.")
GEN_SYS_TEMPORAL = ("You are a meticulous assistant answering a question that requires temporal reasoning. "
    "Propositions carry DATE markers. Read ALL propositions carefully. "
    "Step 1: list relevant dated facts. Step 2: compute the answer using date differences or the most recent event. "
    "Step 3: answer with just the exact fact. Do not say UNKNOWN unless the information is truly absent.")

PROP_SYS = ('You extract atomic factual propositions from a conversation session. '
    'Classify each into exactly one of four types. '
    'USER-PREFERENCE which is what the user likes, dislikes or prefers. '
    'USER-FACT which is facts about the user, e.g. role, location, contact, skills. '
    'USER-DID which is events or actions the user did or experienced. '
    'AGENT-DID which is what the assistant told or did. '
    'Each proposition must be a single atomic fact of subject plus predicate plus object, not a summary. '
    'Output ONLY lines, format: TYPE | proposition. At most 20 propositions. '
    'If nothing extractable, output: NONE')

def extract_propositions(session_text):
    try:
        raw = llm_chat(PROP_SYS, 'Conversation session:\n' + session_text[:10000], max_tokens=450)
    except Exception:
        return []
    props = []
    for line in raw.splitlines():
        line = line.strip()
        if '|' not in line:
            continue
        typ, _, text = line.partition('|')
        typ = typ.strip().upper()
        text = text.strip()
        if typ.startswith('USER-PREF') or typ.startswith('USER-FACT') or typ.startswith('USER-DID') or typ.startswith('AGENT-DID'):
            if text and text.upper() != 'NONE':
                props.append((typ, text))
    return props

def parse_date(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    qdate = parse_date(q.get('question_date'))
    agent = 'prp_' + str(qi)
    try:
        if args.mode == 'verbatim':
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
        else:
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
                props = extract_propositions(text)
                for typ, prop in props[:15]:
                    content_p = prop
                    if args.mode in ('proptime', 'proppref') and d:
                        content_p = '[DATE: ' + str(d) + '] ' + content_p
                    cat = 'preference' if typ.startswith('USER-PREF') else 'lme'
                    try:
                        mem.ingest(content_p, agent_id=agent, category=cat, tags=['prop', typ], postprocess=False)
                    except Exception:
                        pass
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = [(h.get('content') or '').strip()[:6000] for h in hit_list if (h.get('content') or '').strip()]
        if args.mode in ('proptime', 'proppref') and qtype == 'temporal-reasoning':
            blocks = []
            for c in ctx:
                m = re.search(r'\[DATE: ([^\]]+)\]', c)
                d = parse_date(m.group(1)) if m else None
                rel = ''
                if d and qdate:
                    rel = ' [REL: ' + str((qdate - d).days) + ' days before question date]'
                if m:
                    c = c.replace(m.group(0), m.group(0) + rel, 1)
                blocks.append((d, c))
            blocks.sort(key=lambda x: x[0] if x[0] else datetime.max)
            ctx = [b[1] for b in blocks]
        ctx_text = chr(10) + '===PROP===' + chr(10).join(ctx) if ctx else '(no evidence retrieved)'
        if qtype == 'single-session-preference' and args.mode == 'proppref':
            try:
                profiler = ppro.UserProfileDeriver()
                profile = profiler.derive(agent, [c[:500] for c in ctx[:5]])
                attrs = '; '.join(str(k) + '=' + str(v) for k, v in list(profile.attributes.items())[:8])
                prefs = '; '.join(str(k) for k in list(profile.preferences.items())[:8])
                s2 = ('You are a personal assistant who knows this user well. User profile: ' + attrs[:600] + ' | ' + prefs[:300] + chr(10) +
                      'Answer the question with a personalized reply that follows the user profile. Answer directly. Do not restate it.')
                answer = llm_chat(s2, 'Question: ' + question, max_tokens=280)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        else:
            sys_p = GEN_SYS_TEMPORAL if (qtype == 'temporal-reasoning' and args.mode in ('proptime', 'proppref')) else GEN_SYS_PLAIN
            try:
                answer = llm_chat(sys_p, 'Memory propositions:\n' + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        import traceback; traceback.print_exc()
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 5 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'mode': args.mode, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
