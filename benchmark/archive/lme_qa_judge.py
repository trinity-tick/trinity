# -*- coding: utf-8 -*-
"""LLM judge pass for LongMemEval_S QA results, aligned with official task-specific templates.

Official methodology (src/evaluation/evaluate_qa.py): per-question-type judge prompt,
yes/no verdict; temporal-reasoning ignores off-by-one; knowledge-update accepts updated answer.
"""
import json, os, sys, time, argparse, urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--in', dest='inp', default=r'C:\Users\Administrator\.trinity\bench-official\lme_s_full500.json')
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\lme_s_full500_judged.json')
parser.add_argument('--limit', type=int, default=0)
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

TEMPLATES = {
    'single-session-user': ('I will give you a question, a correct answer, and a response from a model. '
        'Please answer yes if the response contains the correct answer. Otherwise, answer no. '
        'If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. '
        'If the response only contains a subset of the information required by the answer, answer no. '
        'Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
    'single-session-assistant': ('I will give you a question, a correct answer, and a response from a model. '
        'Please answer yes if the response contains the correct answer. Otherwise, answer no. '
        'If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. '
        'If the response only contains a subset of the information required by the answer, answer no. '
        'Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
    'multi-session': ('I will give you a question, a correct answer, and a response from a model. '
        'Please answer yes if the response contains the correct answer. Otherwise, answer no. '
        'If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. '
        'If the response only contains a subset of the information required by the answer, answer no. '
        'Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
    'temporal-reasoning': ('I will give you a question, a correct answer, and a response from a model. '
        'Please answer yes if the response contains the correct answer. Otherwise, answer no. '
        'If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. '
        'If the response only contains a subset of the information required by the answer, answer no. '
        'In addition, do not penalize off-by-one errors for the number of days. '
        'If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the response is still correct. '
        'Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
    'knowledge-update': ('I will give you a question, a correct answer, and a response from a model. '
        'Please answer yes if the response contains the correct answer. Otherwise, answer no. '
        'If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer. '
        'Question: {} Correct Answer: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
    'single-session-preference': ('I will give you a question, a rubric for desired personalized response, and a response from a model. '
        'Please answer yes if the response satisfies the desired response. Otherwise, answer no. '
        'The model does not need to reflect all the points in the rubric. '
        'The response is correct as long as it recalls and utilizes the user personal information correctly. '
        'Question: {} Rubric: {} Model Response: {} Is the model response correct? Answer yes or no only.'),
}

def judge(task, answer, expected, question):
    tmpl = TEMPLATES.get(task, TEMPLATES['single-session-user'])
    prompt = tmpl.format(question, expected, answer)
    payload = {'model': 'deepseek-chat',
               'messages': [{'role': 'system', 'content': 'You are an evaluation judge. Reply with exactly YES or NO.'},
                            {'role': 'user', 'content': prompt}],
               'temperature': 0.0, 'max_tokens': 5}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip().upper()

with open(args.inp, 'r', encoding='utf-8') as f:
    data = json.load(f)
results = data['results'] if isinstance(data, dict) else data
if args.limit:
    results = results[:args.limit]

verdicts = []
t0 = time.time()
for i, r in enumerate(results):
    qa = r.get('qa_answer') or ''
    if qa.startswith('ERR:') or not qa:
        verdicts.append(False)
        continue
    exp = r.get('expected') or ''
    task = r.get('question_type') or 'single-session-user'
    try:
        v = judge(task, qa[:400], exp[:200], r.get('question_id', ''))
        ok = v.startswith('YES')
    except Exception:
        ok = False
    verdicts.append(ok)
    if (i + 1) % 50 == 0:
        acc = sum(verdicts) / len(verdicts)
        print('[' + str(i + 1) + '/' + str(len(results)) + '] judged acc=' + str(round(acc, 3)) + ' elapsed=' + str(int(time.time() - t0)) + 's', flush=True)

by_type = {}
for r, v in zip(results, verdicts):
    t_ = r.get('question_type') or 'unknown'
    d = by_type.setdefault(t_, {'n': 0, 'correct': 0})
    d['n'] += 1
    d['correct'] += 1 if v else 0
by_type_out = {k: {'n': v['n'], 'accuracy': round(v['correct'] / v['n'], 4)} for k, v in by_type.items()}

out = {'dataset': 'longmemeval_s_cleaned (official, ICLR 2025)', 'judge_model': 'deepseek-chat',
       'method': 'official task-specific templates (evaluate_qa.py aligned)',
       'qa_accuracy_judged': round(sum(verdicts) / max(1, len(verdicts)), 4),
       'n_judged': len(verdicts), 'elapsed_seconds': round(time.time() - t0, 1),
       'by_type': by_type_out}
with open(args.out, 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
print('judged report:', args.out)
