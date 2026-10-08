#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复习调度（SQLite / 服务中的那个库）——Step 4。

## 为什么需要它（而不是再写一个 FSRS）

**FSRS 调度器已经存在**：`scripts/brain_cycle.py::step_fsrs`（约 225-278 行）——按策略
tiers 初始化 `review_interval_days`、初始化 `next_review_at`、统计到期数、再按
`review_budget_per_day` 预算、**importance DESC** 顺序消费到期队列（间隔 ×1.5、上限 180 天）。

**但它只连 PostgreSQL**（`psycopg2`，`scripts/brain_cycle.py:88,113-118`），
而**常驻 API 服务的是 SQLite 库**（`TRINITY_STORE=…/store-restored`，见 D28）。
实测（2026-10-05）：

| | SQLite（服务中） |
|---|---|
| active | 27,034 |
| `review_interval_days` 为 0/NULL | **26,848** |
| `next_review_at` 为 NULL/空 | **26,848** |
| 已排期 | 仅 186 条，且**全部逾期**（值停在 2026-08-04 ~ 08-22） |

⇒ 缺口**不是「没有机制」，而是「机制打在了另一个库上」**。本脚本把同一套策略
应用到**服务中的那个库**，让「到期复习」这条读路径在线上真正存在。

**不重复实现策略**：间隔天数一律来自 `scripts/forgetting_policy.py::load_policy()`
（该文件自己的注释就写明 `fsrs_days()` 是「供 brain_cycle FSRS 使用，替代内联映射」）
⇒ 策略只有一处真源（AGENTS.md §1050 纪律）。

## 与 brain_cycle 的一处**有意**差异（重要）

`brain_cycle.step_fsrs` 在复习时执行 `access_count = access_count + 1`。
**本脚本默认不这么做**，理由：`access_count` 正是利用率/冷池读数所用的列
（Step 0 的「从未被读」= `COALESCE(access_count,0)=0`）。自动复习去 +1，
会把「机器自己戳了一下」记成「被读过」，**直接把冷率读数做假**。
需要与 brain_cycle 完全对齐时显式加 `--touch-access-count`。

（本仓已用 `U1a_reader_attributed`（走 `audit_log.memory_ids`）来规避这一类自读污染 ——
本脚本的默认行为与之同向。）

## 纪律

- **默认 dry-run**；写库必须显式 `--apply`，且**先备份**（§16.1 同族：改动前留快照）。
- **§13.0**：不参与检索的类目（引擎 `_RETRIEVAL_EXCLUDE_CATEGORIES`）**不进排期**，
  且**显式报数** `retrieval_excluded_rows`。
- **§13.2**：库不可读 ⇒ `INCONCLUSIVE` + 原因，绝不把「取不到」读成「没有」。
- 测量数据落 `output/`（§12：不要放 state/）。

用法：
    python scripts/review_schedule_sqlite.py                 # 只读体检（默认）
    python scripts/review_schedule_sqlite.py --json
    python scripts/review_schedule_sqlite.py --apply         # 初始化排期（先备份）
    python scripts/review_schedule_sqlite.py --consume --apply --budget 200
