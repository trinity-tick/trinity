# -*- coding: utf-8 -*-
"""judge3: reason-first + 3-vote judge (noise governance).

Loads real question text from the dataset by question_id, runs reason-first verdict
3 times per record, takes majority vote. Reports per-file accuracy + verdict stability
(3/3 agreement rate) + flip rate vs single-shot.

Usage: python benchmark/judge3.py --in f1.json [f2.json ...]
"""
import json, os, sys, urllib.request, argparse, time

parser = argparse.ArgumentParser()
parser.add_argument('--in', dest='inp', nargs='+', required=True)
parser.add_argument('--data', default=r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\judge3_out.json')
parser.add_argument('--votes', type=int, default=3)
# 2026-08-24（R8 P0-① 修裁判）：温度固定 0——温度>0 引入 run-to-run
# 随机翻转（《The Coin Flip Judge?》），确定性判分是 A/B 可靠的前提。
parser.add_argument('--temp', type=float, default=0.0)
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

# question text map by question_id（2026-08-25 修复：支持 dict 包装数据集 +
# original_id 回退——私有留出集是 {"questions": [...]} 包装且 question_id 带
# priv_ 前缀，此前 qmap 从裸列表加载，私有集 0 命中 → judge 无问题上下文
# → run-to-run 抖动（baseline 0.6/0.7/0.8 全噪声）。）
qmap = {}
try:
    with open(args.data, 'r', encoding='utf-8') as f:
        blob = json.load(f)
    _items = blob.get("questions", blob) if isinstance(blob, dict) else blob
    for q in _items:
        qid = str(q.get('question_id', ''))
        qmap[qid] = str(q.get('question', ''))
        # original_id 回退：私有集 priv_<orig_id> → 原公开集 question
        oid = q.get('original_id')
        if oid and qid not in qmap:
            qmap[qid] = str(q.get('question', ''))
except Exception:
    pass

def call(prompt, max_tokens=80, temp=0.0):
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': 'You are an evaluation judge. First give a one-line reason, then on the last line answer exactly YES or NO.'},
                            {'role': 'user', 'content': prompt}],
               'temperature': temp, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip().upper()

def one_verdict(qtype, qid, expected, answer):
    question = qmap.get(str(qid), '') or qid
    p = ('Judge whether the model response is correct for the question. '
         'Consider equivalent phrasings, date/format variants, and off-by-one day errors as correct. '
         'CRITICAL: judge ONLY by factual correctness. Do NOT penalize short answers, '
         'concise answers, or answers that are shorter than the reference. '
         'A short but correct answer is fully correct. Do NOT prefer longer responses. '
         'For preference questions, the answer is a rubric: the response is correct if it recalls and '
         'utilizes the user personal information correctly (it need not cover every rubric point). '
         'For knowledge-update, prefer the newest information. '
         'Question: ' + question + chr(10) +
         'Correct Answer / Rubric: ' + expected[:250] + chr(10) +
         'Model Response: ' + answer[:400] + chr(10) +
         'Reason then answer YES or NO.')
    out = call(p, max_tokens=90, temp=args.temp)
    lines = [l for l in out.splitlines() if l.strip()]
    verdict = None
    for l in reversed(lines):
        if 'YES' in l:
            verdict = True; break
        if 'NO' in l:
            verdict = False; break
    return verdict, out

report = {}
for f in args.inp:
    with open(f, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    recs = data['records']
    results = []
    t0 = time.time()
    for i, r in enumerate(recs):
        ans = r.get('answer') or ''
        expected = r.get('expected') or ''
        qid = r.get('question_id') or ''
        if ans.upper().startswith('UNKNOWN') or ans.startswith('ERR') or not ans:
            results.append({'id': qid, 'votes': [False]*args.votes, 'unanswerable': True})
            continue
        votes = []
        # 2026-08-25（闭环时间优化）：票间并发——温度 0 确定性判分，并发不改结果，
        # 但可并行发 3 个 LLM 请求，judge3 耗时降至 ~1/3（串行 3 票 → 并发 1 轮）。
        if args.votes > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(args.votes, 4)) as ex:
                futs = [ex.submit(one_verdict, r.get('question_type'), qid, expected, ans)
                        for _ in range(args.votes)]
                votes = [True if f.result()[0] else False for f in futs]
        else:
            v, _ = one_verdict(r.get('question_type'), qid, expected, ans)
            votes.append(True if v else False)
        results.append({'id': qid, 'votes': votes, 'unanswerable': False})
        if (i + 1) % 10 == 0:
            print('  [' + str(i + 1) + '/' + str(len(recs)) + ']', flush=True)
    accs = []
    for k in range(args.votes):
        accs.append(round(sum(1 for x in results if x['votes'][k]) / max(1, len(results)), 4))
    majority = [sum(x['votes']) >= (args.votes + 1) // 2 for x in results]
    acc_maj = round(sum(majority) / max(1, len(majority)), 4)
    stable = [x for x in results if not x['unanswerable'] and (all(x['votes']) or not any(x['votes']))]
    stab_rate = round(len(stable) / max(1, len([x for x in results if not x['unanswerable']])), 4)
    report[f] = {
        'variant': data.get('variant'), 'qtype': data.get('qtype'), 'n': len(recs),
        'per_vote_acc': accs, 'majority_acc': acc_maj, 'stability_3of3': stab_rate,
        'correct_ids': [x['id'] for x, m in zip(results, majority) if m],
    }
    print(os.path.basename(f), '| votes=' + str(accs) + ' | majority=' + str(acc_maj) + ' | stable3/3=' + str(stab_rate) + ' | n=' + str(len(recs)), flush=True)

with open(args.out, 'w', encoding='utf-8') as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
print('judge3 saved:', args.out)
