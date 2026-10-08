#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""content_hash / sha256_hash 存量回填（Step 1）。

## 为什么重写这个工具（而不是新写一个）

原版 `scripts/backfill_content_hash.py` 存在三个硬缺陷，导致债一直没清：

1. **写死了已废弃的库路径** `…\\.trinity\\store\\trinity_store.db`。
   2026-10-02 灾难恢复后，常驻 API 服务的是 **`…\\.trinity\\store-restored\\trinity_store.db`**
   （见 `dsh-ops/trinity-autostart.ps1:35`）。⇒ 原版跑起来改的是**死库**，真实缺口一条没动。
2. 没有 dry-run、没有备份：直接开写。
3. 只写 `content_hash`，不写 `sha256_hash` ⇒ 两个派生列长期不一致。

## 缺口是什么（2026-10-05 实测）

库内 **897 行** `content_hash IS NULL`（active 886 / archived 10 / merged 1），且其中
**896 行连 `sha256_hash` 也是空串**。全部是 `summ_auto_*` / `exp_*` 这类自动会话摘要
（category `session` 610 / `procedural` 286），**content 为明文**。

后果**不是检索**（实测这 886 行 **886/886 都在 `memories_fts` 里，可被 FTS 搜到**），而是：
  · 无法参与 `(persona_id, agent_id, content_hash)` 幂等去重；
  · 无法做 provenance 复算（`/audit/receipt`）。

**根因已同时修在两个写入方**（本次一并改）：
`trinity/engine_worker.py` 的 `summ_auto_*` INSERT、`scripts/auto_session_summary.py`
的 `exp_*` / `summ_auto_*` INSERT —— 原先都是 `sha256_hash` 传空串且**没有 content_hash 列**。
本工具负责**存量**。

## 与既有实现的关系（§1050：不另立一份）

`pg_content_hash_and_dedup.py::--backfill-hash` 是 **PG 侧**同语义实现（撞唯一索引则跳过）。
本工具是 **SQLite 侧**对应物，**沿用它的语义**（撞索引跳过并保持 NULL、显式报 skipped），
只补上它缺的：活库路径解析、dry-run、备份、双列同写。

## 纪律
- 默认 **dry-run**；写库需 `--apply`，且**先备份**（§16.1），备份**限代数**。
- **§13.1**：本工具只需 sha256(明文)。已实测这些行的 content **全为明文**；
  执行前仍会**抽样断言不以 `enc:v1:` 开头**（取不到明文就计数上报，不静默跳过）。
- **§13.2**：失败原因分开计数，不合并成 `skipped`。
- 产物落 `output/`（§12）。

用法：
    python scripts/backfill_content_hash.py                 # 只读体检（默认）
    python scripts/backfill_content_hash.py --apply         # 回填（先备份）
    python scripts/backfill_content_hash.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/backfill_content_hash.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 常驻 API 真正服务的库（`dsh-ops/trinity-autostart.ps1:35` 设 TRINITY_STORE）
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")
#: 灾难恢复前的旧路径 —— 保留只为**显式告警**（原版工具写死了它）
DEAD_STORE = os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")
BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".trinity", "store-backups")
BACKUP_PREFIX = "trinity_store.db.pre-hashbackfill-"
BACKUP_KEEP = 2


def resolve_store(explicit: str = "") -> "tuple[str, str]":
    """库路径解析：--store > TRINITY_STORE > 引擎 canonical 解析 > 字面默认。

    ⚠ `TRINITY_STORE` 的语义是 **store 目录**，不是库文件
    （`trinity/core/client/_helpers.py:22` `_find_trinity_store`；
    `_constructure.py` 同族写法见 `_construction.py:162-165`：是文件就用文件、
    是目录就拼 `trinity_store.db`）。
    2026-10-05 实测代价：初版直接把 env 当文件路径 ⇒
    `OperationalError: unable to open database file`。
    """
    if explicit:
        return explicit, "cli"
    env = os.environ.get("TRINITY_STORE")
    if env:
        if os.path.isfile(env):
            return env, "env:TRINITY_STORE(file)"
        return os.path.join(env, "trinity_store.db"), "env:TRINITY_STORE(dir)"
    # 复用引擎的 canonical 解析（单一真源，§1050）
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._helpers import _find_trinity_store  # noqa: E402
        d = _find_trinity_store()
        p = d if os.path.isfile(d) else os.path.join(d, "trinity_store.db")
        return p, "engine:_find_trinity_store"
    except Exception:  # noqa: BLE001
        return DEFAULT_STORE, "default"


