# -*- coding: utf-8 -*-
"""forgetting_score.py — 自动遗忘决策（2026-08-27 方向A）。

每记忆遗忘分（0-1）：使用频率低 + 时间久未访问 + importance 低 + 冲突已解决 → 高分。
--apply: 归档 score>0.9 且 importance<0.3 的记忆（保守）。默认只报告。
"""
import os, sys, argparse, time, json
_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def forgetting_score(rec, now):
    """0-1 遗忘倾向。权重：未访问时长 0.4 / 访问频率 0.3 / importance 0.2 / 冲突 0.1。"""
    try:
        last = str(rec.get("last_accessed_at") or rec.get("created_at") or "")
        import datetime
        try:
            ts = datetime.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")) if len(last) >= 19 else now
        idle_days = max(0.0, (now - ts) / 86400.0)
    except Exception:
        idle_days = 999.0
    s_idle = min(1.0, idle_days / 90.0)
    acc = float(rec.get("access_count") or 0)
    s_freq = min(1.0, max(0.0, 1.0 - acc / 20.0))
    imp = float(rec.get("importance") or rec.get("importance_score") or 0.5)
    s_imp = max(0.0, 1.0 - imp / 0.6)  # importance >= 0.6 基本不遗忘
    conflict = 0.0
    if rec.get("is_resolved"):
        conflict = 0.1
    if rec.get("conflict_group_id") and not rec.get("is_resolved"):
        conflict = 0.0
    score = 0.4 * s_idle + 0.3 * s_freq + 0.2 * s_imp + 0.1 * conflict
    return round(min(1.0, score), 3), {"idle_days": round(idle_days, 1), "access": int(acc),
                                        "importance": round(imp, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-score", type=float, default=0.9, help="归档分数下限（阶段2 可下调）")
    ap.add_argument("--max-importance", type=float, default=0.3, help="归档重要度上限")
    ap.add_argument("--apply", action="store_true", help="归档 score>min-score 且 importance<max-importance")
    args = ap.parse_args()
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    rows = mem._adapter.get_all_memories(limit=3000, offset=0)
    now = time.time()
    scored = []
    for r in rows:
        if r.get("status") != "active":
            continue
        s, info = forgetting_score(r, now)
        scored.append((s, r, info))
    scored.sort(key=lambda x: -x[0])
    top = scored[: args.limit]
    print(f"scored {len(scored)} memories | TOP {len(top)} 遗忘候选:")
    for s, r, info in top[: 10]:
        print(f"  {s:.2f} {info} {str(r.get('content') or '')[:40]}")
    if args.apply:
        # 2026-08-27（资产化应用）：高价值记忆豁免——value>=0.7 永不归档
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("mval", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memory_value.py"))
        _mval = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mval)
        arch = [r for s, r, info in scored
                if s > args.min_score
                and float(r.get("importance") or 0) < args.max_importance
                and _mval.value_score(r, time.time())[0] < 0.7]
        if arch:
            mem._adapter.archive_memories([r.get("memory_id") for r in arch])
            print(f"archived {len(arch)} low-value memories")
        else:
            print("nothing to archive (score>0.9 & importance<0.3)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
