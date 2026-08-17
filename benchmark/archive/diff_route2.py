# -*- coding: utf-8 -*-
"""diff_route2: compare r5_route vs r5_route2 answers in detail."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
r1 = json.load(open(base + 'r5_route_50.json', encoding='utf-8'))['records']
r2 = json.load(open(base + 'r5_route2_50.json', encoding='utf-8'))['records']
j1 = json.load(open(base + 'judge3_route.json', encoding='utf-8'))
correct = {}
for k, v in j1.items():
    correct[k.split('/')[-1]] = set(v['correct_ids'])
c1 = correct.get('r5_route_50.json', set())
c2 = correct.get('r5_route2_50.json', set())
m1 = {r['question_id']: r for r in r1}
m2 = {r['question_id']: r for r in r2}
print('route2 gained (route wrong, route2 right):')
for qid in c2 - c1:
    print('  ', qid, '|', m1[qid]['question_type'], '| route:', m1[qid]['answer'][:80], '| route2:', m2[qid]['answer'][:80])
print()
print('route2 regressed (route right, route2 wrong):')
for qid in c1 - c2:
    print('  ', qid, '|', m1[qid]['question_type'], '| route:', m1[qid]['answer'][:80], '| route2:', m2[qid]['answer'][:80])
