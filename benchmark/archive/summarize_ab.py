# -*- coding: utf-8 -*-
"""summarize_ab: print all A/B judged files compactly."""
import json, os

base = "C:/Users/Administrator/.trinity/bench-official/"
for f in ['ab_temporal_judged.json', 'ab_multi_judged.json', 'ab_pref_judged.json']:
    print('=== ' + f + ' ===')
    rep = json.load(open(base + f, encoding='utf-8'))
    for k, v in rep.items():
        name = os.path.basename(k).replace('ab_', '').replace('.json', '')
        print('  ', name, '| acc=' + str(v['accuracy']) + ' | n=' + str(v['n']) + ' | by=' + json.dumps(v['by_type']))
