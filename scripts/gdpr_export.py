#!/usr/bin/env python3
"""
Trinity — GDPR 一键出境/删除（2026-08-15）
============================================
对 SQLite 运行时大库执行合规操作：

  python scripts/gdpr_export.py --persona alice --out-dir .trinity/gdpr   # 导出
  python scripts/gdpr_export.py --persona alice --delete                  # 删除（含审计保留）
  python scripts/gdpr_export.py --dry-run                                # 预览

依赖 adapter 的 export_user_data / forget_user（DCSA 审计链保留软删记录）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity GDPR export/forget")
    parser.add_argument("--persona", required=True, help="目标 persona_id")
    parser.add_argument("--delete", action="store_true", help="删除该 persona 数据（软删+审计）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不执行")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--out-dir", default=os.path.expanduser("~/.trinity/gdpr"))
    args = parser.parse_args()

    from trinity.adapters.sqlite import SQLiteAdapter

    adapter = SQLiteAdapter(db_path=args.sqlite_path)
    adapter.connect()
    try:
        if args.dry_run:
            stats = adapter.get_persona_memories(persona_id=args.persona, limit=10000)
            print(json.dumps({
                "persona": args.persona, "dry_run": True,
                "memories_count": len(stats),
                "action": "delete" if args.delete else "export",
            }, indent=2, ensure_ascii=False))
            return 0

        if args.delete:
            result = adapter.forget_user(persona_id=args.persona)
            print(json.dumps({"persona": args.persona, "delete": result}, indent=2, ensure_ascii=False, default=str))
            return 0

        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        exported = adapter.export_user_data(persona_id=args.persona)
        if not exported:
            print(json.dumps({"persona": args.persona, "export": "no data", "path": str(out)}))
            return 0
        dest = out / f"{args.persona}_export_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        dest.write_text(exported, encoding="utf-8")
        print(json.dumps({"persona": args.persona, "export": "ok", "path": str(dest)}, indent=2))
        return 0
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    sys.exit(main())
