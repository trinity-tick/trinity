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
import logging

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

# ── t69/I9：**写入形态同口径** + 等价性断言 + 冲突分支真被修好 ─────────────
# 症状（t63 实测）：
#   ① 幂等判定用 `sha256(原文)`，而守卫在 `_crud.py:163-165` 已把 content 换成掩码形
#      （去重查询 `:210` 按**掩码后** hash）⇒ 判定整体失效（默认真守下 imported=[2,1]）；
#   ② **冲突分支永不触发**：原实现先按 `content_hash=?` 查出 `cur`，紧接着判断
#      `cur["content_hash"] == chash` —— 这个条件**恒为真**（查询已把它筛住了），
#      而 docstring 里写的"同 content_hash 但 content 不同"本身就是逻辑不可能。
# 修法（队长裁定）：hash/幂等与**将被写入的形态**同口径；冲突改用**可判定的身份键**：
#   「同 `(persona_id, agent_id, source_uri)` 但 content 形态不同 ⇒ 本地与远端分叉」，
#   计数并**不覆盖本地**（决策交上层 `merge_snapshots`）。适配器 `store_memory` 不接受
#   `memory_id=`（未动 `trinity/adapters/**`），故身份键取 `source_uri`。
def _stored_form(content: str, metadata=None):
    """返回 `(将被落库的正文, 守卫判定)`；口径唯一来源 = `trinity.adapters._pii_guard`。"""
    try:
        from trinity.adapters._pii_guard import adapter_pii_guard
    except Exception as _e:                    # 策略层不可用 ⇒ 退化到"原文"（= 改动前行为）
        return content, {"available": False, "refuse": False, "isolate": False, "error": repr(_e)}
    stored, _md, info = adapter_pii_guard(content, dict(metadata or {}))   # 副本探测，见 t63
    info["available"] = True
    return stored, info


def _stored_hash(content: str, metadata=None) -> str:
    """**将被落库形态**的 sha256（与适配器 `content_hash` 同口径）。"""
    return _hash(_stored_form(content, metadata)[0])


def _assert_stored_matches(adapter, memory_id: str, expected_stored: str, result=None) -> None:
    """等价性断言 —— 比对「**落库 == 掩码/隔离后的源**」，**不是**「落库 == 原件」。

    ⚠️ 批写入攒批提交 ⇒ 先 `_flush_batch()` 再查库；查不到时退化为结果 dict 的 `sha256_hash`
    （与 `content_hash` 同值，已实测），避免**假失败**。
    """
    try:
        _fl = getattr(adapter, "_flush_batch", None)
        if callable(_fl):
            _fl()
    except Exception:                                  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/federation_sync.py::_assert_stored_matches")
    row = adapter._conn.execute(
        "SELECT content_hash FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    got = row["content_hash"] if row is not None else None
    if got is None and isinstance(result, dict):
        got = result.get("content_hash") or result.get("sha256_hash")
    exp = _hash(expected_stored)
    assert got == exp, (
        "等价性断言失败：落库形态 != 将被落库的形态（memory_id=%s）"
        "—— 守卫/写入路径的形态口径发生漂移（t69/I9）" % memory_id)


def _detect_conflict(adapter, persona_id: str, agent_id: str, source_uri, expected_hash: str):
    """**冲突判定**（t69/I9 把它提成函数，便于判据直接钉住）：

    同 `(persona_id, agent_id, source_uri)` 的本地行存在、且其 `content_hash` 与快照
    "将被落库形态"的 hash **不同** ⇒ 返回该行 `memory_id`（冲突）；否则 `None`。

    为什么不能用原来的判据：原实现按 `content_hash=?` 查出 `cur` 后再判
    `cur["content_hash"] == chash` —— **恒真**（查询已把它筛住），分支永不触发。
    """
    if not source_uri:
        return None
    row = adapter._conn.execute(
        "SELECT memory_id, content_hash FROM memories "
        "WHERE persona_id=? AND agent_id=? AND source_uri=? LIMIT 1",
        (persona_id, agent_id, source_uri)).fetchone()
    if row is not None and row["content_hash"] != expected_hash:
        return str(row["memory_id"])
    return None


def import_snapshot(db_path: str, file: str, strategy: str = "newer",
                    dry_run: bool = False) -> Dict[str, Any]:
    """导入快照到 Trinity（与**存储形态**同口径的 content_hash 幂等；冲突计数上报）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    snap = json.loads(Path(file).read_text(encoding="utf-8"))
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = conflicts = refused = equivalence_checked = 0
    conflict_ids: List[str] = []
    try:
        for m in snap.get("memories", []):
            content = m.get("content", "")
            if not content:
                continue
            md = dict(m.get("metadata") or {})
            stored_expected, _info = _stored_form(content, md)
            if _info.get("refuse"):
                refused += 1                     # high 档：守卫会让它**不落库**，如实记账
                continue
            chash = _hash(stored_expected)        # ← 同口径（不是原文 hash）
            p = m.get("persona_id", "default")
            a = m.get("agent_id", "default")
            src = m.get("source_uri")
            if src:
                # ② 冲突：同一来源身份、但本地形态与快照形态不同 ⇒ 分叉（t69/I9：**真判**）
                _cid = _detect_conflict(adapter, p, a, src, chash)
                if _cid:
                    conflicts += 1
                    if len(conflict_ids) < 20:
                        conflict_ids.append(_cid)
                    continue                     # 不覆盖本地
            cur = adapter._conn.execute(
                "SELECT memory_id, content_hash FROM memories "
                "WHERE persona_id=? AND agent_id=? AND content_hash=?",
                (p, a, chash),
            ).fetchone()
            if cur is not None:
                skipped += 1                     # 同形态 ⇒ 幂等跳过
                continue
            if dry_run:
                imported += 1
                continue
            _res = adapter.store_memory(
                content=content,
                persona_id=p,
                agent_id=a,
                role=m.get("role", "user"),
                importance=float(m.get("importance", 0.5)),
                tags=m.get("tags") or [],
                category=m.get("category", "general"),
                modality=m.get("modality", "text"),
                metadata=md,
                source_uri=src,
            )
            _mid = (_res or {}).get("memory_id") or ""
            if not _mid:
                refused += 1
                continue
            imported += 1
            _assert_stored_matches(adapter, _mid, stored_expected, _res)
            equivalence_checked += 1
    finally:
        adapter.disconnect()
    return {"imported": imported, "skipped": skipped, "conflicts": conflicts,
            "refused": refused, "equivalence_checked": equivalence_checked,
            "conflict_ids": conflict_ids, "strategy": strategy}


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