def sha256_of(text) -> str:
    """纯函数：与 adapter 同式 `hashlib.sha256(content.encode("utf-8")).hexdigest()`
    （`trinity/adapters/postgresql.py:483`、`sqlite/_crypto.py:40`）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _connect(store: str, write: bool) -> sqlite3.Connection:
    uri = "file:%s?mode=%s" % (store.replace("\\", "/"), "rw" if write else "ro")
    con = sqlite3.connect(uri, uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    return con


def _prune_backups(keep: int = BACKUP_KEEP) -> list:
    try:
        names = [n for n in os.listdir(BACKUP_ROOT) if n.startswith(BACKUP_PREFIX)]
    except OSError:
        return []
    groups: dict = {}
    for n in names:
        stamp = n[len(BACKUP_PREFIX):]
        for suffix in ("-wal", "-shm"):
            if stamp.endswith(suffix):
                stamp = stamp[: -len(suffix)]
        groups.setdefault(stamp, []).append(n)
    removed = []
    for stamp in sorted(groups, reverse=True)[keep:]:
        for n in groups[stamp]:
            try:
                os.remove(os.path.join(BACKUP_ROOT, n))
                removed.append(n)
            except OSError:
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/backfill_content_hash.py::_prune_backups")
    return removed


def backup_store(store: str) -> str:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    dst = os.path.join(BACKUP_ROOT, BACKUP_PREFIX + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(store, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(store + suffix):
            shutil.copy2(store + suffix, dst + suffix)
    _prune_backups()
    return dst


def plan(store: str, source: str, limit: int = 0) -> dict:
    """只读体检：待回填面 + 明文抽样断言 + 唯一索引上下文。"""
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
           "store_source": source}
    if store.replace("\\", "/").lower() == DEAD_STORE.replace("\\", "/").lower():
        out["warning"] = ("指向灾难恢复前的旧库路径；活库应为 %s" % DEFAULT_STORE)
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    try:
        con = _connect(store, write=False)
        cols = [r[1] for r in con.execute("PRAGMA table_info(memories)")]
        need = {"memory_id", "persona_id", "agent_id", "content",
                "content_hash", "sha256_hash", "status"}
        missing = sorted(need - set(cols))
        if missing:
            con.close()
            out.update(verdict="INCONCLUSIVE", error="missing columns: %s" % missing)
            return out
        # 2026-10-06（t74/I14）：原为 **lambda 赋值 + E731 抑制注释**
        # —— E731 是棘轮规则之一，靠抑制把违规藏起来 = "借来的绿" ⇒ 改成 `def`（真修）。
        def q(s, p=()):
            return con.execute(s, p).fetchone()[0]
        out["rows_total"] = q("SELECT COUNT(*) FROM memories")
        out["hash_null_total"] = q("SELECT COUNT(*) FROM memories WHERE content_hash IS NULL")
        out["hash_null_active"] = q(
            "SELECT COUNT(*) FROM memories WHERE content_hash IS NULL AND status='active'")
        out["sha_empty_active"] = q(
            "SELECT COUNT(*) FROM memories WHERE status='active' "
            "AND (sha256_hash IS NULL OR sha256_hash='')")
        out["candidates"] = q(
            "SELECT COUNT(*) FROM memories WHERE content_hash IS NULL AND status='active'")
        out["index_sql"] = (con.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_memories_content_hash'"
        ).fetchone() or [None])[0]
        # §13.1：抽样断言明文（这些行实测全为明文；断言必须**能失败**）
        samp = con.execute(
            "SELECT content FROM memories WHERE content_hash IS NULL AND status='active' "
            "LIMIT 5").fetchall()
        enc = sum(1 for (c,) in samp if (c or "").startswith("enc:v1:"))
        out["plaintext_sample_n"] = len(samp)
        out["plaintext_sample_ciphertext"] = enc
        out["plaintext_ok"] = (len(samp) > 0 and enc == 0)
        if limit:
            out["limit"] = limit
        con.close()
    except Exception as e:  # noqa: BLE001
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    out["verdict"] = "OK"
    return out


def apply_backfill(store: str, limit: int = 0) -> dict:
    """回填：只补空位；撞唯一索引的行跳过并保持 NULL（与 PG 侧同语义）。"""
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store}
    res["backup"] = backup_store(store)
    con = _connect(store, write=True)
    filled = skipped_unique = failed_other = empty_content = 0
    null_memory_id = 0
    sample_ciphertext = 0
    try:
        sql = ("SELECT memory_id, persona_id, agent_id, content FROM memories "
               "WHERE content_hash IS NULL" + (" LIMIT ?" if limit else ""))
        rows = con.execute(sql, (limit,) if limit else ()).fetchall()
        res["candidates_scanned"] = len(rows)
        for mid, persona, agent, content in rows:
            if (content or "").startswith("enc:v1:"):
                sample_ciphertext += 1          # §13.1：拿到密文必须计数，不许静默
                continue
            if content is None or content == "":
                empty_content += 1
                continue
            if mid is None:
                # 2026-10-05 实测：库里真有 1 行 `memory_id IS NULL`（status='merged'）。
                # `WHERE memory_id=?` 传 None 时 SQL 的 NULL 比较恒不成立 ⇒ rowcount=0，
                # 该行**既没被写也没被计数** —— 这正是 §13.2「不许合并/漏计失败原因」。
                # 单列一个桶，让「扫描 897 / 回填 896」的差额永远有解释。
                null_memory_id += 1
                continue
            h = sha256_of(content)
            try:
                # 双列同写：content_hash 与 sha256_hash 必须一致（原版只写前者）
                cur = con.execute(
                    "UPDATE memories SET content_hash=?, sha256_hash=? "
                    "WHERE memory_id=? AND content_hash IS NULL",
                    (h, h, mid))
                con.commit()
                # ⚠ 用 `cur.rowcount`，**不是** `con.total_changes`：
                # 后者是**连接级累计值**（只增不减），拿它当"本次是否写入"
                # 会把 0 行的 UPDATE 也计成成功（自证字段虚报）。
                filled += cur.rowcount
            except sqlite3.IntegrityError:
                con.rollback()
                skipped_unique += 1             # 撞 (persona_id,agent_id,content_hash)
            except Exception:  # noqa: BLE001  §13.2 不与上面合并计数
                con.rollback()
                failed_other += 1
        res.update(filled=filled, skipped_unique_conflict=skipped_unique,
                   failed_other=failed_other, empty_content=empty_content,
                   skipped_null_memory_id=null_memory_id,
                   sample_ciphertext=sample_ciphertext)

        # ---- Pass 2：另一个派生列不一致（content_hash 已设而 sha256_hash 为空）----
        # 2026-10-05 实测：active 240 / archived 32 / merged 186 属于这一类
        # （类目 video_harvested / web_harvested / wms_knowledge…，是**另一个写入方**留下的）。
        # 语义与 Pass 1 对称，故同工具处理，而不是新开一个脚本（§1050）。
        # 关键：**先复算 sha256(content) 与已有 content_hash 比对** —— 相符才补，
        # 不符则计入 `hash_drift`（本仓已知「派生列会与 content 漂移」，
        # 见 `scripts/reconcile_pg_sqlite.py:147`）。**绝不**直接把 content_hash 抄过去。
        sha_repaired = hash_drift = sha_failed = 0
        lim2 = (" LIMIT %d" % limit) if limit else ""
        for mid, ch, content in con.execute(
                "SELECT memory_id, content_hash, content FROM memories "
                "WHERE (sha256_hash IS NULL OR sha256_hash='') "
                "AND content_hash IS NOT NULL AND content_hash<>''" + lim2).fetchall():
            if (content or "").startswith("enc:v1:") or not content or mid is None:
                sha_failed += 1
                continue
            if sha256_of(content) != ch:
                hash_drift += 1                 # 派生列与 content 不一致：只报不改
                continue
            try:
                cur = con.execute(
                    "UPDATE memories SET sha256_hash=? WHERE memory_id=? "
                    "AND (sha256_hash IS NULL OR sha256_hash='')", (ch, mid))
                con.commit()
                sha_repaired += cur.rowcount
            except Exception:  # noqa: BLE001
                con.rollback()
                sha_failed += 1
        res.update(sha256_repaired=sha_repaired, hash_drift=hash_drift,
                   sha_repair_failed=sha_failed)
        # 验证：回填后 active 重复组必须仍为 0（唯一索引语义未被破坏）
        res["active_dup_groups_after"] = con.execute("""
            SELECT COUNT(*) FROM (
                SELECT persona_id, agent_id, content_hash FROM memories
                WHERE status='active' AND content_hash IS NOT NULL
                GROUP BY persona_id, agent_id, content_hash HAVING COUNT(*) > 1)
        """).fetchone()[0]
        res["hash_null_active_after"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE content_hash IS NULL AND status='active'"
        ).fetchone()[0]
    except Exception as e:  # noqa: BLE001
        con.close()
        res.update(verdict="FAILED", error="%s: %s" % (type(e).__name__, str(e)[:200]))
        return res
    con.close()
    res["verdict"] = "OK"
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    store, source = resolve_store(a.store)
    r = apply_backfill(store, a.limit) if a.apply else plan(store, source, a.limit)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== content_hash / sha256_hash 存量回填 ==")
        print("库：%s（来源 %s）" % (r.get("store"), r.get("store_source", "-")))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("warning"):
            print("⚠ %s" % r["warning"])
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error")))
            return 2
        if a.apply:
            print("备份：%s" % r["backup"])
            print("Pass1 扫描候选        %d" % r["candidates_scanned"])
            print("  已回填            %d" % r["filled"])
            print("  撞唯一索引跳过    %d（保持 NULL，与 PG 侧同语义）" % r["skipped_unique_conflict"])
            print("  其它失败          %d（§13.2：不与上面合并计数）" % r["failed_other"])
            print("  空内容            %d" % r["empty_content"])
            print("  memory_id 为 NULL %d（无法按 id UPDATE —— 必须单列，否则读数对不上）"
                  % r.get("skipped_null_memory_id", 0))
            print("  拿到密文          %d（§13.1 计数上报）" % r["sample_ciphertext"])
            print("Pass2 sha256_hash 补齐 %d" % r.get("sha256_repaired", 0))
            print("  hash 漂移（只报不改） %d" % r.get("hash_drift", 0))
            print("  Pass2 失败           %d" % r.get("sha_repair_failed", 0))
            print("回填后 active 重复组 %d（必须 0）" % r["active_dup_groups_after"])
            print("回填后 active NULL   %d" % r["hash_null_active_after"])
            scanned = r["candidates_scanned"]
            accounted = (r["filled"] + r["skipped_unique_conflict"] + r["failed_other"]
                         + r["empty_content"] + r.get("skipped_null_memory_id", 0)
                         + r["sample_ciphertext"])
            print("对账：扫描 %d = 各桶之和 %d ⇒ %s"
                  % (scanned, accounted, "平衡" if scanned == accounted else "**不平衡（有漏计）**"))
        else:
            print("总行 %d；content_hash NULL 合计 %d（active %d）"
                  % (r["rows_total"], r["hash_null_total"], r["hash_null_active"]))
            print("active 中 sha256_hash 为空 %d" % r["sha_empty_active"])
            print("唯一索引：%s" % (r["index_sql"] or "").replace("\n", " ")[:150])
            print("明文抽样 %d 条，其中密文 %d 条 ⇒ plaintext_ok=%s"
                  % (r["plaintext_sample_n"], r["plaintext_sample_ciphertext"],
                     r["plaintext_ok"]))
            if not r["plaintext_ok"]:
                print("⚠ 抽样出现密文/空样本 ⇒ 不可直接 sha256(明文)，需走接口取明文")
            print()
            print("（只读体检。写库请加 --apply；写前自动备份到 ~/.trinity/store-backups/）")

    if a.apply and r.get("verdict") == "OK":
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        out = os.path.join(ROOT, "output",
                           "backfill_content_hash_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print()
        print("产物：%s" % out)
    return 0 if r.get("verdict") == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
