#!/usr/bin/env python3
"""
Trinity — 记忆市场知识包流通（2026-08-15, V2 动作 C ③）
==========================================================
把记忆打包为"可售知识包"（Knowledge Pack）并支持跨实例流通：

  - 打包：按 category/tags 筛选记忆 → 脱敏 → 知识包 JSON
    （含 title/description/category/price_hint/modalities/items）
  - 拆包：知识包 → 导入目标实例（可指定新 persona，隔离原数据）
  - 流通：知识包文件即"市场商品"，可上传市场或跨实例传输

与 TrustExchange 市场衔接：打包产物可直接 /market/estimate 估价、
/market/list 挂单（价格字段兼容）。

用法：
    python scripts/knowledge_pack.py pack --db a.db --category research --out kb_research.json
    python scripts/knowledge_pack.py pack --db a.db --tags "db,cache" --out kb_db.json --title "数据库实践"
    python scripts/knowledge_pack.py unpack --db b.db --file kb_research.json --persona imported
    python scripts/knowledge_pack.py info --file kb_research.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

DEFAULT_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
PACK_SCHEMA = "1.0"

# 脱敏：替换 PII（手机号/邮箱）避免知识包泄露敏感信息
_PII_PATTERNS = [
    (r"1[3-9]\d{9}", "[PHONE]"),
    (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[EMAIL]"),
]


def _redact(content: str) -> str:
    import re
    for pat, rep in _PII_PATTERNS:
        content = re.sub(pat, rep, content)
    return content


def pack_memories(db_path: str, out: str, category: Optional[str] = None,
                  tags: Optional[List[str]] = None, title: str = "",
                  description: str = "", price_hint: float = 0.0,
                  limit: int = 200, redact: bool = True) -> Dict[str, Any]:
    """按 category/tags 筛选记忆打包为知识包。"""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    where = ["status = 'active'"]
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if tags:
        placeholders = ",".join("?" for _ in tags)
        where.append(f"(tags LIKE ? OR tags LIKE ?)")
        # 简化：任一 tag 出现在 tags 字段即可
        tag_conds = " OR ".join(["tags LIKE ?"] * len(tags))
        where[-1] = f"({tag_conds})"
        params.extend([f"%{t}%" for t in tags])
    sql = f"SELECT memory_id, content, persona_id, agent_id, tags, category, importance FROM memories WHERE {' AND '.join(where)} LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = []
    for r in rows:
        content = r["content"]
        if redact:
            content = _redact(content)
        t = r["tags"]
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except Exception:
                t = []
        items.append({
            "content": content,
            "importance": r["importance"],
            "tags": t,
            "source_agent": r["agent_id"],
        })

    pack = {
        "pack_schema": PACK_SCHEMA,
        "title": title or (category or "memory-pack"),
        "description": description or f"Trinity 知识包：{category or 'general'}",
        "category": category or "general",
        "price_hint": price_hint,
        "item_count": len(items),
        "redacted": redact,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    Path(out).write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"items": len(items), "path": out, "title": pack["title"]}


def unpack_pack(db_path: str, file: str, persona_id: str,
                dry_run: bool = False) -> Dict[str, Any]:
    """知识包 → 导入目标实例（隔离到指定 persona）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    pack = json.loads(Path(file).read_text(encoding="utf-8"))
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = 0
    try:
        for it in pack.get("items", []):
            content = it.get("content", "")
            if not content:
                continue
            chash = hashlib.sha256(content.encode()).hexdigest()
            cur = adapter._conn.execute(
                "SELECT memory_id FROM memories WHERE persona_id=? AND content_hash=?",
                (persona_id, chash),
            ).fetchone()
            if cur:
                skipped += 1
                continue
            if dry_run:
                imported += 1
                continue
            adapter.store_memory(
                content=content,
                persona_id=persona_id,
                agent_id=f"kb-{pack.get('category', 'import')}",
                importance=float(it.get("importance", 0.5)),
                tags=it.get("tags") or [],
                category=pack.get("category", "general"),
                metadata={"pack": pack.get("title", ""), "source_agent": it.get("source_agent")},
            )
            imported += 1
    finally:
        adapter.disconnect()
    return {"imported": imported, "skipped": skipped, "pack": pack.get("title")}


def pack_info(file: str) -> Dict[str, Any]:
    pack = json.loads(Path(file).read_text(encoding="utf-8"))
    return {
        "title": pack.get("title"),
        "category": pack.get("category"),
        "item_count": pack.get("item_count"),
        "redacted": pack.get("redacted"),
        "price_hint": pack.get("price_hint"),
        "sample": (pack.get("items") or [{}])[0].get("content", "")[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity knowledge pack")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--db", default=DEFAULT_DB)
    p_pack.add_argument("--out", required=True)
    p_pack.add_argument("--category")
    p_pack.add_argument("--tags", help="逗号分隔")
    p_pack.add_argument("--title")
    p_pack.add_argument("--description")
    p_pack.add_argument("--price-hint", type=float, default=0.0)
    p_pack.add_argument("--limit", type=int, default=200)
    p_pack.add_argument("--no-redact", action="store_true")

    p_un = sub.add_parser("unpack")
    p_un.add_argument("--db", default=DEFAULT_DB)
    p_un.add_argument("--file", required=True)
    p_un.add_argument("--persona", required=True)
    p_un.add_argument("--dry-run", action="store_true")

    p_info = sub.add_parser("info")
    p_info.add_argument("--file", required=True)

    args = parser.parse_args()

    if args.cmd == "pack":
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
        res = pack_memories(args.db, args.out, args.category, tags,
                            args.title, args.description, args.price_hint,
                            args.limit, redact=not args.no_redact)
        print(f"pack: {res['items']} items -> {res['path']} (title: {res['title']})")
        print(f"  （可 /market/estimate 估价、/market/list 挂单）")
        return 0
    if args.cmd == "unpack":
        res = unpack_pack(args.db, args.file, args.persona, args.dry_run)
        print(f"unpack: {res['imported']} imported, {res['skipped']} dup"
              f" (pack: {res['pack']})")
        return 0
    if args.cmd == "info":
        info = pack_info(args.file)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
