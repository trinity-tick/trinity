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
import logging
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

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
            except Exception as _e:
                swallow(__name__, _e)  # 解密失败原样保留（密钥不匹配等）
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

# ── t69/I9：**写入形态同口径** + 等价性断言 ─────────────────────────────
# 症状（t63 实测）：幂等判定用 `sha256(原文)`，而守卫在 `_crud.py:163-165` 已把 content
# 换成掩码形（去重查询 `:210` 按**掩码后** hash）⇒ 判定整体失效：默认真守下同一条被
# 反复"导入"，`TRINITY_ADAPTER_GUARD=0` 时才正常。
# 修法（队长裁定）：**先算出"将被写入的形态"，再对它取 hash** —— 不改成"不掩"。
# 口径唯一来源 = `trinity.adapters._pii_guard`（本文件不复制任何掩码策略）。
def _stored_form(content: str, metadata=None):
    """返回 `(将被落库的正文, 守卫判定)`。"""
    try:
        from trinity.adapters._pii_guard import adapter_pii_guard
    except Exception as _e:                    # 策略层不可用 ⇒ 退化到"原文"（= 改动前行为）
        return content, {"available": False, "refuse": False, "isolate": False, "error": repr(_e)}
    stored, _md, info = adapter_pii_guard(content, dict(metadata or {}))   # 副本探测（见 t63）
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
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/memory_portability.py::_assert_stored_matches")
    row = adapter._conn.execute(
        "SELECT content_hash FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    got = row["content_hash"] if row is not None else None
    if got is None and isinstance(result, dict):
        got = result.get("content_hash") or result.get("sha256_hash")
    exp = _hash(expected_stored)
    assert got == exp, (
        "等价性断言失败：落库形态 != 将被落库的形态（memory_id=%s）"
        "—— 守卫/写入路径的形态口径发生漂移（t69/I9）" % memory_id)


def import_memories(items: List[Dict[str, Any]], db_path: str,
                    persona_id: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]:
    """导入标准格式记忆到 Trinity（幂等：与**存储形态**同口径的 persona+agent+content_hash）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = refused = equivalence_checked = 0
    try:
        for it in items:
            content = it.get("content", "")
            if not content:
                continue
            p = it.get("persona_id") or persona_id or "default"
            a = it.get("agent_id") or agent_id or "default"
            md = dict(it.get("metadata") or {})
            stored_expected, _info = _stored_form(content, md)
            if _info.get("refuse"):
                refused += 1                     # high 档：不落库，如实记账
                continue
            chash = _hash(stored_expected)        # ← 同口径（不是原文 hash）
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
            _res = adapter.store_memory(
                content=content,
                persona_id=p,
                agent_id=a,
                role=it.get("role", "user"),
                importance=float(it.get("importance", 0.5)),
                tags=it.get("tags") or [],
                category=it.get("category", "general"),
                modality=it.get("modality", "text"),
                metadata=md,
                source_uri=it.get("source_uri"),
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
    return {"imported": imported, "skipped": skipped, "refused": refused,
            "equivalence_checked": equivalence_checked}


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
