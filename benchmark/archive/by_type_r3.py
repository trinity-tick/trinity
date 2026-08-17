# -*- coding: utf-8 -*-
"""by_type_r3: show per-type accuracy for route3 configs."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
j = json.load(open(base + 'judge3_r3.json', encoding='utf-8'))
# need by-type; judge3 doesn't save by_type - recompute from records + correct_ids
recs_files = {'baseline': 'r3_baseline.json', 'route': 'r3_route.json', 'route_tt': 'r3_route_tt.json'}
correct = {}
for k, v in j.items():
    name = k.split('\\')[-1].replace('.json', '')
    correct[name] = set(v['correct_ids'])
for label, f in recs_files.items():
    recs = json.load(open(base + f, encoding='utf-8'))['records']
    c = correct.get(f, set())
    by = {}
    for r in recs:
        t = r['question_type']
        d = by.setdefault(t, [0, 0])
        d[0] += 1
        if r['question_id'] in c:
            d[1] += 1
    print(label, '| overall:', str(len(c)) + '/' + str(len(recs)))
    for t, d in sorted(by.items(), key=lambda x: -x[1][0]):
        print('   ', t, str(d[1]) + '/' + str(d[0]), '=', str(round(d[1]/d[0], 3)))
