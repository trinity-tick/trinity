# -*- coding: utf-8 -*-
"""lme_multi_retr: multi-session retrieval-layer A/B (entity-expansion)."""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--entity', action='store_true')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\multi_retr.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=350, temp=0.0):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_mretr_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
data = [q for q in data if q['question_type'] == 'multi-session']
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('entity=' + str(args.entity), '| multi n=' + str(len(data)), flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble.")

def parse_date(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

def extract_entities(question):
    caps = re.findall(r'[A-Z][a-zA-Z]{1,}', question)
    quoted = re.findall(r'"([^"]+)"', question)
    nums = re.findall(r'\b\d+\b', question)
    stop = {'what','when','where','how','why','which','who','the','i','my','their','his','her','our','did','was','were','have','has','do','did'}
    ents = list(dict.fromkeys([c for c in caps if c.lower() not in stop] + quoted + nums))
    return ents[:5]

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    agent = 'mrt_' + str(qi)
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
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        merged = {}
        for h in hit_list:
            if h.get('memory_id'):
                merged[h.get('memory_id')] = h
        if args.entity:
            for ent in extract_entities(question)[:4]:
                try:
                    eh = mem.search(ent, top_k=3, agent_id=agent)
                    el = eh.get('results', []) if isinstance(eh, dict) else (eh if isinstance(eh, list) else [])
                    for h in el:
                        if h.get('memory_id') and h.get('memory_id') not in merged and len(merged) < 6:
                            merged[h.get('memory_id')] = h
                except Exception:
                    pass
        hit_list = sorted(merged.values(), key=lambda x: x.get('score', 0), reverse=True)[:5]
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if not c:
                continue
            q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
            turns = re.split(chr(10) + r'(?=\[(?:user|assistant|system)\])', c)
            kept = []
            for t_ in turns[:40]:
                tl = t_.lower()
                if any(term in tl for term in q_terms) or len(kept) < 2:
                    kept.append(t_[:1000])
            c2 = chr(10).join(kept[:8]) if kept else c[:12000]
            ctx.append(c2[:12000])
        blocks = []
        for c in ctx:
            m = re.search(r'\[DATE: ([^\]]+)\]', c)
            d = parse_date(m.group(1)) if m else None
            blocks.append((d, c))
        blocks.sort(key=lambda x: x[0] if x[0] else datetime.max)
        ctx = [b[1] for b in blocks]
        ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx) if ctx else '(no evidence retrieved)'
        try:
            answer = llm_chat(GEN_SYS_PLAIN, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        except Exception as exc:
            answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        import traceback; traceback.print_exc()
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'entity': bool(args.entity), 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
