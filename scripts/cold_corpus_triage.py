#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cold_corpus_triage.py — 灌入型冷库存的**归档候选筛选**（2026-09-21 §1031，默认 dry-run）

动机（实测）：U1 全局覆盖率 ≈16% 里，perception(6,684@3.96%) + kb-harvester(4,973@3.42%)
两家的 active 体量占 44.7%、近 30 天被读率 <5%（见 EXECUTION §1021 的 U1_attrib）。
它们不是"检索缺陷"，但也**不该无限占用 active 货架** —— 本脚本把"可归档的那部分"
选出来、量化、并**默认只报告**（--apply 才真的改 status，且先落 CSV 备份）。

判据（可失败，见 tests/unit/test_cold_corpus_triage.py）：
  · 被读过 ⇒ 不选；
  · 龄 < min-age-days ⇒ 不选（新写入还没机会被读到，不能当冷库存处理）；
  · importance > max-importance ⇒ 不选（高价值即使冷也要留）；
  · 受保护类别（session/decision/incident/procedural/identity-anchor/milestone…）⇒ 不选。

用法：
    python scripts/cold_corpus_triage.py                  # dry-run（只报告）
    python scripts/cold_corpus_triage.py --limit 500 --apply   # 真归档（可回滚：status 改回 active）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

#: 这些类别**永不**作为冷库存归档候选（人/决策/事故/流程/身份 —— 价值与冷热无关）
PROTECTED_CATEGORIES = {
    "session", "decision", "incident", "procedural", "identity-anchor",
    "milestone", "brain-proposal", "insight", "self-reflection", "ops",
}


# 2026-09-21（§1036）：**检索排除类目**（与搜索引擎同源，见 trinity/core/client/_search.py 的
# _RETRIEVAL_EXCLUDE_CATEGORIES）—— 它们**不参与语义检索**，所以 importance 对它们不代表
# "价值"，只代表"留多久"；对这类行**不适用 importance 门**（否则会给纯遥测数据一条
# 与可检索知识同款的保留门槛）。与引擎常量的漂移由测试看住。
# 2026-09-21（§1044）：口径收进唯一来源 scripts/caliber.py（此前三处各一份 ⇒ 三个漂移点）
from caliber import (  # noqa: E402 — 与本文件同目录（scripts/）
    RETRIEVAL_EXCLUDED_CATEGORIES,
    importance_gate_applies,
)


def select_candidates(rows, min_age_days=14.0, max_importance=0.5,
                      protected=None, retrieval_excluded=None) -> list:
    """纯函数：从候选行里挑出**可归档**的那些（判据见文件头）。

    2026-09-21 追加：类目属于 RETRIEVAL_EXCLUDED_CATEGORIES 时**跳过 importance 门** ——
    实测动机：perception 回填后落在 0.515（>0.5 的归档门），于是"钉死已解除、却一条都归档不了"；
    而 perception 本来就不参与检索（_RETRIEVAL_EXCLUDE_CATEGORIES），它的价值只在"新近"，
    不该拿可检索知识的门槛去卡它。**判据同源**：直接读引擎常量，不另立一份。
    """
    prot = PROTECTED_CATEGORIES if protected is None else protected
    retr = RETRIEVAL_EXCLUDED_CATEGORIES if retrieval_excluded is None else tuple(retrieval_excluded)
    out = []
    for r in rows or []:
        try:
            if r.get("retrieved"):
                continue
            if float(r.get("age_days") or 0) < float(min_age_days):
                continue
            cat = str(r.get("category") or "").strip().lower()
            if cat in prot:
                continue
            if cat not in retr and float(r.get("importance") or 0) > float(max_importance):
                continue
            out.append(r)
        except Exception:  # noqa: BLE001
            continue
    return out


