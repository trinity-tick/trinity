# -*- coding: utf-8 -*-
"""estimate_full: aggregate per-type accuracy from all judge3 files + route3 records,
then project full-500 weighted accuracy under the best combined config.

Official distribution (500q): assistant 56 / user 70 / pref 30 / KU 78 / multi 133 / temporal 133
"""
import json, os

base = "C:/Users/Administrator/.trinity/bench-official/"

# (label, records_file, judge3_file) - judge3 gives correct_ids; records give question_type
pairs = [
    # multi dedicated (turn granularity)
    ('multi_turn_turn', 'turn_turn.json', 'judge3_turn.json'),
    ('multi_turn_baseline', 'turn_baseline.json', 'judge3_turn.json'),
    # temporal dedicated
    ('temporal_baseline', 'ab_temporal_baseline.json', 'judge3_temporal_multi.json'),
    ('temporal_timeline', 'ab_temporal_timeline.json', 'judge3_temporal_multi.json'),
    # pref dedicated (30q full)
    ('pref_pref3_inner2', 'ab_pref_pref3.json', 'judge3_pref.json'),
    ('pref_pref3_noinner2', 'ab_pref_pref3_noinner2.json', 'judge3_pref.json'),
    ('pref_base_inner2', 'ab_pref_baseline.json', 'judge3_pref.json'),
    # mixed 50q route3
    ('route3_baseline', 'r3_baseline.json', 'judge3_r3.json'),
    ('route3_route', 'r3_route.json', 'judge3_r3.json'),
    ('route3_route_tt', 'r3_route_tt.json', 'judge3_r3.json'),
]

def load_correct(jfile):
    j = json.load(open(base + jfile, encoding='utf-8'))
    out = {}
    for k, v in j.items():
        name = k.replace('/', chr(92)).split(chr(92))[-1]
        out[name] = set(v['correct_ids'])
    return out

print('=== per-type accuracy by config (judge3) ===')
for label, rf, jf in pairs:
    try:
        recs = json.load(open(base + rf, encoding='utf-8'))['records']
        correct = load_correct(jf).get(rf, set())
        by = {}
        for r in recs:
            t = r['question_type']
            d = by.setdefault(t, [0, 0]); d[0] += 1
            if r['question_id'] in correct:
                d[1] += 1
        parts = []
        for t, d in sorted(by.items()):
            parts.append(t + '=' + str(round(d[1]/d[0], 3)) + '(' + str(d[1]) + '/' + str(d[0]) + ')')
        print('%-22s overall=%.3f | %s' % (label, len(correct)/max(1,len(recs)), ' '.join(parts)))
    except Exception as e:
        print(label, 'ERR', str(e)[:80])
