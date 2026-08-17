# -*- coding: utf-8 -*-
"""judge2: reason-then-verdict judge for temporal + official-template pass + agreement check.

Two judge passes on the same answers:
  1. official per-type templates (same as before) -> strict accuracy
  2. temporal gets a reason-first judge (write reasoning, then YES/NO) -> relaxed accuracy
Reports both + agreement rate on a sample.
"""
import json, os, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--in', dest='inp', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt_dated_full500.json')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_opt_dated_judged.json')
parser.add_argument('--sample', type=int, default=60, help='temporal sample for dual-pass agreement')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

T = {
 'single-session-user': 'I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.',
 'single-session-assistant': 'SAME',
 'multi-session': 'SAME',
 'temporal-reasoning': 'I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the response is still correct. Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.',
 'knowledge-update': 'I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer. Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.',
 'single-session-preference': 'I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user personal information correctly. Question: {} Rubric: {} Model Response: {} Is the model response correct? Answer yes or no only.',
}

def call(prompt, max_tokens=10):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': 'You are an evaluation judge. Reply with exactly YES or NO.'}, {'role': 'user', 'content': prompt}], 'temperature': 0.0, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip().upper()

def judge_official(task, answer, expected, question):
    tmpl = T.get(task, T['single-session-user'])
    if tmpl == 'SAME':
        tmpl = T['single-session-user']
    v = call(tmpl.format(question, expected, answer))
    return v.startswith('YES')

def judge_reason_first(task, answer, expected, question):
    """先推理再判定（宽松口径）：模型先简述判断理由，再给 YES/NO。"""
    p = ('Compare the model response with the correct answer for the question. '
         'Consider equivalent phrasings, date/format variants, and off-by-one day errors as correct. '
         'First give a one-line reason, then on a new line answer exactly YES or NO. '
         'Question: {} Correct Answer: {} Model Response: {}').format(question, expected, answer)
    out = call(p, max_tokens=60)
    return out.rstrip().endswith('YES'), out

with open(args.inp, 'r', encoding='utf-8') as f:
    data = json.load(f)
recs = data['records']

# pass 1: official templates on ALL
verdicts = []
import time; t0 = time.time()
for i, r in enumerate(recs):
    ans = r.get('answer') or ''
    if ans.upper().startswith('UNKNOWN') or ans.startswith('ERR') or not ans:
        verdicts.append(False); continue
    try:
        verdicts.append(judge_official(r.get('question_type'), ans[:400], (r.get('expected') or '')[:200], r.get('question_id', '')))
    except Exception:
        verdicts.append(False)
    if (i + 1) % 100 == 0:
        print('official pass [' + str(i + 1) + '/' + str(len(recs)) + '] acc=' + str(round(sum(verdicts)/len(verdicts), 4)), flush=True)

acc_official = round(sum(verdicts) / max(1, len(verdicts)), 4)
by = {}
for r, v in zip(recs, verdicts):
    t = r.get('question_type'); d = by.setdefault(t, [0, 0]); d[0] += 1; d[1] += 1 if v else 0

# pass 2: reason-first on temporal sample (agreement check)
import random; random.seed(7)
temporal = [(r, v) for r, v in zip(recs, verdicts) if r.get('question_type') == 'temporal-reasoning']
sample = random.sample(temporal, min(args.sample, len(temporal)))
agree = 0; relaxed = 0
for r, v in sample:
    ans = r.get('answer') or ''
    if ans.upper().startswith('UNKNOWN') or ans.startswith('ERR') or not ans:
        continue
    try:
        rv, _ = judge_reason_first(r.get('question_type'), ans[:400], (r.get('expected') or '')[:200], r.get('question_id', ''))
        if rv == v:
            agree += 1
        if rv:
            relaxed += 1
    except Exception:
        pass
n2 = len(sample)
print('temporal dual-pass: n=' + str(n2) + ' agreement=' + str(round(agree/max(1, n2), 4)) + ' relaxed_acc=' + str(round(relaxed/max(1, n2), 4)))

out = {'mode': 'dated', 'n': len(recs), 'qa_accuracy_official': acc_official, 'elapsed': round(time.time() - t0, 1),
       'by_type_official': {t: {'n': d[0], 'accuracy': round(d[1]/d[0], 4)} for t, d in by.items()},
       'temporal_dual_pass': {'n': n2, 'agreement': round(agree/max(1, n2), 4), 'relaxed_accuracy': round(relaxed/max(1, n2), 4)}}
with open(args.out, 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
print('judged saved:', args.out)
