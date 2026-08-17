# -*- coding: utf-8 -*-
"""lme_qa_opt: modular optimized QA benchmark for official LongMemEval_S.

Modes (A/B via --mode):
  plain           : QA2b baseline (session ingest, full context, plain prompt)
  dated           : + [DATE: ...] prefix from haystack_dates + temporal step-by-step prompt
  types           : dated + per-type generation prompts (preference/multi/knowledge-update)
  inner           : types + inner-session turn filtering (focus context on evidence turns)

Saves per-question retrieved context for post-hoc review.
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=5)
parser.add_argument('--mode', default='types', choices=['plain', 'dated', 'types', 'inner'])
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt.json')
parser.add_argument('--ctx-out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt_ctx.jsonl')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

def llm_chat(system, user, max_tokens=300, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_opt_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(42); data = random.sample(data, args.limit)
print('mode=' + args.mode, 'questions:', len(data), flush=True)

# ---- per-type generation prompts ----
GEN_SYS_PLAIN = ('You are a meticulous assistant with access to full past conversation sessions. '
    'Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. '
    'Find it and answer with the exact fact (name, number, date, title). '
    'Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. '
    'Answer with just the fact, no preamble.')
GEN_SYS_TEMPORAL = ('You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. '
    'Each excerpt is prefixed with [DATE: ...] showing when the conversation happened. '
    'Read ALL excerpts carefully; the answer IS somewhere in them. '
    'Step 1: list every relevant date fact from the excerpts. '
    'Step 2: compute the answer (date differences, most recent event, etc.) carefully. '
    'Step 3: answer with just the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')
GEN_SYS_PREFERENCE = ('You are a personal assistant answering a question about how this user likes to be served. '
    'Read ALL excerpts carefully; the user preferences ARE in them. '
    'First, in one line, state the user preferences you found. '
    'Then produce a short personalized reply that follows those preferences (e.g. a recommendation, a suggested option, a tailored answer). '
    'Answer the question directly. Do not restate the question.')
GEN_SYS_MULTI = ('You are answering a question that requires combining information from MULTIPLE different conversations. '
    'The excerpts come from different sessions at different times. Read all of them. '
    'Combine the relevant facts across sessions to answer. Answer with just the exact fact. '
    'If the information is not present, answer exactly: UNKNOWN')
GEN_SYS_KU = ('You are a meticulous assistant answering a question about the CURRENT state of something. '
    'Read ALL excerpts carefully. If the excerpts contain both old and updated information, use the NEWEST information as the answer. '
    'Answer with just the exact fact. Do not say UNKNOWN unless the information is truly absent.')

def gen_system(qtype):
    if args.mode == 'plain':
        return GEN_SYS_PLAIN
    if args.mode in ('dated', 'types', 'inner'):
        if args.mode == 'plain':
            return GEN_SYS_PLAIN
        if args.mode == 'dated':
            return GEN_SYS_TEMPORAL if qtype == 'temporal-reasoning' else GEN_SYS_PLAIN
        m = {
            'temporal-reasoning': GEN_SYS_TEMPORAL,
            'single-session-preference': GEN_SYS_PREFERENCE,
            'multi-session': GEN_SYS_MULTI,
            'knowledge-update': GEN_SYS_KU,
        }
        return m.get(qtype, GEN_SYS_PLAIN)
    return GEN_SYS_PLAIN

records = []
t0 = time.time()
with open(args.ctx_out, 'w', encoding='utf-8') as ctxf:
    for qi, q in enumerate(data):
        qid = q['question_id']
        qtype = q['question_type']
        question = str(q['question'])
        expected = str(q.get('answer', ''))
        sessions = q.get('haystack_sessions', [])
        dates = q.get('haystack_dates', []) or []
        agent = 'opt_' + str(qi)
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
                if args.mode in ('dated', 'types', 'inner'):
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
            ctx_meta = []
            for h in hit_list:
                c = (h.get('content') or '').strip()
                if not c:
                    continue
                if args.mode == 'inner':
                    # 会话内二次检索：按查询词过滤 turn，聚焦证据
                    q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
                    turns = re.split(r'\n(?=\[(?:user|assistant|system)\])', c)
                    kept = []
                    for t_ in turns[:40]:
                        tl = t_.lower()
                        if any(term in tl for term in q_terms):
                            kept.append(t_[:1000])
                    c2 = '\n'.join(kept[:6]) if kept else c[:12000]
                    ctx.append(c2[:12000])
                else:
                    ctx.append(c[:12000])
                ctx_meta.append({'memory_id': h.get('memory_id'), 'score': h.get('score'), 'created_at': h.get('created_at')})
            ctx_text = '\n===SESSION===\n'.join(ctx) if ctx else '(no evidence retrieved)'
            sys_p = gen_system(qtype)
            try:
                answer = llm_chat(sys_p, 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=300)
            except Exception as exc:
                answer = 'ERR:' + type(exc).__name__
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:500], 'n_sessions': len(sessions)})
            ctxf.write(json.dumps({'question_id': qid, 'question_type': qtype, 'question': question, 'ctx_meta': ctx_meta, 'ctx_preview': ctx_text[:500]}, ensure_ascii=False) + '\n')
        except Exception as exc:
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__, 'n_sessions': 0})
        if (qi + 1) % 10 == 0:
            print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'mode': args.mode, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
