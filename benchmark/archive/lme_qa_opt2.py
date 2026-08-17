# -*- coding: utf-8 -*-
"""lme_qa_opt2: round-2 optimizations on top of dated mode (official LongMemEval_S).

Flags (combinable; baseline = dated mode):
  --pref2   : two-stage preference generation (pref summary -> personalized reply)
  --multi2  : gentle cross-session prompt + context sorted by [DATE]
  --inner2  : refined inner-session turn filtering (query-terms + last-2 fallback)
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re

parser = argparse.ArgumentParser()
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--limit', type=int, default=50)
parser.add_argument('--top-k', type=int, default=5)
parser.add_argument('--pref2', action='store_true')
parser.add_argument('--multi2', action='store_true')
parser.add_argument('--inner2', action='store_true')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt2x.json')
parser.add_argument('--ctx-out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt2x_ctx.jsonl')
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

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='lme_opt2x_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity import Trinity
mem = Trinity()

with open(args.data, 'r', encoding='utf-8') as f:
    data = json.load(f)
if args.limit:
    import random; random.seed(42); data = random.sample(data, args.limit)
flags = ('pref2=' + str(args.pref2) + ' multi2=' + str(args.multi2) + ' inner2=' + str(args.inner2))
print('flags:', flags, '| questions:', len(data), flush=True)

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
GEN_SYS_MULTI2 = ('You are a meticulous assistant answering a question that requires combining information from MULTIPLE past conversations. '
    'The excerpts come from different sessions at different times, prefixed with [DATE: ...]. '
    'Read ALL of them carefully; the answer IS somewhere in these excerpts, possibly split across sessions. '
    'Combine the relevant facts across sessions and answer with the exact fact. '
    'Do not say UNKNOWN unless the information is truly absent.')

def gen_system(qtype):
    if qtype == 'temporal-reasoning':
        return GEN_SYS_TEMPORAL
    if qtype == 'multi-session' and args.multi2:
        return GEN_SYS_MULTI2
    return GEN_SYS_PLAIN

def preference_two_stage(question, ctx_text):
    """两段式：①从上下文提取用户偏好摘要 ②基于摘要生成个性化回复。"""
    s1 = ('You are analyzing a user conversation archive. Extract the user preferences, tastes and '
          'likes that are evident from the excerpts, focusing on what is RELEVANT to this question: '
          + question + '. Output a compact bullet list of 3-8 specific preferences. '
          'If nothing is evident, output: NONE')
    summary = llm_chat(s1, 'Conversation excerpts:\n' + ctx_text[:12000], max_tokens=200)
    if 'NONE' in summary.upper() and len(summary) < 20:
        return 'UNKNOWN'
    s2 = ('You are a personal assistant who knows this user well. The user preferences are: ' + summary[:800] + '\n'
          'Answer the question by giving a personalized reply (a recommendation, a suggestion or a tailored answer) '
          'that clearly follows the user preferences. Answer the question directly; do not restate it.')
    return llm_chat(s2, 'Question: ' + question, max_tokens=250)

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
        agent = 'o2x_' + str(qi)
        try:
            # ingest with [DATE:] prefix (dated baseline)
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
            hits = mem.search(question, top_k=args.top_k, agent_id=agent)
            hit_list = hits.get('results', []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
            ctx = []
            for h in hit_list:
                c = (h.get('content') or '').strip()
                if not c:
                    continue
                if args.inner2:
                    q_terms = set(re.findall(r'[a-z0-9]+', question.lower()))
                    turns = re.split(r'\n(?=\[(?:user|assistant|system)\])', c)
                    kept = []
                    for t_ in turns[:40]:
                        tl = t_.lower()
                        if any(term in tl for term in q_terms) or len(kept) < 2:
                            kept.append(t_[:1000])
                    c2 = '\n'.join(kept[:8]) if kept else c[:12000]
                    ctx.append(c2[:12000])
                else:
                    ctx.append(c[:12000])
            if args.multi2 and qtype == 'multi-session':
                ctx.sort()  # [DATE: ...] 前缀按字典序即时间序
            ctx_text = '\n===SESSION===\n'.join(ctx) if ctx else '(no evidence retrieved)'
            if qtype == 'single-session-preference' and args.pref2:
                try:
                    answer = preference_two_stage(question, ctx_text)
                except Exception as exc:
                    answer = 'ERR:' + type(exc).__name__
            else:
                try:
                    answer = llm_chat(gen_system(qtype), 'Conversation excerpts:\n' + ctx_text + '\n\nQuestion: ' + question + '\nAnswer:', max_tokens=300)
                except Exception as exc:
                    answer = 'ERR:' + type(exc).__name__
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': answer[:500], 'n_sessions': len(sessions)})
            ctxf.write(json.dumps({'question_id': qid, 'question_type': qtype, 'question': question, 'ctx_preview': ctx_text[:400]}, ensure_ascii=False) + '\n')
        except Exception as exc:
            records.append({'question_id': qid, 'question_type': qtype, 'expected': expected[:200], 'answer': 'ERR:' + type(exc).__name__, 'n_sessions': 0})
        if (qi + 1) % 10 == 0:
            print('[' + str(qi + 1) + '/' + str(len(data)) + '] elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

with open(args.out, 'w', encoding='utf-8') as f:
    json.dump({'flags': flags, 'records': records, 'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
print('saved:', args.out, 'elapsed:', int(time.time() - t0), 's')
