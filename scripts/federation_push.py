# -*- coding: utf-8 -*-
"""federation_push.py — 联邦定时推送（2026-08-27 第三阶段）。

导出 decision/knowledge 包 -> push_remote 到目标实例。
用法: python scripts/federation_push.py <target_base> [--limit N]
"""
import os
import sys
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target_base", help="target instance base url (http://host:port)")
    ap.add_argument("--categories", default="decision,knowledge")
    ap.add_argument("--limit", type=int, default=10000)
    args = ap.parse_args()
    from trinity.agents.federation import Federation
    f = Federation("sqlite")
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    pack = f.export_pack(categories=cats, limit=args.limit)
    print("exported:", pack["count"])
    n = f.push_remote(args.target_base, pack, timeout=30)
    print("pushed:", n)
    return 0


def incremental_sync(target_base: str, categories=None, limit=10000) -> dict:
    """2026-08-27（一致性）：增量同步——只推 created_at > 上次 sync 的记忆。"""
    import json
    from trinity.agents.federation import Federation
    state_file = os.path.expanduser("~/.trinity/fed_sync_state.json")
    last = ""
    if os.path.exists(state_file):
        try:
            last = json.load(open(state_file, encoding="utf-8")).get("last_sync_ts", "")
        except Exception:
            last = ""
    f = Federation("sqlite")
    pack = f.export_pack(categories=categories or ["decision", "knowledge"], limit=limit)
    if last:
        pack["items"] = [it for it in pack["items"] if str(it.get("created_at") or "") > last]
        print("incremental since", last, "->", len(pack["items"]), "items")
    n = f.push_remote(target_base, pack, timeout=30)
    with open(state_file, "w", encoding="utf-8") as fp:
        json.dump({"last_sync_ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                   "last_pushed": n}, fp)
    return {"pushed": n, "total_exported": pack["count"]}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
