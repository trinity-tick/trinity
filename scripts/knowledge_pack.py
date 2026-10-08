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
    """按 category/tags 筛选记忆打包为知识包。

    2026-08-24（R8 P1-5 配套）：存储加密默认开启后 content 列可能为密文
    （enc:v1: 前缀）——知识包导出必须先解密再脱敏，否则 PII 脱敏与内容
    可读性均失效。解密失败/无密钥时原样保留。
    """
    from trinity.security.crypto import get_storage_cipher
    # 2026 优化轮 B6：本函数调用 `sign_pack(pack)`（下方写文件处）与 `key_source()`，
    # 但导入原本只写在**另一个函数** `unpack_pack` 里（函数内导入只绑该函数局部名）
    # ⇒ 本函数**必抛 NameError**，且抛在 `Path(out).write_text(...)` 之前
    # ⇒ **知识包根本不会落盘**，A10 的"HMAC 签名"实际从未生效（4 个用例长期红）。
    # 判据与同类缺陷见 GUARDS.md G9；回归守卫见
    # tests/unit/test_b6_commit_boundary.py::TestUndefinedGlobalGuard。
    from trinity.security.pack_signing import key_source, sign_pack
    cipher = get_storage_cipher()  # 默认 on；显式 off 时 None
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
        # 密文 → 明文（存储加密兼容，脱敏前必须解密）
        if cipher is not None and isinstance(content, str) and content.startswith("enc:v1:"):
            try:
                content = cipher.decrypt(content)
            except Exception as _e:
                swallow(__name__, _e)
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
    Path(out).write_text(json.dumps(sign_pack(pack), ensure_ascii=False, indent=1), encoding="utf-8")
    return {"items": len(items), "path": out, "title": pack["title"],
            "signed": True, "key_source": key_source()}


# ── t69/I9：**写入形态同口径** + 等价性断言 ─────────────────────────────
# 症状（t63 实测）：本脚本原先用 sha256(**原文**) 做幂等判定，而适配器守卫在
#   `trinity/adapters/sqlite/_crud.py:163-165` 已于**落库前**把 content 换成掩码形
#   （去重查询在 `:210`，用的是**掩码后**文本的 content_hash）
#   ⇒ 本脚本的幂等判定整体失效：默认真守下同一条被反复"导入"（实测 imported=[2,1]；
#      `TRINITY_ADAPTER_GUARD=0` 时才是 [2,0]）。
# 修法（队长裁定，不采纳"给搬运脚本加不掩开关"）：**先算出"将被写入的形态"，再对它取 hash**。
# 口径的唯一来源是 `trinity.adapters._pii_guard`；本文件**不复制**任何掩码/策略正则。
def _stored_form(content: str, metadata=None):
    """返回 `(将被落库的正文, 守卫判定)`。"""
    try:
        from trinity.adapters._pii_guard import adapter_pii_guard
    except Exception as _e:                    # 策略层不可用 ⇒ 退化到"原文"（= 改动前行为）
        return content, {"available": False, "refuse": False, "isolate": False, "error": repr(_e)}
    # ⚠️ 用 metadata 的**副本**探测：守卫会往 metadata 写 `pii_redaction`；若把同一个 dict
    # 传下去，真实写入那次会被守卫当成"已掩过"而放行 ⇒ 口径又会错。
    stored, _md, info = adapter_pii_guard(content, dict(metadata or {}))
    info["available"] = True
    return stored, info


def _stored_hash(content: str, metadata=None) -> str:
    """**将被落库形态**的 sha256（与适配器 `content_hash` 同口径）。"""
    return hashlib.sha256(_stored_form(content, metadata)[0].encode()).hexdigest()