def _pg():
    # t31：凭证走统一入口（env → yaml **refs** → 默认）。修前按**顶层键**取 ⇒ 版本化文件下恒空
    # ⇒ 密码空串、用户回落 postgres ⇒ 连接静默失败（异常被吞）。判据：test_credentials_readers_20261006
    import psycopg2
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"],
                            connect_timeout=5)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=float, default=14.0)
    ap.add_argument("--max-importance", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--apply", action="store_true", help="真的改 status=archived（默认只报告）")
    # 2026-09-21（§1036 实测的"测量改变被测"）：bulk_cold 的判定**依赖覆盖率**，
    # 而归档本身会提高覆盖率 ⇒ 归档 500 条后 perception 直接掉出 bulk_cold 名单，
    # 剩下 3,563 条候选**一次都选不出来**（复核显示"候选池 4972 / 可归档 0"）。
    # 故把生产者范围显式化：默认仍用分类器的 bulk_cold；要按既定范围继续处置时显式指定。
    ap.add_argument("--producers", default="",
                    help="逗号分隔的 agent_id（默认=分类器判定的 bulk_cold 生产者）")
    a = ap.parse_args(argv)

    import memory_utilization_audit as MUA
    conn = _pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("select coalesce(agent_id, '(null)') as producer, count(*) as active, "
                "count(*) filter (where last_retrieved_at > now() - interval '30 days') "
                "as hit_30d from memories where status='active' group by 1")
    rows = [{"producer": p, "active": n, "hit_30d": h} for p, n, h in cur.fetchall()]
    attrib = MUA.classify_attribution(rows)
    bulk = ([p.strip() for p in a.producers.split(",") if p.strip()]
            if a.producers else (attrib.get("bulk_cold_producers") or []))
    print("bulk_cold 生产者: %s（冷库存占 active %.1f%%）"
          % (bulk, 100 * (attrib.get("cold_corpus_share") or 0)))
    if not bulk:
        print("无需处置。")
        return 0

    cur.execute("select memory_id, category, importance, agent_id, "
                "extract(epoch from now() - created_at)/86400.0 as age_days, "
                "(last_retrieved_at is not null) as retrieved "
                "from memories where status='active' and agent_id = ANY(%s)", (bulk,))
    cand_rows = [{"memory_id": r0[0], "category": r0[1], "importance": r0[2],
                  "producer": r0[3], "age_days": round(float(r0[4] or 0), 1),
                  "retrieved": bool(r0[5])} for r0 in cur.fetchall()]
    picked = select_candidates(cand_rows, a.min_age_days, a.max_importance)
    # 2026-09-21（§1031）：**为什么只剩这么少**必须能回答（否则「候选 2 条」会被读成
    # 「冷库存没问题」）。按**首个命中的排除条件**分类计数 —— 这就是"货架为什么动不了"。
    _why = {"retrieved_ever": 0, "too_young": 0, "high_importance": 0,
            "protected_category": 0, "importance_gate_skipped(检索排除类目)": 0}
    for r0 in cand_rows:
        _cat = str(r0.get("category") or "").strip().lower()
        _excl = _cat in RETRIEVAL_EXCLUDED_CATEGORIES
        if r0.get("retrieved"):
            _why["retrieved_ever"] += 1
        elif float(r0.get("age_days") or 0) < a.min_age_days:
            _why["too_young"] += 1
        elif _cat in PROTECTED_CATEGORIES:
            _why["protected_category"] += 1
        elif (not _excl) and float(r0.get("importance") or 0) > a.max_importance:
            _why["high_importance"] += 1
        elif _excl and float(r0.get("importance") or 0) > a.max_importance:
            # 2026-09-21（§1036）：这些行**本来会被 importance 门拦下**，现在按同源判据放行 ——
            # 单独计数，免得读的人以为"高价值也没被保护"（它是"不参与检索，故不看价值"）。
            _why["importance_gate_skipped(检索排除类目)"] += 1
    print("排除原因（首个命中）:", json.dumps(_why, ensure_ascii=False))
    picked.sort(key=lambda r0: -r0["age_days"])
    print("候选池 %d 条 ⇒ 可归档 %d 条（龄 >=%sd、importance <=%s、未被读过、非受保护类别）"
          % (len(cand_rows), len(picked), a.min_age_days, a.max_importance))
    by_cat = {}
    for r0 in picked:
        k = "%s" % (r0.get("category") or "(null)")
        by_cat[k] = by_cat.get(k, 0) + 1
    print("按类别:", json.dumps(dict(sorted(by_cat.items(), key=lambda kv: -kv[1])[:8]),
                                 ensure_ascii=False))
    # 2026-09-21（§1031 实测发现）：**候选极少**（11,657 ⇒ 2）的真因不是「都读过」，而是
    # **冷库存很年轻** —— 冻结条件里的龄阈值把绝大多数挡在门外。故必须把**龄分布**打出来，
    # 否则读的人会以为「没得可归档」，实际是「该动的是写入侧配额，不是归档侧」。
    _buckets = {"<7d": 0, "7-14d": 0, "14-30d": 0, ">30d": 0}
    for r0 in cand_rows:
        _ag = float(r0.get("age_days") or 0)
        if _ag < 7:
            _buckets["<7d"] += 1
        elif _ag < 14:
            _buckets["7-14d"] += 1
        elif _ag < 30:
            _buckets["14-30d"] += 1
        else:
            _buckets[">30d"] += 1
    print("候选池龄分布:", json.dumps(_buckets, ensure_ascii=False),
          "⇒ 若 <7d 占多数，说明**活跃写入**才是冷库存的来源（归档侧无货可动）")
    print("最老的 3 条:", [(r0["memory_id"][:14], r0["category"], r0["age_days"])
                           for r0 in picked[:3]])
    if not a.apply:
        print("（dry-run：未改任何行；加 --apply 才归档，且先落 CSV 备份）")
        return 0
    todo = picked[: max(0, a.limit)]
    if not todo:
        print("--limit 为 0 ⇒ 不动作。")
        return 0
    out_dir = os.path.join(ROOT, "output")
    os.makedirs(out_dir, exist_ok=True)
    csv_p = os.path.join(out_dir, "cold_triage_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(csv_p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["memory_id", "category", "importance",
                                           "producer", "age_days"])
        w.writeheader()
        for r0 in todo:
            w.writerow({k: r0.get(k) for k in w.fieldnames})
    cur.execute("update memories set status='archived', updated_at=now() "
                "where memory_id = ANY(%s) and status='active'",
                ([r0["memory_id"] for r0 in todo],))
    print("已归档 %d 条（备份 %s）；回滚：把 status 改回 active" % (cur.rowcount, csv_p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())