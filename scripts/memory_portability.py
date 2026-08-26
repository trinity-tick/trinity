#!/usr/bin/env python3
"""
Trinity — 记忆可迁移标准工具（2026-08-15, V2 动作 A）
========================================================
让"记忆可进可出"成为事实标准（记忆护城河的入场券）：

  - 导出：Trinity → 标准化 JSON/NDJSON（含 content/persona/agent/tags/
    category/importance/created_at/metadata/source_uri，可选全字段）
  - 导入：标准化格式 → Trinity（幂等：按 content_hash 去重）
  - 跨系统适配：Mem0 / Zep 风格 JSON 的导入转换（
    Mem0: [{memory, user_id, metadata}]
    Zep:  [{content, metadata, type}]）

用法：
    python scripts/memory_portability.py export --out memories.json
    python scripts/memory_portability.py export --out memories.ndjson --format ndjson
    python scripts/memory_portability.py import --file memories.json
    python scripts/memory_portability.py import-mem0 --file mem0_export.json --persona p1
    python scripts/memory_portability.py import-zep --file zep_export.json --persona p1
    python scripts/memory_portability.py --dry-run export ...

设计：
    - 标准格式每条约 14 个字段（核心 8 + 元数据 6），含 schema 版本
    - 导出含 schema_version + exported_at + source（可追溯）
    - 导入幂等：persona+agent+content_hash 去重（与 store_memory 一致）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

SCHEMA_VERSION = "1.0"
DEFAULT_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")

# 标准导出字段（核心 8 + 元数据）
CORE_FIELDS = ["content", "persona_id", "agent_id", "tags", "category",
               "importance", "role", "modality"]
META_FIELDS = ["memory_id", "created_at", "updated_at", "metadata",
               "source_uri", "session_id"]


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 导出 ──────────────────────────────────────────────────────────────

def export_memories(db_path: str, persona_id: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    active_only: bool = True,
                    include_all_fields: bool = False) -> List[Dict[str, Any]]:
    """从 Trinity 导出记忆为标准格式。

    2026-08-24（R8 P1-5 配套）：存储加密默认开启后，content 列可能为
    密文（enc:v1: 前缀）——导出（GDPR 数据可携权）必须输出明文，
    此处用存储密钥解密；解密失败/无密钥时原样保留（不阻断导出）。
    """
    import sqlite3
    from trinity.security.crypto import get_storage_cipher
    cipher = get_storage_cipher()  # 默认 on；显式 off 时 None
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    where = ["status = 'active'"] if active_only else []
    params: list = []
    if persona_id:
        where.append("persona_id = ?")
        params.append(persona_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    sql = "SELECT * FROM memories" + (" WHERE " + " AND ".join(where) if where else "")
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = []
    for r in rows:
        rec = {f: r[f] for f in CORE_FIELDS if f in r.keys()}
        # 密文 → 明文（存储加密兼容）
        content = rec.get("content", "")
        if cipher is not None and isinstance(content, str) and content.startswith("enc:v1:"):
            try:
                rec["content"] = cipher.decrypt(content)
            except Exception:
                pass  # 解密失败原样保留（密钥不匹配等）
        # tags 是 JSON 字符串 → 列表
        if isinstance(rec.get("tags"), str):
            try:
                rec["tags"] = json.loads(rec["tags"])
            except Exception:
                rec["tags"] = []
        # metadata 是 JSON 字符串 → dict
        if isinstance(rec.get("metadata"), str):
            try:
                rec["metadata"] = json.loads(rec["metadata"])
            except Exception:
                rec["metadata"] = {}
        if include_all_fields:
            for f in r.keys():
                if f not in rec:
                    rec[f] = r[f]
        items.append(rec)
    return items


def write_export(items: List[Dict[str, Any]], out_path: str,
                 fmt: str = "json") -> Dict[str, Any]:
    """写为标准 JSON 或 NDJSON（带 schema 头）。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": _now_iso(),
        "source": "trinity",
        "count": len(items),
        "memories": items,
    }
    out = Path(out_path)
    if fmt == "ndjson":
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"schema_version": SCHEMA_VERSION,
                                "exported_at": payload["exported_at"],
                                "source": "trinity"}) + "\n")
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    else:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"count": len(items), "path": str(out), "format": fmt}


# ── 导入 ──────────────────────────────────────────────────────────────

