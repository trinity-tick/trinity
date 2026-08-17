# -*- coding: utf-8 -*-
"""judge_ab: judge multiple A/B records files with official LongMemEval templates.
Usage: python benchmark/judge_ab.py --in f1.json f2.json ... (each must have records with question_type/expected/answer)
Output: per-file QA accuracy + by-type breakdown, saved to --out (json).
"""
import json, os, sys, urllib.request, argparse, time

parser = argparse.ArgumentParser()
parser.add_argument('--in', dest='inp', nargs='+', required=True)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\ab_judged.json')
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

def judge(rec):
    qtype = rec.get('question_type', 'single-session-user')
    ans = rec.get('answer') or ''
    expected = rec.get('expected') or ''
    qid = rec.get('question_id') or ''
    if ans.upper().startswith('UNKNOWN') or ans.startswith('ERR') or not ans:
        return False
    tmpl = T.get(qtype, T['single-session-user'])
    if tmpl == 'SAME':
        tmpl = T['single-session-user']
    try:
        v = call(tmpl.format(qid, expected[:200], ans[:400]))
        return v.startswith('YES')
    except Exception:
        return False

report = {}
for f in args.inp:
    with open(f, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    recs = data['records']
    verdicts = [judge(r) for r in recs]
    acc = round(sum(verdicts) / max(1, len(verdicts)), 4)
    by = {}
    for r, v in zip(recs, verdicts):
        t = r.get('question_type'); d = by.setdefault(t, [0, 0]); d[0] += 1; d[1] += 1 if v else 0
    report[f] = {
        'variant': data.get('variant'), 'qtype': data.get('qtype'), 'n': len(recs),
        'accuracy': acc,
        'by_type': {t: round(d[1] / d[0], 4) for t, d in by.items()},
        'correct_ids': [r.get('question_id') for r, v in zip(recs, verdicts) if v],
    }
    print(os.path.basename(f), '| acc=' + str(acc) + ' | n=' + str(len(recs)), flush=True)

with open(args.out, 'w', encoding='utf-8') as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
print('judged saved:', args.out)
