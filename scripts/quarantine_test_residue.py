#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试残留隔离（Step 1 的隔离半）—— 只动**无歧义**的测试名空间，可回滚。

## 为什么只有 27 行，而计划里写的是「约 750 行」

计划里的 750 是**我按名空间粗估**的，实测**不成立**（2026-10-05）：

| 桶 | 实测 | 处置 |
|---|---|---|
| `ablate-locomo`(85) / `stress-agent`(1) | 类目 `benchmark`/`stress-test` | **已被 §13.0 排除，无需处理** |
| **无歧义测试残留** | **27 行**（`cache_test`/`kw_test`/`t1`/`a`/`b1`/`sig_*`/`_warmup`/`probe-agent`/**‹smoke›**…） | 本工具处理 |
| `agent_id` 为空的不可归属行 | 1 行 | 默认**不**动，单列报告（`--include-unattributed` 才处理） |
| `reader*` / `u1` / `usage-feedback` | 60 行 | **看着像合法子 agent**，判为 `unknown` 等人工定性，**不动** |
| `machine_self`（`brain-procedure` 2,653 等） | **3,922 行（17.1%）** | **是管道状态，不是残留**；归档会打断管道 ⇒ **需人工拍板，本工具不碰** |

## 与既有实现的关系（§1050：不另立一份）

`scripts/quarantine_benchmark_active.py` 已存在，同属「把非生产语料移出 active 面」的关切，
形态也照抄它（**只归档不删除**、dry-run 默认、先备份、幂等）。本工具不与它重复的地方：

1. **目标集不同**：它打的是评测/消融语料（`ablate-%`/`bench-%`/`category IN benchmark,lme,…`），
   本工具打的是**测试脚手架名空间**（`a`/`b1`/`t1`/`cache_test`…）；
2. **库不同**：它是 `pg_connect()`（**PG only**），而常驻 API 服务的是 **SQLite**
   （D28）；实测它的判据一条都命中不了 SQLite 侧这些行；
3. 它写 PG 审计链（advisory 锁），本工具不碰审计链 —— 只落 `output/` 证据 +
   **回滚清单**，避免把 PG 的锁机制硬搬到 SQLite。

**若将来要合并**：把本工具的选择集并进它的 `TARGET_PREDICATE`，并给它加 SQLite 分支。

## 纪律
- **默认 dry-run**；`--apply` 才写，且**先备份**。
- **可回滚**：写前落 `output/quarantine_test_residue_<ts>.rollback.json`（含 memory_id 与
  原 status），`--rollback <file>` 可原样还原。
- **幂等**：重复运行只影响仍是 `active` 的行。
- **§13.0**：已在排除类目里的行**不再动**（它们本来就不在检索面），并显式报数。
- **§13.2**：取不到/失败**分原因计数**。

用法：
    python scripts/quarantine_test_residue.py                    # 只读体检（默认）
    python scripts/quarantine_test_residue.py --apply            # 归档（先备份）
    python scripts/quarantine_test_residue.py --rollback output/quarantine_test_residue_XXX.rollback.json
"""
from __future__ import annotations

import argparse
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
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/quarantine_test_residue.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")
BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".trinity", "store-backups")
BACKUP_PREFIX = "trinity_store.db.pre-testresidue-"
BACKUP_KEEP = 2

#: **只放无歧义的测试脚手架名空间**。判准：名字本身就是测试用语
#: （`*_test` / `t1` / `a` / `b1` / `sig_*` / `_warmup` / `probe-agent` / `smoke` / `*-test*`）。
#: **不放**：`reader*`/`u1`/`usage-feedback`（疑似合法子 agent）、
#: 任何 `machine_self` 管道名空间（是管道状态，需人工拍板）。
RESIDUE_AGENTS = (
    "cache_test", "kw_test", "inc-test", "inc-vec", "inc-vec2", "heur-test3",
    "fix-test", "probe-agent", "t1", "a", "b1", "sig_0", "sig_1", "_warmup",
    "ingest_test", "test-vec-agent", "smoke",
)


def resolve_store(explicit: str = "") -> "tuple[str, str]":
    """`TRINITY_STORE` 语义是 **store 目录**（`_helpers.py:22` / `_construction.py:162-165`）。"""
    if explicit:
        return explicit, "cli"
    env = os.environ.get("TRINITY_STORE")
    if env:
        return (env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")), \
            "env:TRINITY_STORE"
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._helpers import _find_trinity_store  # noqa: E402
        d = _find_trinity_store()
        return (d if os.path.isfile(d) else os.path.join(d, "trinity_store.db")), \
            "engine:_find_trinity_store"
    except Exception:  # noqa: BLE001
        return DEFAULT_STORE, "default"


def load_exclusions() -> "tuple[list, str]":
    """§13.0：排除类目只从引擎常量取（不另立一份）。"""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402
        return sorted(set(_RETRIEVAL_EXCLUDE_CATEGORIES)), "trinity.core.client._search"
    except Exception as e:  # noqa: BLE001
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def _connect(store: str, write: bool = False) -> sqlite3.Connection:
    uri = "file:%s?mode=%s" % (store.replace("\\", "/"), "rw" if write else "ro")
    con = sqlite3.connect(uri, uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    return con


def is_residue_agent(agent_id) -> bool:
    """纯函数：该 agent 名空间是否属于**无歧义**测试残留。可单测。"""
    return (agent_id or "") in RESIDUE_AGENTS


def select_targets(con, excl: list, include_unattributed: bool = False) -> dict:
    """挑出仍在检索面的测试残留行。纯查询，不写。"""
    ph = ",".join("?" for _ in excl) if excl else "''"
    q = ",".join("?" for _ in RESIDUE_AGENTS)
    rows = con.execute(
        "SELECT memory_id, agent_id, category, status FROM memories "
        "WHERE status='active' AND agent_id IN (%s) AND category NOT IN (%s)"
        % (q, ph), tuple(RESIDUE_AGENTS) + tuple(excl)).fetchall()
    out = {"targets": [dict(memory_id=r[0], agent_id=r[1], category=r[2], status=r[3])
                       for r in rows]}
    # 已在排除类目里的同批：**不需要动**，但必须显式报数（§13.0 附带纪律）
    out["already_excluded"] = con.execute(
        "SELECT COUNT(*) FROM memories WHERE status='active' AND agent_id IN (%s) "
        "AND category IN (%s)" % (q, ph),
        tuple(RESIDUE_AGENTS) + tuple(excl)).fetchone()[0]
    if include_unattributed:
        extra = con.execute(
            "SELECT memory_id, agent_id, category, status FROM memories "
            "WHERE status='active' AND (agent_id IS NULL OR agent_id='') "
            "AND category NOT IN (%s)" % ph, tuple(excl)).fetchall()
        out["targets"] += [dict(memory_id=r[0], agent_id=r[1], category=r[2],
                                status=r[3]) for r in extra]
    out["unattributed_present"] = con.execute(
        "SELECT COUNT(*) FROM memories WHERE status='active' "
        "AND (agent_id IS NULL OR agent_id='')").fetchone()[0]
    return out


def verify_absent(con, ids: list) -> int:
    """仍是 active 的目标数（0 = 隔离成功）。"""
    if not ids:
        return 0
    ph = ",".join("?" for _ in ids)
    return con.execute(
        "SELECT COUNT(*) FROM memories WHERE status='active' AND memory_id IN (%s)"
        % ph, tuple(ids)).fetchone()[0]


def _prune(keep: int = BACKUP_KEEP) -> None:
    try:
        names = [n for n in os.listdir(BACKUP_ROOT) if n.startswith(BACKUP_PREFIX)]
    except OSError:
        return
    groups: dict = {}
    for n in names:
        stamp = n[len(BACKUP_PREFIX):]
        for sfx in ("-wal", "-shm"):
            if stamp.endswith(sfx):
                stamp = stamp[: -len(sfx)]
        groups.setdefault(stamp, []).append(n)
    for stamp in sorted(groups, reverse=True)[keep:]:
        for n in groups[stamp]:
            try:
                os.remove(os.path.join(BACKUP_ROOT, n))
            except OSError:
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/quarantine_test_residue.py::_prune")


def backup_store(store: str) -> str:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    dst = os.path.join(BACKUP_ROOT, BACKUP_PREFIX + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(store, dst)
    for sfx in ("-wal", "-shm"):
        if os.path.exists(store + sfx):
            shutil.copy2(store + sfx, dst + sfx)
    _prune()
    return dst


def plan(store: str, source: str, include_unattributed: bool = False) -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
           "store_source": source}
    excl, esrc = load_exclusions()
    out["engine_exclude_categories"] = excl
    out["engine_exclude_source"] = esrc
    if not excl:
        out.update(verdict="INCONCLUSIVE", error="engine exclusions unavailable: %s" % esrc)
        return out
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    try:
        con = _connect(store)
        sel = select_targets(con, excl, include_unattributed)
        con.close()
    except Exception as e:  # noqa: BLE001
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    out.update({"targets": len(sel["targets"]),
                "already_excluded_rows": sel["already_excluded"],
                "unattributed_present": sel["unattributed_present"],
                "include_unattributed": include_unattributed,
                "by_agent": {}})
    for t in sel["targets"]:
        k = t["agent_id"] or "<empty>"
        out["by_agent"][k] = out["by_agent"].get(k, 0) + 1
    out["verdict"] = "OK"
    return out


def apply_quarantine(store: str, include_unattributed: bool = False) -> dict:
    excl, _ = load_exclusions()
    if not excl:
        return {"verdict": "INCONCLUSIVE", "error": "engine exclusions unavailable"}
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store}
    res["backup"] = backup_store(store)
    con = _connect(store, write=True)
    try:
        sel = select_targets(con, excl, include_unattributed)
        ids = [t["memory_id"] for t in sel["targets"]]
        res["targets"] = len(ids)
        # 回滚清单**写在实际改动之前**：万一写库中途失败，清单也已存在
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        rb = os.path.join(ROOT, "output", "quarantine_test_residue_%s.rollback.json"
                          % time.strftime("%Y%m%d_%H%M%S"))
        with open(rb, "w", encoding="utf-8") as fh:
            json.dump({"store": store, "created_at": res["ts"],
                       "rows": sel["targets"],
                       "rollback_sql": "UPDATE memories SET status='active' "
                                       "WHERE memory_id IN (...)"}, fh,
                      ensure_ascii=False, indent=1)
        res["rollback_manifest"] = rb
        changed = 0
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        for t in sel["targets"]:
            cur.execute("UPDATE memories SET status='archived' "
                        "WHERE memory_id=? AND status='active'", (t["memory_id"],))
            changed += cur.rowcount
        con.commit()
        res["archived"] = changed
        res["already_excluded_untouched"] = sel["already_excluded"]
        res["still_active_after"] = verify_absent(con, ids)
    except Exception as e:  # noqa: BLE001
        con.rollback()
        con.close()
        res.update(verdict="FAILED", error="%s: %s" % (type(e).__name__, str(e)[:200]))
        return res
    con.close()
    res["verdict"] = "OK"
    return res


def rollback(store: str, manifest: str) -> dict:
    """按回滚清单把行还原为 active。"""
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store, "manifest": manifest}
    try:
        data = json.load(open(manifest, encoding="utf-8"))
        ids = [r["memory_id"] for r in (data.get("rows") or []) if r.get("memory_id")]
    except Exception as e:  # noqa: BLE001
        res.update(verdict="FAILED", error="manifest unreadable: %s" % str(e)[:150])
        return res
    if not ids:
        res.update(verdict="OK", restored=0, note="清单为空")
        return res
    res["backup"] = backup_store(store)
    con = _connect(store, write=True)
    try:
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        n = 0
        for mid in ids:
            cur.execute("UPDATE memories SET status='active' "
                        "WHERE memory_id=? AND status='archived'", (mid,))
            n += cur.rowcount
        con.commit()
        res["restored"] = n
        # 还原后校验：这批 id 里仍被归档的数量（0 = 全部还原）
        ph = ",".join("?" for _ in ids)
        res["still_archived"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='archived' AND memory_id IN (%s)"
            % ph, tuple(ids)).fetchone()[0]
    except Exception as e:  # noqa: BLE001
        con.rollback()
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
    ap.add_argument("--rollback", default="")
    ap.add_argument("--include-unattributed", action="store_true",
                    help="连 agent_id 为空的行一起归档（默认不动，单列报告）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    store, src = resolve_store(a.store)

    if a.rollback:
        r = rollback(store, a.rollback)
    elif a.apply:
        r = apply_quarantine(store, a.include_unattributed)
    else:
        r = plan(store, src, a.include_unattributed)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== 测试残留隔离（Step 1 隔离半）==")
        print("库：%s（来源 %s）" % (r.get("store"), r.get("store_source", "-")))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error")))
            return 2
        if a.rollback:
            print("已还原 %d 行（备份 %s）" % (r["restored"], r.get("backup")))
        elif a.apply:
            print("备份：%s" % r["backup"])
            print("回滚清单：%s" % r["rollback_manifest"])
            print("目标 %d 行；实际归档 %d 行" % (r["targets"], r["archived"]))
            print("已在排除类目中、未动 %d 行（§13.0）" % r["already_excluded_untouched"])
            print("归档后仍为 active 的目标数 %d（必须 0）" % r["still_active_after"])
        else:
            print("引擎排除类目 %s" % r["engine_exclude_categories"])
            print("待隔离（仍在检索面）%d 行" % r["targets"])
            print("  按名空间：%s" % r["by_agent"])
            print("已在排除类目中、无需处理 %d 行（§13.0 显式报数）"
                  % r["already_excluded_rows"])
            print("agent_id 为空的不可归属行 %d（默认不动；--include-unattributed 才处理）"
                  % r["unattributed_present"])
            print()
            print("（只读体检。写库请加 --apply；写前自动备份并落回滚清单）")
    if a.apply and r.get("verdict") == "OK" and not a.json:
        print()
        print("回滚：python scripts/quarantine_test_residue.py --rollback %s"
              % r["rollback_manifest"])
    return 0 if r.get("verdict") == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