def import_memories(items: List[Dict[str, Any]], db_path: str,
                    persona_id: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]:
    """导入标准格式记忆到 Trinity（幂等：persona+agent+content_hash 去重）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = 0
    try:
        for it in items:
            content = it.get("content", "")
            if not content:
                continue
            p = it.get("persona_id") or persona_id or "default"
            a = it.get("agent_id") or agent_id or "default"
            chash = _hash(content)
            # 幂等检查
            cur = adapter._conn.execute(
                "SELECT memory_id FROM memories WHERE persona_id=? AND agent_id=? AND content_hash=?",
                (p, a, chash),
            ).fetchone()
            if cur:
                skipped += 1
                continue
            if dry_run:
                imported += 1
                continue
            adapter.store_memory(
                content=content,
                persona_id=p,
                agent_id=a,
                role=it.get("role", "user"),
                importance=float(it.get("importance", 0.5)),
                tags=it.get("tags") or [],
                category=it.get("category", "general"),
                modality=it.get("modality", "text"),
                metadata=it.get("metadata") or {},
                source_uri=it.get("source_uri"),
            )
            imported += 1
    finally:
        adapter.disconnect()
    return {"imported": imported, "skipped": skipped}


def load_standard_file(path: str) -> List[Dict[str, Any]]:
    """读标准 JSON 或 NDJSON。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if path.endswith(".ndjson"):
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "schema_version" in obj or "memories" in obj:
                continue  # header 行
            items.append(obj)
        return items
    data = json.loads(text)
    return data.get("memories", [])


# ── 跨系统适配 ────────────────────────────────────────────────────────

def convert_mem0(items: List[Dict[str, Any]], persona_id: str,
                 agent_id: str = "mem0-import") -> List[Dict[str, Any]]:
    """Mem0 格式 [{memory, user_id, metadata}] → 标准格式。"""
    out = []
    for it in items:
        content = it.get("memory") or it.get("content") or ""
        if not content:
            continue
        out.append({
            "content": content,
            "persona_id": it.get("user_id") or persona_id,
            "agent_id": agent_id,
            "tags": (it.get("metadata") or {}).get("tags", []),
            "category": (it.get("metadata") or {}).get("category", "general"),
            "importance": float((it.get("metadata") or {}).get("importance", 0.5)),
            "role": "user",
            "modality": "text",
            "metadata": {k: v for k, v in (it.get("metadata") or {}).items()
                         if k not in ("tags", "category", "importance")},
        })
    return out


def convert_zep(items: List[Dict[str, Any]], persona_id: str,
                agent_id: str = "zep-import") -> List[Dict[str, Any]]:
    """Zep 风格 [{content, metadata, type}] → 标准格式。"""
    out = []
    for it in items:
        content = it.get("content") or it.get("text") or ""
        if not content:
            continue
        out.append({
            "content": content,
            "persona_id": persona_id,
            "agent_id": agent_id,
            "tags": (it.get("metadata") or {}).get("tags", []),
            "category": it.get("type") or (it.get("metadata") or {}).get("category", "general"),
            "importance": float((it.get("metadata") or {}).get("importance", 0.5)),
            "role": "user",
            "modality": "text",
            "metadata": {k: v for k, v in (it.get("metadata") or {}).items()
                         if k not in ("tags", "category", "importance")},
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity memory portability")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="导出标准格式")
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--format", default="json", choices=["json", "ndjson"])
    p_exp.add_argument("--persona")
    p_exp.add_argument("--agent")
    p_exp.add_argument("--all-fields", action="store_true")
    p_exp.add_argument("--db", default=DEFAULT_DB)

    p_imp = sub.add_parser("import", help="导入标准格式")
    p_imp.add_argument("--file", required=True)
    p_imp.add_argument("--persona")
    p_imp.add_argument("--agent")
    p_imp.add_argument("--db", default=DEFAULT_DB)
    p_imp.add_argument("--dry-run", action="store_true")

    for name in ("import-mem0", "import-zep"):
        p = sub.add_parser(name, help=f"{name.split('-')[1]} 格式导入")
        p.add_argument("--file", required=True)
        p.add_argument("--persona", required=True)
        p.add_argument("--agent")
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "export":
        items = export_memories(args.db, args.persona, args.agent,
                                include_all_fields=args.all_fields)
        res = write_export(items, args.out, args.format)
        print(f"exported {res['count']} memories -> {res['path']} ({res['format']})")
        return 0

    if args.cmd == "import":
        items = load_standard_file(args.file)
        res = import_memories(items, args.db, args.persona, args.agent, args.dry_run)
        print(f"import: {res['imported']} new, {res['skipped']} dup"
              f" ({'dry-run' if args.dry_run else 'written'})")
        return 0

    if args.cmd in ("import-mem0", "import-zep"):
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("memories", [])
        converted = convert_mem0(items, args.persona, args.agent or "mem0-import") \
            if args.cmd == "import-mem0" else \
            convert_zep(items, args.persona, args.agent or "zep-import")
        res = import_memories(converted, args.db, args.persona, args.agent, args.dry_run)
        print(f"{args.cmd}: {res['imported']} new, {res['skipped']} dup"
              f" ({'dry-run' if args.dry_run else 'written'})")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
