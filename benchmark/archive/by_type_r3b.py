# -*- coding: utf-8 -*-
"""by_type_r3b: per-type accuracy using judge3 correct ids with proper path handling."""
import json, os

base = "C:/Users/Administrator/.trinity/bench-official/"
j = json.load(open(base + 'judge3_r3.json', encoding='utf-8'))
recs_files = {'baseline': 'r3_baseline.json', 'route': 'r3_route.json', 'route_tt': 'r3_route_tt.json'}
for label, f in recs_files.items():
    full = os.path.join(base, f)
    v = j.get(full) or j.get(full.replace('/', chr(92))) or j.get(f)
    if not v:
        print(label, 'NOT FOUND in judge3; keys:', list(j.keys()))
        continue
    c = set(v['correct_ids'])
    recs = json.load(open(full, encoding='utf-8'))['records']
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
