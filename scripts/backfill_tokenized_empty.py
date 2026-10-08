# -*- coding: utf-8 -*-
"""backfill_tokenized_empty.py —— 为「**content 是密文、`tokenized_content` 为空**」的行回填**明文分词**。

为什么需要（**缺陷**，t137/G10R5 已定性）：
  FTS 触发器取 `COALESCE(new.tokenized_content, new.content)`（`sqlite/_schema.py:499-500`）；
  当 `tokenized_content` 为空且 `content` 是**密文**时，FTS 索引里落的是**密文** ⇒ ⭐ **该行内容检索不可见**
  （实测 18 条 active `perception` 行）。

口径（**与常规写入路径一致**）：`_crypto.py:85 _tokenized_for_storage(plain, tokenized)` 在加密模式下
对"无 CJK 分词结果"的内容**回退写明文** ⇒ 本脚本同样写**明文**，因此**只能吃【解密后的明文】**：
  ⚠️ 隐私前提（t137 已取证）：调用方**必须传【已掩码】文本**。本脚本处理的是**已在库中**的行，
     其 `content` 密文解密出来的就是**入库当时已掩码**的文本（掩码发生在更早的写入路径上）
     ⇒ ⭐ **不去掩码、不新增 PII 面**（只是把已有的那份明文的分词写进影子列）。

用法
  python scripts/backfill_tokenized_empty.py --db <sqlite 路径>            # dry-run（默认）
  python scripts/backfill_tokenized_empty.py --db <sqlite 路径> --apply    # 真写（**切勿指向生产**）
  # 可选：--limit N / --json-out <文件> / --scope cipher-empty|all-empty

安全设计
  · `--db` **必填**（**没有默认值** ⇒ 不可能误指生产）；
  · `--apply` **默认关** ⇒ 不写任何东西；
  · ⭐ **fail-open 防护（t156）**：解密后**仍是 `enc:v1:` 前缀**（或解密抛错）⇒ **跳过 + 计数**，**绝不写入密文**；
  · ⭐ **只 UPDATE `tokenized_content` 一列**（**不改 `content`/不改 hash/不碰 `created_at`**）；
  · 每条 UPDATE 在**一个 `BEGIN IMMEDIATE` 事务**里，失败即回滚；
  · ⚠️ **不在循环内切换 `query_only`**（v1 曾如此做 ⇒ 只写 1 行就再也写不动；已修）；
  · 全程打印三态：`would_update / skipped_decrypt_failed / errors`。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

CIPHER_PREFIX = "enc:v1:"


def _tokenize_plain(plain: str):
    """与常规路径同源的"明文分词"：优先用适配器（含 jieba 与 `_tokenized_for_storage` 语义），否则退化。

    返回值: (将写入 tokenized_content 的字符串, 使用的口径名)
    """
    try:
        from trinity.adapters.sqlite import SQLiteAdapter as _SQLA  # type: ignore
        tok = _SQLA._tokenize_content_for_fts(plain)
        val = _SQLA._tokenized_for_storage(plain, tok)
        return (val, "adapter._tokenized_for_storage")
    except Exception:  # noqa: BLE001 —— 退化路径必须显式标注口径
        try:
            import jieba  # type: ignore
            joined = " ".join(w for w in jieba.cut(plain) if w.strip())
            return (joined or plain, "jieba.cut")
        except Exception:  # noqa: BLE001
            return (plain, "plain-fallback")


def collect_candidates(con: sqlite3.Connection, scope: str, limit=None):
    """候选行：`content` 为密文 且 `tokenized_content` 为空（`all-empty` 另含明文行）。"""
    if scope == "cipher-empty":
        where = "coalesce(tokenized_content,'')='' AND content LIKE ?"
        args = (CIPHER_PREFIX + "%",)
    else:  # all-empty
        where = "coalesce(tokenized_content,'')=''"
        args = ()
    sql = ("SELECT rowid, memory_id, content, status, category, agent_id, created_at "
           "FROM memories WHERE " + where + " ORDER BY rowid")
    if limit:
        sql += " LIMIT %d" % int(limit)
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def run(db: str, apply: bool = False, scope: str = "cipher-empty", limit=None, json_out=None):
    from trinity.security.crypto import decrypt_content

    rep = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "db": db, "apply": bool(apply),
           "scope": scope, "would_update": 0, "updated": 0, "skipped_decrypt_failed": 0,
           "errors": 0, "caliber_counts": {}, "rows": []}
    if not os.path.exists(db):
        raise SystemExit("库不存在：%s" % db)
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA query_only=%d" % (0 if apply else 1))
    try:
        rows = collect_candidates(con, scope, limit)
        for r in rows:
            raw = r["content"] or ""
            try:
                plain = decrypt_content(raw) if raw.startswith(CIPHER_PREFIX) else raw
            except Exception as e:  # noqa: BLE001
                rep["errors"] += 1
                rep["rows"].append({"memory_id": r["memory_id"], "state": "error",
                                    "err": "%s: %s" % (type(e).__name__, str(e)[:80])})
                continue
            # ⭐ fail-open（t156）：解密后仍是密文 ⇒ 跳过，绝不把密文写进影子列
            if str(plain).startswith(CIPHER_PREFIX):
                rep["skipped_decrypt_failed"] += 1
                rep["rows"].append({"memory_id": r["memory_id"], "state": "skipped_decrypt_failed"})
                continue
            val, caliber = _tokenize_plain(str(plain))
            rep["caliber_counts"][caliber] = rep["caliber_counts"].get(caliber, 0) + 1
            if not val or str(val).startswith(CIPHER_PREFIX):
                rep["skipped_decrypt_failed"] += 1
                rep["rows"].append({"memory_id": r["memory_id"], "state": "skipped_decrypt_failed",
                                    "why": "tokenize 结果为空或仍是密文"})
                continue
            rep["would_update"] += 1
            if apply:
                try:
                    con.execute("BEGIN IMMEDIATE")
                    cur = con.execute("UPDATE memories SET tokenized_content=? WHERE rowid=?",
                                      (val, r["rowid"]))
                    if cur.rowcount != 1:
                        raise RuntimeError("rowcount=%s" % cur.rowcount)
                    con.commit()
                    rep["updated"] += 1
                except Exception as e:  # noqa: BLE001
                    con.rollback()
                    rep["errors"] += 1
                    rep["rows"].append({"memory_id": r["memory_id"], "state": "error",
                                        "err": "%s: %s" % (type(e).__name__, str(e)[:80])})
    finally:
        con.close()
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
    print("[backfill-tokenized] db=%s apply=%s scope=%s" % (db, apply, scope))
    print("  would_update=%d  updated=%d  skipped_decrypt_failed=%d  errors=%d"
          % (rep["would_update"], rep["updated"], rep["skipped_decrypt_failed"], rep["errors"]))
    print("  分词口径计数：%s" % rep["caliber_counts"])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填「密文 content + 空 tokenized_content」行的明文分词（默认 dry-run）")
    ap.add_argument("--db", required=True, help="**必填** SQLite 路径（没有默认值，防止误指生产库）")
    ap.add_argument("--apply", action="store_true", help="真写（默认关）")
    ap.add_argument("--scope", choices=("cipher-empty", "all-empty"), default="cipher-empty")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)
    run(a.db, apply=a.apply, scope=a.scope, limit=a.limit, json_out=a.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
