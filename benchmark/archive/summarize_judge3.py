# -*- coding: utf-8 -*-
"""summarize_judge3: compare old judge vs judge3 across all configs."""
import json, os

base = "C:/Users/Administrator/.trinity/bench-official/"
j3_files = {
    'temporal': 'judge3_temporal_multi.json',
    'pref': 'judge3_pref.json',
    'multi_extract': 'judge3_multi_extract.json',
}
for grp, f in j3_files.items():
    print('=== ' + grp + ' ===')
    rep = json.load(open(base + f, encoding='utf-8'))
    for k, v in rep.items():
        name = os.path.basename(k).replace('.json', '')
        print('  ' + name + ' | maj=' + str(v['majority_acc']) + ' | per_vote=' + str(v['per_vote_acc']) + ' | stable3/3=' + str(v['stability_3of3']))
