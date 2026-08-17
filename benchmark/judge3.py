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
parser.add_argument('--temp', type=float, default=0.3)
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

# question text map by question_id
qmap = {}
try:
    with open(args.data, 'r', encoding='utf-8') as f:
        for q in json.load(f):
            qmap[str(q.get('question_id'))] = str(q.get('question', ''))
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
        for _ in range(args.votes):
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
