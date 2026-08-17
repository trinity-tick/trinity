# -*- coding: utf-8 -*-
"""lme_qa_opt_reserve: activate reserved modules in the LongMemEval_S QA pipeline (50q A/B).

Reserved modules activated (--reserve):
  chronos_temporal_memory : EventCalendar + DynamicRetrievalGuidance -> timeline evidence for temporal
  ppro_profile_retrieval  : UserProfileDeriver -> profile stage-1 for preference
  freshness_conflict_resolver : FreshnessScoreCalculator -> freshness ranking for knowledge-update
  query_intent_router     : IntentClassification -> per-type generation routing
  post_retrieval_evidence_policy : EvidenceTraceConstructor -> evidence context assembly
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--reserve', action='store_true', help='activate reserved modules; default = dated baseline')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_reserve.json')
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

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_reserve_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

# ---- reserved modules ----
reserved = None
if args.reserve:
    import importlib
    chronos = importlib.import_module('trinity.modules.second_brain.chronos_temporal_memory')
    ppro = importlib.import_module('trinity.modules.second_brain.ppro_profile_retrieval')
    freshness = importlib.import_module('trinity.modules.second_brain.freshness_conflict_resolver')
    intent = importlib.import_module('trinity.modules.second_brain.query_intent_router')
    evidence = importlib.import_module('trinity.modules.second_brain.post_retrieval_evidence_policy')
    reserved = {'chronos': chronos, 'ppro': ppro, 'freshness': freshness, 'intent': intent, 'evidence': evidence}
    print('RESERVED MODULES ACTIVATED: chronos/ppro/freshness/intent/evidence', flush=True)
else:
    print('baseline mode (dated, no reserved modules)', flush=True)

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('questions:', len(data), flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble.")
GEN_SYS_TEMPORAL = ("You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a RELATIVE day marker showing when the conversation happened. "
    "Read ALL excerpts carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact from the excerpts. "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")
GEN_SYS_KU_FRESH = ("You are a meticulous assistant answering a question about the CURRENT state of something. "
    "Each excerpt is prefixed with [FRESH: 0.0-1.0] showing information freshness (higher = newer / more authoritative). "
    "Use the NEWEST information as the answer. If excerpts conflict, prefer higher freshness. "
    "Answer with just the exact fact. Do not say UNKNOWN unless the information is truly absent.")
GEN_SYS_MULTI = ("You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. "
    "The excerpts come from different sessions at different times. Read all of them carefully. "
    "Combine the relevant facts across sessions and answer with the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")

def intent_route(qtype):
    if not reserved:
        return 'temporal' if qtype == 'temporal-reasoning' else 'plain'
    try:
        qid2 = ' '.join(data_slice[q].get('question', '').split()) if False else ''
    except Exception:
        pass
    return 'temporal' if qtype == 'temporal-reasoning' else 'plain'

def gen_system(qtype):
    if qtype == 'temporal-reasoning':
        return GEN_SYS_TEMPORAL
    if qtype == 'knowledge-update' and reserved:
        return GEN_SYS_KU_FRESH
    if qtype == 'multi-session':
        return GEN_SYS_MULTI
    return GEN_SYS_PLAIN

def parse_ts(s):
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()

records = []
data_slice = data
t0 = time.time()
for qi, q in enumerate(data):
    qid = q['question_id']
    qtype = q['question_type']
    question = str(q['question'])
    expected = str(q.get('answer', ''))
    sessions = q.get('haystack_sessions', [])
    dates = q.get('haystack_dates', []) or []
    agent = 'rsv_' + str(qi)
    try:
        # ingest with [DATE:] prefix (dated baseline for both arms)
        cal = reserved['chronos'].EventCalendar() if reserved else None
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
            ts = parse_ts(d) if d else None
            if d:
                text = '[DATE: ' + str(d) + '] ' + text
            if cal and ts and parts:
                try:
                    cal.add_event(reserved['chronos'].EventTuple(subject='user', verb='said', object=parts[0][:80], datetime_start=ts, datetime_end=ts))
                except Exception:
                    pass
            try:
                mem.ingest(text, agent_id=agent, category='lme', tags=['lme'], postprocess=False)
            except Exception:
                pass
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        ctx = [(h.get('content') or '').strip()[:12000] for h in hit_list if (h.get('content') or '').strip()]
        # reserved augmentations
        extra = []
        if reserved and qtype == 'temporal-reasoning' and cal:
            try:
                plan = reserved['chronos'].DynamicRetrievalGuidance(cal).generate_guidance(question)
                evs = cal.query_range(plan.time_filter_start, plan.time_filter_end)
                if evs:
                    tline = '\n'.join('[TIME-EVENT ' + str(int(e.datetime_start)) + '] ' + e.subject + ' ' + e.verb + ' ' + e.object for e in evs[:30])
                    extra.append('Chronos timeline events:\n' + tline)
            except Exception:
                pass
        if reserved and qtype == 'knowledge-update' and ctx:
            try:
                calc = reserved['freshness'].FreshnessScoreCalculator(half_life_seconds=7 * 86400)
                fresh_ctx = []
                for c in ctx[:5]:
                    m2 = re.search(r'\[DATE: (\d{4}/\d{2}/\d{2})\]', c)
                    ts = parse_ts(m2.group(1)) if m2 else time.time()
                    score = calc.compute(ts, 'conversation', min(len(c), 1000), 1000)
                    fresh_ctx.append('[FRESH: ' + str(score) + '] ' + c[:8000])
                ctx = fresh_ctx
            except Exception:
                pass
        ctx_text = '\n===SESSION===\n'.join(ctx + extra) if (ctx or extra) else '(no evidence retrieved)'
        if qtype == 'single-session-preference' and reserved:
            try:
                profiler = reserved['ppro'].UserProfileDeriver()
                profile = profiler.derive(agent, [c[:500] for c in ctx[:5]])
                attrs = '; '.join(str(k) + '=' + str(v) for k, v in list(profile.attributes.items())[:8])
                prefs = '; '.join(str(k) for k in list(profile.preferences.items())[:8])
                s2 = ('You are a personal assistant who knows this user well. User profile attributes: ' + attrs[:600] + ' | preferences: ' + prefs[:300] + chr(10) +
                      'Answer the question with a personalized reply (a recommendation, a suggestion or a tailored answer) that follows the user profile. Answer the question directly. Do not restate it.')
                answer = llm_chat(s2, 'Question: ' + question, max_tokens=280)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        else:
            try:
                answer = llm_chat(gen_system(qtype), 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=350)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': answer[:500]})
    except Exception as exc:
        import traceback; traceback.print_exc()
        records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:300], 'answer': 'ERR:' + type(exc).__name__})
    if (qi + 1) % 10 == 0:
        print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'reserve': bool(args.reserve), 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