"""
from __future__ import annotations

import argparse
import datetime as _dt
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
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/review_schedule_sqlite.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 活库默认位置。**只作最后的字面兜底** —— 正常路径一律走 `resolve_store()`
#: （复用引擎 canonical 解析）。硬编码活库路径正是旧 `backfill_content_hash.py`
#: 的病灶（灾难恢复后它还在写死库），不能重犯。
DEFAULT_STORE = os.path.join(
    os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db")
BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".trinity", "store-backups")


def resolve_store(explicit: str = "") -> "tuple[str, str]":
    """库路径解析：--store > TRINITY_STORE > 引擎 canonical > 字面兜底。

    ⚠ `TRINITY_STORE` 的语义是 **store 目录**（`trinity/core/client/_helpers.py:22`；
    `_construction.py:162-165`：是文件用文件、是目录拼 `trinity_store.db`）。
    2026-10-05 实测：把 env 当文件路径直接用 ⇒ `unable to open database file`。
    """
    if explicit:
        return explicit, "cli"
    env = os.environ.get("TRINITY_STORE")
    if env:
        if os.path.isfile(env):
            return env, "env:TRINITY_STORE(file)"
        return os.path.join(env, "trinity_store.db"), "env:TRINITY_STORE(dir)"
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._helpers import _find_trinity_store  # noqa: E402
        d = _find_trinity_store()
        return (d if os.path.isfile(d) else os.path.join(d, "trinity_store.db")), \
            "engine:_find_trinity_store"
    except Exception:  # noqa: BLE001
        return DEFAULT_STORE, "default"

#: 写库时的日期格式：ISO-8601 + 显式 UTC 偏移。
#: 选它的理由：库里既有的 186 条是 `YYYY-MM-DDTHH:MM:SS`（无偏移），
#: 本格式与它**逐字节可比到秒**（字典序即时间序），且比无偏移形式更明确。
#: brain_cycle 用的是 `YYYY-MM-DD HH24:MI:SS+00`（空格分隔）——两者**不可混比**，
#: 故此处显式记录差异，避免后来者把两个库的读数直接相减。
TS_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def fmt(ts: _dt.datetime) -> str:
    return ts.strftime(TS_FMT)


def load_tiers() -> "tuple[list, str]":
    """FSRS 间隔分层**只从策略文件取**（§1050：不另立一份）。

    返回 (tiers, source)；tiers 形如 [{"min_imp":0.8,"days":30}, ...] 按 min_imp 降序。
    取不到时返回 ([], reason)，由调用方判 INCONCLUSIVE —— 不用本文件的硬编码兜底冒充策略。
    """
    p = os.path.join(ROOT, "scripts")
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import forgetting_policy  # noqa: E402
        pol = forgetting_policy.load_policy()
        tiers = sorted(pol.get("fsrs", {}).get("tiers", []) or [],
                       key=lambda t: t["min_imp"], reverse=True)
        if not tiers:
            return [], "UNAVAILABLE: forgetting_policy has no fsrs.tiers"
        return tiers, "scripts/forgetting_policy.py"
    except Exception as e:  # noqa: BLE001  §13.2 失败原因必须留痕
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def load_budget() -> int:
    p = os.path.join(ROOT, "scripts")
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import forgetting_policy  # noqa: E402
        return int(forgetting_policy.load_policy().get("schedule", {})
                   .get("review_budget_per_day", 500))
    except Exception:  # noqa: BLE001
        return 500


def load_default_days() -> int:
    """兜底天数 = 策略的 `fsrs.default_days`（**不要**用 tiers[-1].days 代替）。

    2026-10-05 实测代价：本脚本初版在无 tier 命中时回落到 `tiers[-1]["days"]`（=60），
    而策略文件与 `forgetting_policy.fsrs_days()` 的兜底都是 **`default_days` = 90**
    （`scripts/forgetting_policy.py:40,65`），brain_cycle 的内联 CASE 也是 `ELSE '90'`。
    该错会让 importance<0.6 的 21,570 行被排成 60 天而非 90 天 —— 静默、且与既有实现不一致。
    """
    p = os.path.join(ROOT, "scripts")
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import forgetting_policy  # noqa: E402
        return int(forgetting_policy.load_policy().get("fsrs", {}).get("default_days", 90))
    except Exception:  # noqa: BLE001
        return 90


def interval_days(importance, tiers: list, default_days: int = 90) -> int:
    """纯函数：importance -> 间隔天数。无 tier 命中时用 `default_days`（=策略兜底）。"""
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        imp = -1.0
    for t in tiers:
        if imp >= float(t["min_imp"]):
            return int(t["days"])
    return int(default_days)


def advance_days(current_interval, cap: int = 180, factor: float = 1.5) -> int:
    """纯函数：FSRS 简版推进（与 brain_cycle 的 `LEAST(interval*1.5, 180)` 同式）。"""
    try:
        cur = int(current_interval or 0)
    except (TypeError, ValueError):
        cur = 0
    if cur <= 0:
        cur = 1
    return min(int(cur * factor), cap)


def load_engine_exclusions() -> "tuple[list, str]":
    """§13.0：排除类目只从引擎常量取。"""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402
        return sorted(set(_RETRIEVAL_EXCLUDE_CATEGORIES)), "trinity.core.client._search"
    except Exception as e:  # noqa: BLE001
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def _connect(store: str, write: bool) -> sqlite3.Connection:
    uri = "file:%s?mode=%s" % (store.replace("\\", "/"), "rw" if write else "ro")
    con = sqlite3.connect(uri, uri=True, timeout=40)
    con.execute("PRAGMA busy_timeout=35000")
    return con


#: 备份家族前缀与保留代数。
#: 2026-10-05 实测：库是 2.5 GB，每写一次就整库拷一份 ⇒ 跑两次即 5.16 GB。
#: 这是**本脚本自己引入**的缺陷（不是既有问题），所以就地设上限：同族只留最新 2 代。
BACKUP_PREFIX = "trinity_store.db.pre-reviewsched-"
BACKUP_KEEP = 2


def backup_store(store: str) -> str:
    """写库前备份（§16.1：改动带未提交状态的活系统前先留快照），并**限制代数**。

    只清理**本家族**（`BACKUP_PREFIX` 前缀）的文件，不碰目录里其它备份。
    """
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_ROOT, BACKUP_PREFIX + stamp)
    shutil.copy2(store, dst)
    for suffix in ("-wal", "-shm"):
        src = store + suffix
        if os.path.exists(src):
            shutil.copy2(src, dst + suffix)
    _prune_backups()
    return dst


def _prune_backups(keep: int = BACKUP_KEEP) -> list:
    """同族只留最新 `keep` 代（按 stamp 分组，连同 -wal/-shm 一起删）。返回被删列表。"""
    try:
        names = [n for n in os.listdir(BACKUP_ROOT) if n.startswith(BACKUP_PREFIX)]
    except OSError:
        return []
    groups: dict = {}
    for n in names:
        stamp = n[len(BACKUP_PREFIX):]           # 去掉前缀
        for suffix in ("-wal", "-shm"):
            if stamp.endswith(suffix):
                stamp = stamp[: -len(suffix)]
        groups.setdefault(stamp, []).append(n)
        # stamp 形如 20261005-180252 ⇒ 字典序即时间序
    removed = []
    for stamp in sorted(groups, reverse=True)[keep:]:
        for n in groups[stamp]:
            try:
                os.remove(os.path.join(BACKUP_ROOT, n))
                removed.append(n)
            except OSError:
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/review_schedule_sqlite.py::_prune_backups")
    return removed


def plan(store: str = DEFAULT_STORE) -> dict:
    """只读体检 + 出计划。不写库。"""
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store}
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    tiers, tsrc = load_tiers()
    excl, esrc = load_engine_exclusions()
    dd = load_default_days()
    out["tiers"] = tiers
    out["default_days"] = dd
    out["tiers_source"] = tsrc
    out["engine_exclude_categories"] = excl
    out["engine_exclude_source"] = esrc
    if not tiers:
        out.update(verdict="INCONCLUSIVE", error="policy unavailable: %s" % tsrc)
        return out
    if not excl:
        out.update(verdict="INCONCLUSIVE", error="engine exclusions unavailable: %s" % esrc)
        return out

    try:
        con = _connect(store, write=False)
        cols = [r[1] for r in con.execute("PRAGMA table_info(memories)")]
        need = {"status", "importance", "review_interval_days", "next_review_at",
                "category", "agent_id"}
        missing = sorted(need - set(cols))
        if missing:
            con.close()
            out.update(verdict="INCONCLUSIVE",
                       error="missing columns: %s" % missing)
            return out
        ph = ",".join("?" for _ in excl)
        base = "status='active' AND category NOT IN (%s)" % ph

        out["active_total"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        out["retrieval_excluded_rows"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND category IN (%s)"
            % ph, tuple(excl)).fetchone()[0]
        out["schedulable_rows"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE " + base, tuple(excl)).fetchone()[0]

        out["interval_unset"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE " + base +
            " AND (review_interval_days IS NULL OR CAST(review_interval_days AS INTEGER)<=0)",
            tuple(excl)).fetchone()[0]
        out["next_unset"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE " + base +
            " AND (next_review_at IS NULL OR next_review_at='')", tuple(excl)).fetchone()[0]
        out["next_set"] = out["schedulable_rows"] - out["next_unset"]
        out["due_now"] = con.execute(
            "SELECT COUNT(*) FROM memories WHERE " + base +
            " AND next_review_at IS NOT NULL AND next_review_at<>'' "
            " AND next_review_at <= ?", tuple(excl) + (fmt(now_utc()),)).fetchone()[0]

        # 初始化后会落到哪几档（用于给预算定量，而不是拍脑袋）
        dist = {}
        for imp, n in con.execute(
                "SELECT importance, COUNT(*) FROM memories WHERE " + base +
                " GROUP BY importance", tuple(excl)):
            d = interval_days(imp, tiers, dd)
            dist[d] = dist.get(d, 0) + n
        out["planned_interval_histogram"] = {str(k): v for k, v in sorted(dist.items())}
        out["budget_policy"] = load_budget()
        con.close()
    except Exception as e:  # noqa: BLE001
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    out["verdict"] = "OK"
    return out


def apply_init(store: str) -> dict:
    """写库：初始化 review_interval_days 与 next_review_at（只补空位，不覆盖既有值）。"""
    tiers, tsrc = load_tiers()
    excl, _ = load_engine_exclusions()
    if not tiers or not excl:
        return {"verdict": "INCONCLUSIVE", "error": "policy/exclusions unavailable"}
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
           "tiers_source": tsrc}
    res["backup"] = backup_store(store)
    now = now_utc()
    ph = ",".join("?" for _ in excl)
    base = "status='active' AND category NOT IN (%s)" % ph
    con = _connect(store, write=True)
    try:
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        # ① 间隔：按策略 tiers 写（只补 NULL/<=0，不覆盖既有）
        #    ⚠ 占位符顺序：SET 的 ? → base 里 IN 的 ? → 末尾 importance 的 ?。
        #    2026-10-05 实测代价：初版把 importance 参数写在 base 之前 ⇒
        #    IN 子句收到的是 0.8 这种数字（等于不过滤），importance 比较收到的是
        #    'perception'（CAST 成 0.0，恒真）⇒ 三档依次覆盖，最终**全部落到最长档 90**。
        #    是 tests/unit/test_review_schedule_sqlite.py::test_interval_written_matches_tier
        #    当场抓出来的（三档断言 30/60/90 全变 90）。
        n1 = 0
        for t in tiers:
            cur.execute(
                "UPDATE memories SET review_interval_days=? WHERE " + base +
                " AND (review_interval_days IS NULL OR CAST(review_interval_days AS INTEGER)<=0)"
                " AND CAST(COALESCE(importance,-1) AS REAL)>=?",
                (str(int(t["days"])),) + tuple(excl) + (float(t["min_imp"]),))
            n1 += cur.rowcount
        # 兜底档 = 策略 `fsrs.default_days`（不是 tiers[-1].days；见 load_default_days 的实测代价）
        floor_days = load_default_days()
        cur.execute(
            "UPDATE memories SET review_interval_days=? WHERE " + base +
            " AND (review_interval_days IS NULL OR CAST(review_interval_days AS INTEGER)<=0)",
            (str(floor_days),) + tuple(excl))
        n1 += cur.rowcount
        # ② 到期日：按刚写好的间隔（或既有间隔）算，只补空位
        n2 = 0
        for t in tiers:
            d = int(t["days"])
            cur.execute(
                "UPDATE memories SET next_review_at=? WHERE " + base +
                " AND (next_review_at IS NULL OR next_review_at='')"
                " AND CAST(COALESCE(importance,-1) AS REAL)>=?",
                (fmt(now + _dt.timedelta(days=d)),) + tuple(excl) + (float(t["min_imp"]),))
            n2 += cur.rowcount
        cur.execute(
            "UPDATE memories SET next_review_at=? WHERE " + base +
            " AND (next_review_at IS NULL OR next_review_at='')",
            (fmt(now + _dt.timedelta(days=floor_days)),) + tuple(excl))
        n2 += cur.rowcount
        con.commit()
    except Exception as e:  # noqa: BLE001
        con.rollback()
        con.close()
        return {"verdict": "FAILED", "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                "backup": res["backup"]}
    con.close()
    res.update(verdict="OK", interval_written=n1, next_written=n2)
    return res


def consume(store: str, budget: int, touch_access_count: bool = False) -> dict:
    """消费到期队列：按 importance DESC 取至多 budget 条，推进 next_review（×1.5 上限 180）。

    **默认不动 access_count**（见模块 docstring：那会把冷率读数做假）。
    """
    tiers, _ = load_tiers()
    excl, _ = load_engine_exclusions()
    if not tiers or not excl:
        return {"verdict": "INCONCLUSIVE", "error": "policy/exclusions unavailable"}
    floor_days = load_default_days()
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
           "budget": budget, "touch_access_count": bool(touch_access_count)}
    res["backup"] = backup_store(store)
    ph = ",".join("?" for _ in excl)
    base = "status='active' AND category NOT IN (%s)" % ph
    now = now_utc()
    con = _connect(store, write=True)
    try:
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in cur.execute(
            "SELECT memory_id FROM memories WHERE " + base +
            " AND next_review_at IS NOT NULL AND next_review_at<>'' AND next_review_at<=?"
            " ORDER BY CAST(COALESCE(importance,0) AS REAL) DESC LIMIT ?",
            tuple(excl) + (fmt(now), budget)).fetchall()]
        done = 0
        for mid in ids:
            row = cur.execute("SELECT review_interval_days FROM memories WHERE memory_id=?",
                              (mid,)).fetchone()
            nxt = advance_days(row[0] if row else floor_days)
            if touch_access_count:
                cur.execute(
                    "UPDATE memories SET next_review_at=?, access_count="
                    "COALESCE(access_count,0)+1 WHERE memory_id=?",
                    (fmt(now + _dt.timedelta(days=nxt)), mid))
            else:
                cur.execute("UPDATE memories SET next_review_at=? WHERE memory_id=?",
                            (fmt(now + _dt.timedelta(days=nxt)), mid))
            done += cur.rowcount
        con.commit()
    except Exception as e:  # noqa: BLE001
        con.rollback()
        con.close()
        return {"verdict": "FAILED", "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                "backup": res["backup"]}
    con.close()
    res.update(verdict="OK", picked=len(ids), reviewed=done)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只读体检）")
    ap.add_argument("--consume", action="store_true", help="消费到期队列")
    ap.add_argument("--budget", type=int, default=0, help="0=用策略里的 review_budget_per_day")
    ap.add_argument("--touch-access-count", action="store_true",
                    help="与 brain_cycle 对齐：复习时 access_count+1（会污染冷率读数）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    store, _src = resolve_store(a.store)
    a.store = store

    if a.consume:
        r = consume(a.store, a.budget or load_budget(), a.touch_access_count) if a.apply \
            else {"verdict": "DRY_RUN", "note": "加 --apply 才消费；本模式不改库",
                  "due_now": plan(a.store).get("due_now"),
                  "budget": a.budget or load_budget()}
    elif a.apply:
        r = apply_init(a.store)
    else:
        r = plan(a.store)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif a.apply or a.consume:
        # 写操作的读数形状与 plan() **不同**（没有 engine_exclude_categories 等字段）。
        # 2026-10-05 实测代价：初版把两种形状塞进同一个打印分支 ⇒ 写**已经成功提交**之后
        # 打印阶段 KeyError('engine_exclude_categories') 崩掉，退出码 1
        # —— 表现为「命令失败」，而实际上数据已经写好了。症状与真相相反，正是
        # §13.5「写侧的哑线」的镜像（这次是失败假象），故显式分开两条打印路径。
        print("== 复习调度（SQLite / 服务中的库）——写操作 ==")
        print("库：%s" % r.get("store"))
        print("[采样时刻] %s" % r.get("ts"))
        print("判定：%s" % r.get("verdict"))
        if r.get("error"):
            print("错误：%s" % r["error"])
        if r.get("backup"):
            print("备份：%s" % r["backup"])
        for k in ("interval_written", "next_written", "picked", "reviewed",
                  "budget", "touch_access_count"):
            if k in r:
                print("  %-20s %s" % (k, r[k]))
    else:
        print("== 复习调度（SQLite / 服务中的库）==")
        print("库：%s" % r.get("store"))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error") or r.get("note") or ""))
            if r.get("verdict") == "DRY_RUN":
                for k in ("due_now", "budget"):
                    print("  %-14s %s" % (k, r.get(k)))
                return 0
            return 2
        print("策略来源 %s；tiers=%s；兜底 %s 天"
              % (r["tiers_source"], r["tiers"], r.get("default_days")))
        print("引擎排除类目 %s（来源 %s）" % (r["engine_exclude_categories"],
                                     r["engine_exclude_source"]))
        print()
        print("active 总数                %d" % r["active_total"])
        print("retrieval_excluded_rows    %d（按 §13.0 不排期，显式报数）"
              % r["retrieval_excluded_rows"])
        print("可排期 active              %d" % r["schedulable_rows"])
        print("  间隔未初始化             %d" % r["interval_unset"])
        print("  到期日未初始化           %d" % r["next_unset"])
        print("  已排期                   %d" % r["next_set"])
        print("  **已到期**               %d" % r["due_now"])
        print("初始化后间隔分布（按策略） %s" % r["planned_interval_histogram"])
        print("策略日预算                 %d" % r["budget_policy"])
        if r["due_now"] > r["budget_policy"]:
            print("⚠ 到期 %d > 日预算 %d ⇒ 需 %d 天才能清空积压"
                  % (r["due_now"], r["budget_policy"],
                     -(-r["due_now"] // max(1, r["budget_policy"]))))
        if not a.apply:
            print()
            print("（只读体检。写库请加 --apply；写库前会自动备份到 ~/.trinity/store-backups/）")

    if r.get("verdict") == "OK" and a.apply:
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        out = a.out or os.path.join(
            ROOT, "output", "review_schedule_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print()
        print("产物：%s" % out)
    return 0 if r.get("verdict") in ("OK", "DRY_RUN") else 2


if __name__ == "__main__":
    sys.exit(main())
