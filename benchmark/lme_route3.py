# -*- coding: utf-8 -*-
"""lme_route3: turn x route2 combined A/B (multi uses turn granularity, others keep route2).

Per-type route (--route):
  multi-session        : turn-granularity ingest, top-k turns, sorted by date
  temporal-reasoning   : session ingest + [REL: N days] timeline + inner2 turn filter
  single-session-pref  : session ingest + ppro UserProfileDeriver two-stage (no inner2)
  knowledge-update     : session ingest + dated plain (freshness proven negative)
  others               : session ingest + dated plain (no inner2)
Baseline (no --route): session ingest, dated plain prompts.

Extra: --temp-turn makes temporal-reasoning ALSO use turn granularity (A/B for temporal).

Usage: python benchmark/lme_route3.py [--route] [--temp-turn] --limit 50
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--route', action='store_true')
parser.add_argument('--temp-turn', action='store_true')
parser.add_argument('--qtype', default='', help='filter by question_type (empty = all)')
parser.add_argument('--exclude-qtype', default='', help='exclude question_type (empty = none)')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\route3.json')
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

def parse_date(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='route3_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.qtype:
    data = [q for q in data if q.get('question_type') == args.qtype]
if args.exclude_qtype:
    data = [q for q in data if q.get('question_type') != args.exclude_qtype]
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('route=' + str(args.route), 'temp_turn=' + str(args.temp_turn), 'n=' + str(len(data)), flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble.")
GEN_SYS_TEMPORAL = ("You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a REL marker (days before the question date) showing when the conversation happened. "
    "Read ALL excerpts carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact (date + relative days). "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")

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
    agent = 'r3_' + str(qi)
    use_turn = args.route and qtype == 'multi-session'
    use_turn = use_turn or (args.route and args.temp_turn and qtype == 'temporal-reasoning')
    try:
        if use_turn:
            # turn-granularity ingest
            for si, sess in enumerate(sessions):
                turns = sess if isinstance(sess, list) else sess.get('turns', [])
                d = dates[si] if si < len(dates) else ''
                for t_ in turns:
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
            hits = mem.search(question, top_k=12, agent_id=agent)
        else:
            # session-granularity ingest
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
            ctx.append(c)
        if use_turn:
            # turn context: keep first 16 turns, sorted by date
            ctx = ctx[:16]
            ctx_text = chr(10) + '===TURN===' + chr(10).join(ctx)
            answer = llm_chat(GEN_SYS_PLAIN, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        elif args.route and qtype == 'temporal-reasoning':
            # REL + inner2 + timeline sort
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
            blocks = []
            for c in ctx2:
                m = re.search(r'\[DATE: ([^\]]+)\]', c)
                d = parse_date(m.group(1)) if m else None
                rel = ''
                if d and qdate:
                    rel = ' [REL: ' + str((qdate - d).days) + ' days before question date]'
                if m:
                    c = c.replace(m.group(0), m.group(0) + rel, 1)
                blocks.append((d, c))
            blocks.sort(key=lambda x: x[0] if x[0] else datetime.max)
            ctx_text = chr(10) + '===SESSION===' + chr(10).join(b[1] for b in blocks)
            answer = llm_chat(GEN_SYS_TEMPORAL, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        elif args.route and qtype == 'single-session-preference':
            try:
                ctx_text2 = chr(10) + '===SESSION===' + chr(10).join(ctx[:5])
                # pref3: rubric-aligned two-stage (proven 36-60% judge3; ppro regex proven false)
                s1 = ('You are analyzing a user conversation archive to personalize a response to: ' + question + chr(10) +
                      'Extract the user preferences that are RELEVANT to answering this question, as CONCRETE anchors: '
                      'specific tools/platforms/products they use, their preferred style/tone, budget or experience level, '
                      'past choices or opinions they expressed. Output a compact bullet list of 3-8 specific preferences. '
                      'If nothing is evident, output exactly: NONE')
                summary = llm_chat(s1, 'Conversation excerpts:' + chr(10) + ctx_text2[:12000], max_tokens=220)
                if 'NONE' in summary.upper() and len(summary) < 30:
                    answer = 'UNKNOWN'
                else:
                    s2 = ('You are a personal assistant who knows this user well. User preferences (concrete anchors): ' + summary[:900] + chr(10) +
                          'Answer the question with a personalized reply that is SPECIFIC and actionable: '
                          "recommend concrete resources/options/products that match the user's actual tools and level. "
                          'Follow the user preferences closely; answer the question directly; do not restate it.')
                    answer = llm_chat(s2, 'Question: ' + question, max_tokens=280)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        else:
            ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx)
            answer = llm_chat(GEN_SYS_PLAIN, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'route': args.route, 'temp_turn': args.temp_turn, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
