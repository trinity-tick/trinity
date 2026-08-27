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


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
