# -*- coding: utf-8 -*-
"""diag_turn: show baseline vs turn answers for multi."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
b = json.load(open(base + 'turn_baseline.json', encoding='utf-8'))['records']
t = json.load(open(base + 'turn_turn.json', encoding='utf-8'))['records']
j = json.load(open(base + 'judge3_turn.json', encoding='utf-8'))
correct = {}
for k, v in j.items():
    correct[k.split('\\')[-1]] = set(v['correct_ids'])
c1 = correct.get('turn_baseline.json', set())
c2 = correct.get('turn_turn.json', set())
bm = {r['question_id']: r for r in b}
tm = {r['question_id']: r for r in t}
print('baseline correct:', len(c1), '| turn correct:', len(c2))
print('turn gained:', len(c2 - c1), '| regressed:', len(c1 - c2))
for qid in list(c2 - c1)[:6]:
    print('  +', qid, '| base:', bm[qid]['answer'][:80], '|| turn:', tm[qid]['answer'][:80])
print()
print('regressions:')
for qid in list(c1 - c2)[:4]:
    print('  -', qid, '| base:', bm[qid]['answer'][:80], '|| turn:', tm[qid]['answer'][:80])
