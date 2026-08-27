# -*- coding: utf-8 -*-
"""knowledge_produce.py v2 (no literal newline)."""
import os
import sys
import argparse
import datetime

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
NL = chr(10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    rows = mem._adapter.get_all_memories(limit=5000, offset=0)
    since = (datetime.datetime.now(datetime.timezone.utc) -
             datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    picked = []
    for r in rows:
        if r.get("status") != "active":
            continue
        cat = r.get("category") or ""
        if cat not in ("decision", "knowledge", "kb-section", "summary"):
            continue
        if str(r.get("created_at") or "") < since:
            continue
        picked.append(r)
    groups = {}
    for r in picked:
        groups.setdefault(r.get("category") or "other", []).append(r)
    today = datetime.date.today().isoformat()
    L = []
    A = L.append
    A("# Trinity 知识周报（" + today + "）")
    A("")
    A("> 自动生成：高价值决策/知识/总结记忆聚合（近 " + str(args.days) + " 天）")
    A("")
    for cat in sorted(groups):
        items = sorted(groups[cat], key=lambda x: str(x.get("created_at") or ""), reverse=True)[: args.limit]
        A("## " + cat + "（" + str(len(items)) + " 条）")
        A("")
        for r in items:
            c = str(r.get("content") or "").replace(NL, " ")[:160]
            A("- " + c)
        A("")
    out = os.path.join(_TRINITY_ROOT, "docs", "KNOWLEDGE_WEEKLY_" + today + ".md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(NL.join(L))
    print("produced:", out, "| total:", len(picked))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
