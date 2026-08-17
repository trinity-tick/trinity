# -*- coding: utf-8 -*-
"""lme_multi_extract: multi-session two-stage structured extraction A/B.

Baseline: dated + inner2 (existing multi baseline).
extract: stage-1 extracts per-session key facts (one LLM call per top session, structured bullets);
         stage-2 aggregates all facts into the answer. No per-type prompt tweaking.

Usage: python benchmark/lme_multi_extract.py --variant baseline|extract --limit 50
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--variant', default='extract', choices=['baseline', 'extract'])
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=5)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\multi_extract.json')
parser.add_argument('--ctx-out', default=r'C:\Users\Administrator\.trinity\bench-official\multi_extract_ctx.jsonl')
parser.add_argument('--seed', type=int, default=42)
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

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='multi_ext_')
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

GEN_SYS_MULTI = ('You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. '
    'The excerpts come from different sessions at different times, prefixed with [DATE: ...]. '
    'Read ALL of them carefully; the answer IS somewhere in these excerpts, possibly split across sessions. '
    'Combine the relevant facts across sessions and answer with the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')
EXTRACT_SYS = ('You are extracting facts from ONE conversation session relevant to a question. '
    'Output ONLY a compact bullet list of the concrete facts from this session that could help answer: '
    'names, numbers, dates, counts, preferences, statements. If the session has nothing relevant, output: NONE')
AGG_SYS = ('You are answering a question using facts extracted from several past conversation sessions. '
    'The facts are listed per session below (each section may be from a different date). '
    'Combine/count/compare the facts across sessions to answer the question with the exact fact. '
    'If the facts together cannot answer it, answer exactly: UNKNOWN')

records = []
t0 = time.time()
with open(args.ctx_out, 'w', encoding='utf-8') as ctxf:
    for qi, q in enumerate(data):
        qid = q['question_id']
        question = str(q['question'])
        expected = str(q.get('answer', ''))
        sessions = q.get('haystack_sessions', [])
        dates = q.get('haystack_dates', []) or []
        agent = 'mx_' + args.variant[:3] + '_' + str(qi)
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
                q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
                turns = re.split(chr(10) + r'(?=\[(?:user|assistant|system)\])', c)
                kept = []
                for t_ in turns[:40]:
                    tl = t_.lower()
                    if any(term in tl for term in q_terms) or len(kept) < 2:
                        kept.append(t_[:1000])
                c2 = chr(10).join(kept[:8]) if kept else c[:12000]
                ctx.append(c2[:12000])
            ctx_text = chr(10) + '===SESSION===' + chr(10).join(ctx) if ctx else '(no evidence retrieved)'
            if args.variant == 'extract':
                # stage-1: per-session fact extraction (one call per session)
                fact_blocks = []
                for si, c in enumerate(ctx):
                    try:
                        facts = llm_chat(EXTRACT_SYS, 'Question: ' + question + chr(10) + 'Session excerpt:' + chr(10) + c[:6000], max_tokens=200)
                        if facts.strip() and 'NONE' not in facts.upper()[:8]:
                            m = re.search(r'\[DATE: ([^\]]+)\]', c)
                            d = m.group(1) if m else ('session ' + str(si))
                            fact_blocks.append('[DATE: ' + d + ']' + chr(10) + facts)
                    except Exception:
                        pass
                facts_text = chr(10) + '---SESSION FACTS---' + chr(10).join(fact_blocks) if fact_blocks else '(no facts extracted)'
                answer = llm_chat(AGG_SYS, 'Question: ' + question + chr(10) + facts_text + chr(10) + 'Answer:', max_tokens=320)
                ctxf.write(json.dumps({'question_id': qid, 'question': question, 'facts_preview': facts_text[:400]}, ensure_ascii=False) + chr(10))
            else:
                answer = llm_chat(GEN_SYS_MULTI, 'Conversation excerpts:' + chr(10) + ctx_text + chr(10) + chr(10) + 'Question: ' + question + chr(10) + 'Answer:', max_tokens=320)
            records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:200], 'answer': answer[:500], 'n_sessions': len(sessions)})
        except Exception as exc:
            records.append({'question_id': qid, 'question_type': 'multi-session', 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__, 'n_sessions': 0})
        if (qi + 1) % 10 == 0:
            print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'variant': args.variant, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