def _assert_stored_matches(adapter, memory_id: str, expected_stored: str, result=None) -> None:
    """等价性断言（t69/建议③）—— **口径写清**：

    比对的是「**落库 == 掩码/隔离后的源**」，**不是**「落库 == 原件」：
      · 含 PII 的内容会被守卫掩成 `138********` 这类形态 ⇒ **原件 ≠ 落库是预期的**；
      · 判据取 `content_hash`（适配器按**它收到的文本**算，与密文/明文无关）。

    ⚠️ **批写入是攒批提交**（`_BATCH_SIZE=100` / `_BATCH_TIMEOUT=5s`）⇒ 刚写完可能**还没落盘**，
    直接查库会**假失败**（实测过一次：判据偶发红）。所以先 `_flush_batch()`（有则调），
    查不到时退化为比对结果 dict 里的 `sha256_hash`（= 同一列的取值，已实测相等）。
    """
    try:
        _fl = getattr(adapter, "_flush_batch", None)
        if callable(_fl):
            _fl()
    except Exception:                                  # noqa: BLE001 —— flush 失败不掩盖断言
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/knowledge_pack.py::_assert_stored_matches")
    row = adapter._conn.execute(
        "SELECT content_hash FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    got = row["content_hash"] if row is not None else None
    if got is None and isinstance(result, dict):
        got = result.get("content_hash") or result.get("sha256_hash")
    exp = hashlib.sha256(expected_stored.encode()).hexdigest()
    assert got == exp, (
        "等价性断言失败：落库形态 != 将被落库的形态（memory_id=%s）"
        "—— 守卫/写入路径的形态口径发生漂移（t69/I9）" % memory_id)


def unpack_pack(db_path: str, file: str, persona_id: str,
                dry_run: bool = False) -> Dict[str, Any]:
    """知识包 → 导入目标实例（隔离到指定 persona）。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.security.pack_signing import sign_pack, verify_pack, key_source  # noqa: F401

    pack = json.loads(Path(file).read_text(encoding="utf-8"))
    # A10：完整性校验（fail-closed）。
    # 借鉴定性来源（Aivy 授权缓存）：「cache 已 HMAC 签名防篡改」。
    # 此前知识包**完全无校验** —— 一个字节被改动即无人察觉。
    # 未签名的历史包仍可导入（ok=True, signed=False），但结果里**如实标记 unsigned**，
    # 不把"没验证"说成"验证通过"。
    _v = verify_pack(pack)
    if not _v["ok"]:
        return {"imported": 0, "skipped": 0, "refused": 0, "equivalence_checked": 0,
                "pack": pack.get("title"),
                "rejected": True, "signed": _v["signed"], "reason": _v["reason"]}
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    imported = skipped = refused = equivalence_checked = 0
    try:
        for it in pack.get("items", []):
            content = it.get("content", "")
            if not content:
                continue
            _md = dict(it.get("metadata") or {})
            # 保留原有 provenance 字段（改动前是写死的两个键；这里不丢）
            _md.setdefault("pack", pack.get("title", ""))
            _md.setdefault("source_agent", it.get("source_agent"))
            # ① 先算"将被落库的形态"（同口径），再对它取 hash
            stored_expected, _info = _stored_form(content, _md)
            if _info.get("refuse"):
                # high 档 ⇒ 守卫会让这条**不落库**：如实计入 refused，不假装导入成功
                refused += 1
                continue
            chash = hashlib.sha256(stored_expected.encode()).hexdigest()
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
            _res = adapter.store_memory(
                content=content,
                persona_id=persona_id,
                agent_id=f"kb-{pack.get('category', 'import')}",
                importance=float(it.get("importance", 0.5)),
                tags=it.get("tags") or [],
                category=pack.get("category", "general"),
                metadata=_md,
            )
            _mid = (_res or {}).get("memory_id") or ""
            if not _mid:                      # 适配器侧又拒/隔离失败 ⇒ 如实记账
                refused += 1
                continue
            imported += 1
            # ② 等价性断言：落库必须等于"将被落库的形态"
            _assert_stored_matches(adapter, _mid, stored_expected, _res)
            equivalence_checked += 1
    finally:
        adapter.disconnect()
    return {"imported": imported, "skipped": skipped, "refused": refused,
            "equivalence_checked": equivalence_checked, "pack": pack.get("title")}


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
