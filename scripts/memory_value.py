# -*- coding: utf-8 -*-
"""memory_value.py — 记忆资产化（2026-08-27 方向C）。

价值分 = 访问频率0.4 + 时效0.3 + 重要性0.2 + 完整度0.1。
报告：TOP 价值记忆 + 类别价值分布 + 投资回报（高频命中记忆）。
"""
import os
import sys
import time
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def value_score(rec, now):
    try:
        acc = float(rec.get("access_count") or 0)
    except Exception:
        acc = 0.0
    s_freq = min(1.0, acc / 50.0)
    try:
        last = str(rec.get("last_accessed_at") or rec.get("created_at") or "")
        import datetime
        try:
            ts = datetime.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")) if len(last) >= 19 else now
        idle = max(0.0, (now - ts) / 86400.0)
    except Exception:
        idle = 999.0
    s_rec = min(1.0, max(0.0, 1.0 - idle / 90.0))
    try:
        imp = float(rec.get("importance") or rec.get("importance_score") or 0.5)
    except Exception:
        imp = 0.5
    s_imp = min(1.0, imp / 0.9)
    content = str(rec.get("content") or "")
    s_full = min(1.0, len(content) / 300.0)
    v = 0.4 * s_freq + 0.3 * s_rec + 0.2 * s_imp + 0.1 * s_full
    return round(min(1.0, v), 3), {"access": int(acc), "idle_days": round(idle, 1),
                                    "importance": round(imp, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    rows = mem._adapter.get_all_memories(limit=5000, offset=0)
    now = time.time()
    scored = []
    cats = {}
    for r in rows:
        if r.get("status") != "active":
            continue
        v, info = value_score(r, now)
        scored.append((v, r, info))
        c = r.get("category") or "general"
        cats[c] = cats.get(c, 0) + 1
    scored.sort(key=lambda x: -x[0])
    print(f"scored {len(scored)} active memories")
    print("TOP 价值记忆:")
    for v, r, info in scored[: args.limit]:
        print(f"  {v:.2f} {info} {str(r.get('content') or '')[:44]}")
    cat_val = {}
    for v, r, info in scored:
        c = r.get("category") or "general"
        cat_val[c] = cat_val.get(c, 0.0) + v
    print("类别价值分布 (TOP5):")
    for c, tv in sorted(cat_val.items(), key=lambda x: -x[1])[:5]:
        print(f"  {c}: {tv:.0f} (n={cats.get(c, 0)})")
    high = [r for v, r, info in scored if v >= 0.7]
    print(f"高价值记忆 (>=0.7): {len(high)} 条——优先防过期、防遗忘")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
