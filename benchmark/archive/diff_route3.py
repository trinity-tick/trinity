# -*- coding: utf-8 -*-
"""diff_route3: compare r5_route vs r5_route2 with full-path keys."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
r1 = json.load(open(base + 'r5_route_50.json', encoding='utf-8'))['records']
r2 = json.load(open(base + 'r5_route2_50.json', encoding='utf-8'))['records']
j1 = json.load(open(base + 'judge3_route.json', encoding='utf-8'))
correct = {}
for k, v in j1.items():
    correct[k.split('\\')[-1]] = set(v['correct_ids'])
c1 = correct.get('r5_route_50.json', set())
c2 = correct.get('r5_route2_50.json', set())
m1 = {r['question_id']: r for r in r1}
m2 = {r['question_id']: r for r in r2}
print('c1:', len(c1), 'c2:', len(c2))
print('route2 gained:', len(c2 - c1))
for qid in list(c2 - c1)[:8]:
    print('  +', qid, '|', m1[qid]['question_type'], '| route:', m1[qid]['answer'][:70], '|| route2:', m2[qid]['answer'][:70])
print('route2 regressed:', len(c1 - c2))
for qid in list(c1 - c2)[:8]:
    print('  -', qid, '|', m1[qid]['question_type'], '| route:', m1[qid]['answer'][:70], '|| route2:', m2[qid]['answer'][:70])
