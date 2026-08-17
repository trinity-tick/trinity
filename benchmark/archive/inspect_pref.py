# -*- coding: utf-8 -*-
"""inspect_pref: show correct ids + answer samples across pref configs."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
files = [
    ('baseline_inner2', 'ab_pref_judged.json'),
    ('baseline_noinner2', 'ab_pref_baseline_noinner2_judged.json'),
    ('pref3_inner2', 'ab_pref_pref3_judged.json'),
    ('pref3_noinner2', 'ab_pref_pref3_noinner2_judged.json'),
]
seen = {}
for name, f in files:
    rep = json.load(open(base + f, encoding='utf-8'))
    for k, v in rep.items():
        print(name, '| acc=' + str(v['accuracy']), '| n=' + str(v['n']), '| correct=' + str(len(v['correct_ids'])), ':', sorted(v['correct_ids'])[:12])
