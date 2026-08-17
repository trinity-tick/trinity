# -*- coding: utf-8 -*-
"""lme_qa_opt3: round-3 A/B experiments on official LongMemEval_S (50q per variant, same-batch baseline).

Variants (--variant):
  baseline     : dated-mode context (existing best) -- same-batch control
  timeline     : P0 - Chain-of-Timeline style: sessions sorted into a timeline with
                 relative-day annotations ([REL: N days before question]) + step-by-step
                 date-diff reasoning. Targets temporal-reasoning.
  stitch       : P1 - cross-session evidence stitching: sessions sorted by date into a
                 timeline, prompt forces per-session fact extraction THEN cross-session
                 aggregation. Targets multi-session.
  pref3        : P2 - two-stage preference with rubric-aligned stage-1: summary asks for
                 concrete preference anchors (tools/platforms/style/budget/level) so stage-2
                 generates specific resource recommendations. Targets single-session-preference.

Usage: python benchmark/lme_qa_opt3.py --variant timeline --qtype temporal-reasoning --limit 50
       python benchmark/lme_qa_opt3.py --variant stitch --qtype multi-session --limit 50
       python benchmark/lme_qa_opt3.py --variant pref3 --qtype single-session-preference --limit 30
Output: records json compatible with lme_judge2.py (question_id/question_type/expected/answer)
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--variant', default='baseline', choices=['baseline', 'timeline', 'stitch', 'pref3'])
parser.add_argument('--qtype', default='temporal-reasoning')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=5)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt3.json')
parser.add_argument('--ctx-out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt3_ctx.jsonl')
parser.add_argument('--seed', type=int, default=42)
parser.add_argument('--no-inner2', action='store_true')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=320, temp=0.0):
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

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_opt3_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
data = [q for q in data if q.get('question_type') == args.qtype]
if args.limit:
    import random; random.seed(args.seed); data = random.sample(data, args.limit)
print('variant=' + args.variant, 'qtype=' + args.qtype, 'n=' + str(len(data)), flush=True)

GEN_SYS_PLAIN = ('You are a meticulous assistant with access to full past conversation sessions. '
    'Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. '
    'Find it and answer with the exact fact (name, number, date, title). '
    'Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. '
    'Answer with just the fact, no preamble.')
GEN_SYS_TEMPORAL = ('You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. '
    'Each excerpt is prefixed with [DATE: ...] and [REL: N days before the question date] showing when the conversation happened. '
    'Read ALL excerpts carefully; the answer IS somewhere in them. '
    'Step 1: list every relevant dated fact from the excerpts (date + relative day offset). '
    'Step 2: compute the answer using date differences / most recent event / explicit day counts, being exact. '
    'Step 3: answer with just the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')
GEN_SYS_STITCH = ('You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. '
    'The excerpts below come from DIFFERENT sessions at DIFFERENT times, sorted chronologically with [DATE: ...] prefixes. '
    'The answer may be split across several sessions. '
    'Step 1: for EACH excerpt, write one line of what key facts it contains relevant to the question. '
    'Step 2: combine/count/compare the facts across sessions to derive the answer. '
    'Step 3: answer with just the exact fact. Do not say UNKNOWN unless the information is truly absent.')
GEN_SYS_MULTI2 = ('You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. '
    'The excerpts come from different sessions at different times, prefixed with [DATE: ...]. '
    'Read ALL of them carefully; the answer IS somewhere in these excerpts, possibly split across sessions. '
    'Combine the relevant facts across sessions and answer with the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')

def pref_two_stage(question, ctx_text, rubric_aligned=False):
    if rubric_aligned:
        s1 = ('You are analyzing a user conversation archive to personalize a response to: ' + question + chr(10) +
              'Extract the user preferences that are RELEVANT to answering this question, as CONCRETE anchors: '
              'specific tools/platforms/products they use, their preferred style/tone, budget or experience level, '
              'past choices or opinions they expressed. Output a compact bullet list of 3-8 specific preferences. '
              'If nothing is evident, output exactly: NONE')
    else:
        s1 = ('You are analyzing a user conversation archive. Extract the user preferences, tastes and '
              'likes that are evident from the excerpts, focusing on what is RELEVANT to this question: '
              + question + '. Output a compact bullet list of 3-8 specific preferences. '
              'If nothing is evident, output: NONE')
    summary = llm_chat(s1, 'Conversation excerpts:' + chr(10) + ctx_text[:12000], max_tokens=220)
    if 'NONE' in summary.upper() and len(summary) < 30:
        return 'UNKNOWN'
    if rubric_aligned:
        s2 = ('You are a personal assistant who knows this user well. User preferences (concrete anchors): ' + summary[:900] + chr(10) +
              'Answer the question with a personalized reply that is SPECIFIC and actionable: '
              "recommend concrete resources/options/products that match the user's actual tools and level "
              '(e.g. if they use Adobe Premiere Pro, point to Premiere-specific advanced resources, not generic video editing guides). '
              'Follow the user preferences closely; answer the question directly; do not restate it.')
    else:
        s2 = ('You are a personal assistant who knows this user well. The user preferences are: ' + summary[:800] + chr(10) +
              'Answer the question by giving a personalized reply (a recommendation, a suggestion or a tailored answer) '
              'that clearly follows the user preferences. Answer the question directly; do not restate it.')
    return llm_chat(s2, 'Question: ' + question, max_tokens=280)

records = []
t0 = time.time()
qdate = None
with open(args.ctx_out, 'w', encoding='utf-8') as ctxf:
    for qi, q in enumerate(data):
        qid = q['question_id']
        qtype = q['question_type']
        question = str(q['question'])
        expected = str(q.get('answer', ''))
        sessions = q.get('haystack_sessions', [])
        dates = q.get('haystack_dates', []) or []
        agent = 'o3_' + args.variant[:3] + '_' + str(qi)
        qdate = parse_date(q.get('question_date')) or qdate
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
                if args.no_inner2:
                    ctx.append(c[:12000])
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
            if args.variant == 'timeline':
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
            elif args.variant == 'stitch':
                blocks = []
                for c in ctx:
                    m = re.search(r'\[DATE: ([^\]]+)\]', c)
                    d = parse_date(m.group(1)) if m else None
                    blocks.append((d, c))
                blocks.sort(key=lambda x: x[0] if x[0] else datetime.max)
                ctx = [b[1] for b in blocks]
            ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx) if ctx else '(no evidence retrieved)'
            if args.variant == 'timeline':
                sys_p = GEN_SYS_TEMPORAL
                answer = llm_chat(sys_p, 'Conversation excerpts (timeline):' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=320)
            elif args.variant == 'stitch':
                sys_p = GEN_SYS_STITCH
                answer = llm_chat(sys_p, 'Conversation excerpts (chronological):' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=320)
            elif args.variant == 'pref3':
                answer = pref_two_stage(question, ctx_text, rubric_aligned=True)
            else:
                if qtype == 'temporal-reasoning':
                    sys_p = GEN_SYS_TEMPORAL.replace(' and [REL: N days before the question date]', '')
                    answer = llm_chat(sys_p, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=320)
                elif qtype == 'multi-session':
                    answer = llm_chat(GEN_SYS_MULTI2, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=320)
                else:
                    answer = pref_two_stage(question, ctx_text, rubric_aligned=False)
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:500], 'n_sessions': len(sessions)})
            ctxf.write(json.dumps({'question_id': qid, 'question_type': qtype, 'question': question, 'ctx_preview': ctx_text[:300]}, ensure_ascii=False) + chr(10))
        except Exception as exc:
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__, 'n_sessions': 0})
        if (qi + 1) % 10 == 0:
            print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'variant': args.variant, 'qtype': args.qtype, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')