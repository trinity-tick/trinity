# -*- coding: utf-8 -*-
"""diag_con: inspect con answers vs baseline to find why con failed."""
import json

base = "C:/Users/Administrator/.trinity/bench-official/"
b = json.load(open(base + 'mc_baseline.json', encoding='utf-8'))['records']
c = json.load(open(base + 'mc_con.json', encoding='utf-8'))['records']
bm = {r['question_id']: r for r in b}
cm = {r['question_id']: r for r in c}
for qid in list(bm)[:8]:
    print('QID', qid)
    print('  EXPECTED:', bm[qid]['expected'][:100])
    print('  BASELINE:', bm[qid]['answer'][:150])
    print('  CON     :', cm[qid]['answer'][:150])
    print('---')
