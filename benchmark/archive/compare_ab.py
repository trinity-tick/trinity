# -*- coding: utf-8 -*-
"""compare_ab: show baseline vs variant answer diffs for a question type."""
import json, sys

b = json.load(open(sys.argv[1], encoding='utf-8'))['records']
p = json.load(open(sys.argv[2], encoding='utf-8'))['records']
bm = {r['question_id']: r for r in b}
pm = {r['question_id']: r for r in p}
common = [qid for qid in bm if qid in pm]
diffs = [qid for qid in common if bm[qid]['answer'] != pm[qid]['answer']]
print('common:', len(common), 'diffs:', len(diffs))
for qid in diffs[:8]:
    print('QID', qid)
    print('  BASELINE:', bm[qid]['answer'][:180])
    print('  VARIANT :', pm[qid]['answer'][:180])
    print('  EXPECTED:', bm[qid]['expected'][:180])
    print('---')
