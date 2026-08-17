# -*- coding: utf-8 -*-
"""judge_pref2: preference reason-first dual-pass on existing preference results.

Official template vs reason-first judge agreement on preference answers;
quantifies judge noise flagged in 3.7.
"""
import json, os, urllib.request, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--in', dest='inp', nargs='+', required=True)
parser.add_argument('--out', default=r'C:\Users\Administrator\.trinity\bench-official\pref_judged2.json')
args = parser.parse_args()

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
assert api_key

PREF_TMPL = ('I will give you a question, a rubric for desired personalized response, and a response from a model. '
 'Please answer yes if the response satisfies the desired response. Otherwise, answer no. '
 'The model does not need to reflect all the points in the rubric. '
 'The response is correct as long as it recalls and utilizes the user personal information correctly. '
 'Question: {} Rubric: {} Model Response: {} Is the model response correct? Answer yes or no only.')

def call(prompt, max_tokens=10):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': 'You are an evaluation judge. Reply with exactly YES or NO.'}, {'role': 'user', 'content': prompt}], 'temperature': 0.0, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip().upper()

def judge_official(answer, expected, question):
    v = call(PREF_TMPL.format(question, expected, answer))
    return v.startswith('YES')

def judge_reason_first(answer, expected, question):
    p = ('Compare the model response with the rubric for the question. The rubric describes the desired personalized response. '
         'Decide whether the response satisfies the rubric (recalls and uses the user personal info correctly; it does not need to cover every point). '
         'First give a one-line reason, then on a new line answer exactly YES or NO. '
         'Question: {} Rubric: {} Model Response: {}').format(question, expected, answer)
    out = call(p, max_tokens=60)
    return out.rstrip().endswith('YES'), out

for fn in args.inp:
    with open(fn, encoding='utf-8') as f:
        recs = json.load(f)['records']
    recs = [r for r in recs if r['question_type'] == 'single-session-preference']
    off, rf, agree = [], [], 0
    for r in recs:
        ans = r.get('answer') or ''
        if ans.upper().startswith('UNKNOWN') or ans.startswith('ERR') or not ans:
            off.append(False); rf.append(False); continue
        try:
            a = judge_official(ans[:400], (r.get('expected') or '')[:250], r.get('question_id', ''))
            b, _ = judge_reason_first(ans[:400], (r.get('expected') or '')[:250], r.get('question_id', ''))
            off.append(a); rf.append(b)
            if a == b:
                agree += 1
        except Exception:
            off.append(False); rf.append(False)
    n = len(recs)
    print(fn.split('\\')[-1], '| n=' + str(n), 'official=' + str(round(sum(off)/n, 4)), 'reason-first=' + str(round(sum(rf)/n, 4)), 'agreement=' + str(round(agree/max(1, n), 4)))
