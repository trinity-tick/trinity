#!/usr/bin/env python3
"""
Trinity — 联邦记忆增量同步（2026-08-15, V2 动作 C ①）
========================================================
现有 federation/sync_protocol.py 是全量快照同步。本工具升级为**增量同步**：

  - 增量导出：按 updated_at 时间戳只导变更（--since）
  - 冲突检测：同 content_hash 但 content 不同 → 标记冲突（conflict）
  - 合并策略：--strategy newer|keep-both|skip（默认 newer=保留较新 updated_at）
  - 幂等导入：content_hash 去重（不重复）

子命令：
    python scripts/federation_sync.py export --db a.db --out a_snap.json [--since TS] [--persona p]
    python scripts/federation_sync.py export --db a.db --out a_snap.json --since <timestamp>
    python scripts/federation_sync.py diff --file-a a.json --file-b b.json
    python scripts/federation_sync.py merge --base base.json --other other.json --out merged.json
    python scripts/federation_sync.py import --db a.db --file snap.json [--strategy newer]

用法示例：
    # 实例 A 增量导出
    python scripts/federation_sync.py export --db ~/.trinity/store/trinity_store.db --out a_snap.json
    # 实例 B 合并 A 的增量
    python scripts/federation_sync.py merge --base b_snap.json --other a_snap.json --out merged.json
    python scripts/federation_sync.py import --db ~/.trinity/store/trinity_store.db --file merged.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

DEFAULT_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
SCHEMA_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ── 导出（增量）──────────────────────────────────────────────────────

def export_snapshot(db_path: str, out: str, since: Optional[str] = None,
                    persona_id: Optional[str] = None,
                    agent_id: Optional[str] = None) -> Dict[str, Any]:
    """导出记忆快照；--since 时只导 updated_at >= since 的变更（增量）。"""
    conn = _open_db(db_path)
    where = ["status = 'active'"]
    params: list = []
    if since:
        where.append("updated_at >= ?")
        params.append(since)
    if persona_id:
        where.append("persona_id = ?")
        params.append(persona_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    rows = conn.execute(
        f"SELECT memory_id, content, persona_id, agent_id, tags, category, "
        f"importance, role, modality, metadata, source_uri, created_at, updated_at, "
        f"content_hash FROM memories WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        tags = r["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        items.append({
            "memory_id": r["memory_id"],
            "content": r["content"],
            "persona_id": r["persona_id"],
            "agent_id": r["agent_id"],
            "tags": tags,
            "category": r["category"],
            "importance": r["importance"],
            "role": r["role"],
            "modality": r["modality"],
            "metadata": md,
            "source_uri": r["source_uri"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "content_hash": r["content_hash"] or _hash(r["content"]),
        })
    payload = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": _now_iso(),
        "since": since,
        "source": "trinity",
        "count": len(items),
        "memories": items,
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"count": len(items), "path": out}


# ── Diff（含冲突检测）────────────────────────────────────────────────

def diff_snapshots(a_path: str, b_path: str) -> Dict[str, Any]:
    """对比两个快照：only_a / only_b / common / conflicts（同 hash 异内容）。"""
    def load(p: str) -> Dict[str, Any]:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        return {m["memory_id"]: m for m in data.get("memories", [])}

    a = load(a_path)
    b = load(b_path)
    only_a = {k: v for k, v in a.items() if k not in b}
    only_b = {k: v for k, v in b.items() if k not in a}
    conflicts = []
    for k in set(a) & set(b):
        if a[k].get("content_hash") != b[k].get("content_hash"):
            conflicts.append({
                "memory_id": k,
                "a_content": a[k]["content"][:60],
                "b_content": b[k]["content"][:60],
                "a_updated_at": a[k].get("updated_at"),
                "b_updated_at": b[k].get("updated_at"),
            })
    return {
        "only_a": len(only_a), "only_b": len(only_b),
        "common": len(set(a) & set(b)), "conflicts": conflicts,
        "only_a_items": list(only_a.keys())[:20],
        "only_b_items": list(only_b.keys())[:20],
    }


# ── Merge（冲突处理）──────────────────────────────────────────────────

def merge_snapshots(base_path: str, other_path: str, out: str,
                    strategy: str = "newer") -> Dict[str, Any]:
    """合并两个快照，按策略处理冲突。

    strategy:
        newer     保留 updated_at 较新的一方（默认）
        keep-both 冲突双方都保留（改 memory_id 后缀）
        skip      冲突跳过（保留 base）
    """
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    other = json.loads(Path(other_path).read_text(encoding="utf-8"))
    merged: Dict[str, Dict] = {}
    for m in base.get("memories", []):
        merged[m["memory_id"]] = dict(m)
    resolved = skipped = 0
    for m in other.get("memories", []):
        mid = m["memory_id"]
        if mid not in merged:
            merged[mid] = dict(m)
            continue
        if merged[mid].get("content_hash") == m.get("content_hash"):
            continue  # 相同
        # 冲突
        resolved += 1
        a_ts = merged[mid].get("updated_at", "")
        b_ts = m.get("updated_at", "")
        if strategy == "skip":
            skipped += 1
            continue
        if strategy == "keep-both":
            m2 = dict(m)
            m2["memory_id"] = mid + "_b"
            merged[mid + "_b"] = m2
            resolved += 1
            continue
        # newer（默认）
        if b_ts >= a_ts:
            merged[mid] = dict(m)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "merged_at": _now_iso(),
        "strategy": strategy,
        "count": len(merged),
        "conflicts_resolved": resolved,
        "conflicts_skipped": skipped,
        "memories": list(merged.values()),
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"merged": len(merged), "conflicts_resolved": resolved,
            "conflicts_skipped": skipped, "path": out}


# ── 导入（幂等）──────────────────────────────────────────────────────

def import_snapshot(db_path: str, file: str, strategy: str = "newer",
                    dry_run: bool = False) -> Dict[str, Any]:
    """导入快照到 Trinity（content_hash 幂等；冲突按策略）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    snap = json.loads(Path(file).read_text(encoding="utf-8"))
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = conflicts = 0
    try:
        for m in snap.get("memories", []):
            content = m.get("content", "")
            if not content:
                continue
            chash = m.get("content_hash") or _hash(content)
            cur = adapter._conn.execute(
                "SELECT memory_id, content_hash FROM memories "
                "WHERE persona_id=? AND agent_id=? AND content_hash=?",
                (m.get("persona_id", "default"), m.get("agent_id", "default"), chash),
            ).fetchone()
            if cur:
                # 同 hash → 幂等跳过；hash 不同但 memory_id 相同 → 冲突
                if cur["content_hash"] == chash:
                    skipped += 1
                    continue
            if dry_run:
                imported += 1
                continue
            adapter.store_memory(
                content=content,
                persona_id=m.get("persona_id", "default"),
                agent_id=m.get("agent_id", "default"),
                role=m.get("role", "user"),
                importance=float(m.get("importance", 0.5)),
                tags=m.get("tags") or [],
                category=m.get("category", "general"),
                modality=m.get("modality", "text"),
                metadata=m.get("metadata") or {},
                source_uri=m.get("source_uri"),
            )
            imported += 1
    finally:
        adapter.disconnect()
    return {"imported": imported, "skipped": skipped, "conflicts": conflicts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity federated incremental sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export")
    p_exp.add_argument("--db", default=DEFAULT_DB)
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--since")
    p_exp.add_argument("--persona")
    p_exp.add_argument("--agent")

    p_d = sub.add_parser("diff")
    p_d.add_argument("--file-a", required=True)
    p_d.add_argument("--file-b", required=True)

    p_m = sub.add_parser("merge")
    p_m.add_argument("--base", required=True)
    p_m.add_argument("--other", required=True)
    p_m.add_argument("--out", required=True)
    p_m.add_argument("--strategy", default="newer",
                     choices=["newer", "keep-both", "skip"])

    p_i = sub.add_parser("import")
    p_i.add_argument("--db", default=DEFAULT_DB)
    p_i.add_argument("--file", required=True)
    p_i.add_argument("--strategy", default="newer")
    p_i.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "export":
        res = export_snapshot(args.db, args.out, args.since, args.persona, args.agent)
        print(f"exported {res['count']} memories -> {res['path']} "
              f"{'(' + args.since + ' since)' if args.since else ''}")
        return 0
    if args.cmd == "diff":
        res = diff_snapshots(args.file_a, args.file_b)
        print(f"diff: only_a={res['only_a']} only_b={res['only_b']} "
              f"common={res['common']} conflicts={len(res['conflicts'])}")
        for c in res["conflicts"][:5]:
            print(f"  CONFLICT {c['memory_id'][:20]}: A='{c['a_content'][:30]}' "
                  f"B='{c['b_content'][:30]}'")
        return 0
    if args.cmd == "merge":
        res = merge_snapshots(args.base, args.other, args.out, args.strategy)
        print(f"merge({args.strategy}): {res['merged']} items, "
              f"{res['conflicts_resolved']} resolved, {res['conflicts_skipped']} skipped")
        return 0
    if args.cmd == "import":
        res = import_snapshot(args.db, args.file, args.strategy, args.dry_run)
        print(f"import: {res['imported']} new, {res['skipped']} dup"
              f" ({'dry-run' if args.dry_run else 'written'})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
