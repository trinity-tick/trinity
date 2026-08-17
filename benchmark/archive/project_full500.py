# -*- coding: utf-8 -*-
"""project_full500: weighted projection of full-500 QA accuracy.

Official distribution: assistant 56 / user 70 / pref 30 / KU 78 / multi 133 / temporal 133 (=500)

Best-estimate per-type accuracy (judge3, from dedicated 30-50q samples + mixed route3):
  - single-session-assistant: route3 shows 1.0 (n=3 small); dated full500 old-judge 0.911 -> judge3 est ~0.92
  - single-session-user     : route3 1.0 (n=10); dated old-judge 0.871 -> judge3 est ~0.90
  - knowledge-update        : route3 0.857-1.0 (n=7); dated old-judge 0.641 -> judge3 est ~0.85 (KU is easy for judge3)
  - temporal-reasoning      : dedicated 0.62 (n=50) with REL+inner2 == plain+inner2; best combo estimate 0.62-0.64
  - multi-session           : turn-granularity 0.52 (n=50 dedicated); route3 mixed 0.588 (n=17); best estimate 0.55-0.60
  - single-session-preference: pref3 (LLM two-stage) 0.367 no-inner2 / 0.60 inner2; inner2 harmful per earlier finding;
                               honest estimate with pref3 + no inner2: 0.40-0.45 (30q sample noisy)
"""
dist = {'single-session-assistant': 56, 'single-session-user': 70, 'single-session-preference': 30,
        'knowledge-update': 78, 'multi-session': 133, 'temporal-reasoning': 133}
total = sum(dist.values())

def project(name, low, mid, high):
    w = dist[name] / total
    return w * low, w * mid, w * high

scenarios = {
    'conservative (low)': {
        'single-session-assistant': 0.90, 'single-session-user': 0.88, 'single-session-preference': 0.35,
        'knowledge-update': 0.80, 'multi-session': 0.52, 'temporal-reasoning': 0.60},
    'best-estimate (mid)': {
        'single-session-assistant': 0.92, 'single-session-user': 0.90, 'single-session-preference': 0.42,
        'knowledge-update': 0.85, 'multi-session': 0.56, 'temporal-reasoning': 0.63},
    'optimistic (high)': {
        'single-session-assistant': 0.95, 'single-session-user': 0.93, 'single-session-preference': 0.50,
        'knowledge-update': 0.90, 'multi-session': 0.60, 'temporal-reasoning': 0.66},
}
print('official distribution (500q):', dist)
print()
for name, accs in scenarios.items():
    weighted = sum(accs[k] * dist[k] for k in accs) / total
    print('%-22s -> 全量预估 = %.1f%%' % (name, weighted * 100))
    for k, v in sorted(accs.items(), key=lambda x: -dist[x[0]]):
        print('     %-28s acc=%.2f  w=%.1f%%' % (k, v, dist[k]/total*100))
    print()
print('=== 关键杠杆 (题型 x 占比 x 可提升空间) ===')
for k in sorted(dist, key=lambda x: -dist[x]):
    print('  %-28s n=%3d 占比=%.1f%%  low=%.2f high=%.2f' % (k, dist[k], dist[k]/total*100, scenarios['conservative (low)'][k], scenarios['optimistic (high)'][k]))
