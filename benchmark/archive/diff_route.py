# -*- coding: utf-8 -*-
"""diff_route: compare r5_route vs r5_route2 answer diffs."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
r1 = json.load(open(base + 'r5_route_50.json', encoding='utf-8'))['records']
r2 = json.load(open(base + 'r5_route2_50.json', encoding='utf-8'))['records']
j1 = json.load(open(base + 'judge3_route.json', encoding='utf-8'))
# find correct ids per file
correct = {}
for k, v in j1.items():
    name = k.split('/')[-1]
    correct[name] = set(v['correct_ids'])
print('r5_route  correct:', len(correct.get('r5_route_50.json', set())))
print('r5_route2 correct:', len(correct.get('r5_route2_50.json', set())))
m1 = {r['question_id']: r for r in r1}
m2 = {r['question_id']: r for r in r2}
c1 = correct.get('r5_route_50.json', set())
c2 = correct.get('r5_route2_50.json', set())
print('route2 new-correct (route missed):', len(c2 - c1))
print('route2 regressions (route had, route2 lost):', len(c1 - c2))
diffs = [qid for qid in m1 if qid in m2 and m1[qid]['answer'] != m2[qid]['answer']]
print('answer diffs:', len(diffs))
for qid in list(c2 - c1)[:6]:
    print('  NEW-CORRECT', qid, '|', m2[qid]['answer'][:120])
