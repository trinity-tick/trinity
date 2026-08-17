# -*- coding: utf-8 -*-
"""sensitivity: how the full-500 projection moves with the two big buckets."""
dist = {'single-session-assistant': 56, 'single-session-user': 70, 'single-session-preference': 30,
        'knowledge-update': 78, 'multi-session': 133, 'temporal-reasoning': 133}
total = 500

base = {'single-session-assistant': 0.92, 'single-session-user': 0.90, 'single-session-preference': 0.42,
        'knowledge-update': 0.85, 'multi-session': 0.56, 'temporal-reasoning': 0.63}
def calc(accs):
    return sum(accs[k] * dist[k] for k in accs) / total * 100

print('mid baseline:', round(calc(base), 1), '%')
print()
# sensitivity: multi and temporal each hold ~26.6% weight
for label, delta in [('multi +-0.05', {'multi-session': base['multi-session'] + 0.05}), ('multi -0.05', {'multi-session': base['multi-session'] - 0.05}),
                     ('temporal +-0.05', {'temporal-reasoning': base['temporal-reasoning'] + 0.05}), ('temporal -0.05', {'temporal-reasoning': base['temporal-reasoning'] - 0.05})]:
    accs = dict(base); accs.update(delta)
    print('  %s -> %.1f%%' % (label, calc(accs)))
print()
print('pref sensitivity (6% weight):')
for p in [0.30, 0.42, 0.60]:
    accs = dict(base); accs['single-session-preference'] = p
    print('  pref=%.2f -> %.1f%%' % (p, calc(accs)))
print()
print('note: pref 30q sample noise is large (+-10pp on the type = +-0.6pp overall)')
print('multi turn-granularity 50q dedicated = 0.52; route3 mixed = 0.588; gap = different samples')
