# -*- coding: utf-8 -*-
"""inspect_pref2: read each judged file, map by its own key."""
import json, os

base = "C:/Users/Administrator/.trinity/bench-official/"
files = [
    ('baseline_inner2', 'ab_pref_judged.json'),
    ('pref3_inner2', 'ab_pref_pref3_judged.json'),
    ('baseline_noinner2', 'ab_pref_baseline_noinner2_judged.json'),
    ('pref3_noinner2', 'ab_pref_pref3_noinner2_judged.json'),
]
for name, f in files:
    rep = json.load(open(base + f, encoding='utf-8'))
    for k, v in rep.items():
        tag = os.path.basename(k).replace('ab_', '').replace('.json', '')
        print(name + ' (' + tag + ') | acc=' + str(v['accuracy']) + ' | n=' + str(v['n']) + ' | correct=' + str(len(v['correct_ids'])) + ':', sorted(v['correct_ids'])[:12])
