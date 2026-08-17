# -*- coding: utf-8 -*-
"""lme_qa_route_chronos: route2 + chronos fine-grained event extraction (temporal)."""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=0)
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--chronos', action='store_true')
parser.add_argument('--only-type', default='')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_chronos.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=400, temp=0.0):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_chron_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()
import importlib
ppro = importlib.import_module('trinity.modules.second_brain.ppro_profile_retrieval')
chronos = importlib.import_module('trinity.modules.second_brain.chronos_temporal_memory')

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.only_type:
    data = [q for q in data if q['question_type'] == args.only_type]
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('chronos=' + str(args.chronos), '| n=' + str(len(data)), flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble.")
GEN_SYS_TEMPORAL = ("You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a REL marker (days before the question date). "
    "Read ALL excerpts carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact (date + relative days). "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")
GEN_SYS_TEMPORAL_EV = ("You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a REL marker (days before the question date). "
    "A chronological EVENT timeline is also provided. "
    "Read ALL excerpts and the timeline carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact (date + relative days). "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")

def parse_date(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

def extract_events_llm(session_texts, question):
    sys_p = ('You extract timestamped events from conversation excerpts. '
             'An event is a fact about what someone did or what happened at a specific time. '
             'Output ONLY lines, one event per line, format: date|subject|verb|object '
             'where date is YYYY/MM/DD. Extract at most 15 events, skip filler.')
    user_p = 'Conversation excerpts:\n' + session_texts[:12000]
    try:
        raw = llm_chat(sys_p, user_p, max_tokens=400)
    except Exception:
        return []
    events = []
    for line in raw.splitlines():
        line = line.strip()
        parts = [p.strip() for p in line.split('|')]
        if len(parts) == 4 and re.match(r'\d{4}/\d{2}/\d{2}', parts[0]):
            events.append({'date': parts[0], 'subject': parts[1], 'verb': parts[2], 'object': parts[3]})
    return events

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
    agent = 'chr_' + str(qi)
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
        use_inner = qtype == 'temporal-reasoning'
        ctx = []
        for h in hit_list:
            c = (h.get('content') or '').strip()
            if not c:
                continue
            if use_inner:
                q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
                turns = re.split(chr(10) + r'(?=\[(?:user|assistant|system)\])', c)
                kept = []
                for t_ in turns[:40]:
                    tl = t_.lower()
                    if any(term in tl for term in q_terms) or len(kept) < 2:
                        kept.append(t_[:1000])
                c2 = chr(10).join(kept[:8]) if kept else c[:12000]
                ctx.append(c2[:12000])
            else:
                ctx.append(c[:12000])
        extra = []
        if qtype == 'temporal-reasoning':
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
            if args.chronos:
                try:
                    evs = extract_events_llm(chr(10).join(ctx[:3]), question)
                    cal = chronos.EventCalendar()
                    for ev in evs[:15]:
                        dd = parse_date(ev.get('date'))
                        if not dd:
                            continue
                        ts = dd.timestamp()
                        cal.add_event(chronos.EventTuple(subject=ev['subject'][:50], verb=ev['verb'][:50], object=str(ev.get('object', ''))[:80], datetime_start=ts, datetime_end=ts))
                    plan = chronos.DynamicRetrievalGuidance(cal).generate_guidance(question)
                    matched = cal.query_range(plan.time_filter_start, plan.time_filter_end)
                    if matched:
                        tl = []
                        for e in matched[:15]:
                            rel = ''
                            ed = datetime.fromtimestamp(e.datetime_start)
                            if qdate:
                                rel = ' [REL: ' + str((qdate - ed).days) + ' days]'
                            tl.append('[EVENT ' + ed.strftime('%Y/%m/%d') + rel + '] ' + e.subject + ' ' + e.verb + ' ' + e.object)
                        extra.append('Chronos event timeline:\n' + chr(10).join(tl))
                except Exception:
                    pass
        ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx + extra) if (ctx or extra) else '(no evidence retrieved)'
        if qtype == 'single-session-preference':
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
            sys_p = (GEN_SYS_TEMPORAL_EV if args.chronos else GEN_SYS_TEMPORAL) if qtype == 'temporal-reasoning' else GEN_SYS_PLAIN
            try:
                answer = llm_chat(sys_p, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=350)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        import traceback; traceback.print_exc()
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'chronos': bool(args.chronos), 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
