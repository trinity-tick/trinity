#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""corpus_backfill_dryrun.py —— 历史语料回填工具（**默认 dry-run**；t4 / 2026-10-06）。

## 纪律（按任务书④）

1. **默认 dry-run**：不带 `--apply` 时只读源库、只算"将改动多少行 + 样例 10 条"，不写一个字节。
2. **先备份才允许 --apply**：`--backup <path>` 指向的备份必须通过
   `PRAGMA integrity_check` **且**行数与源库一致，否则拒绝执行（不是"提醒"，是拒绝）。
3. 再加一道显式令牌 `--confirm-token I-UNDERSTAND-PRODUCTION-WRITE`；
   三重门（apply + 备份校验 + 令牌）缺一即拒。
4. **本仓库本轮不执行 apply**：t4 不直接改生产数据（队长裁决：dry-run + 备份 + 队长决定）。
   备份实现参考 `trinity-hardening-20261006/tools/backup_store.py`（sqlite online backup API，
   在源库有并发写入时仍产生事务一致快照）。

## 三个作业

| 作业 | 干什么 | 为什么需要 |
|---|---|---|
| `dedup` | 把**重复组**里的非规范副本标成 `status='merged'` + `merged_into=<canonical>` | 实测 41,139 行冗余（35.90%），其中 34,897 行落在**全归档**重复组里（归档把去重契约洗白的存量） |
| `tags` | 按**显式** `--tag-map`（category → tags）给无标签行**建议**标签 | 无标签 30,506 行（26.62% 全库 / 19.85% active）；**工具不发明标签**：映射里没有的 category 一律不出建议 |
| `guard` | 把全库行回放进 `corpus_write_guard` 判据，报"会拦什么" | 写入侧拦截的**生产语料命中率**（不是单测里的构造样例） |

用法：
    python scripts/corpus_backfill_dryrun.py dedup  --json out.json
    python scripts/corpus_backfill_dryrun.py tags   --tag-map tagmap.json
    python scripts/corpus_backfill_dryrun.py guard  --json out.json
    python scripts/corpus_backfill_dryrun.py backup --backup-to D:/backups/store.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLY_TOKEN = "I-UNDERSTAND-PRODUCTION-WRITE"
SAMPLE_N = 10


def resolve_db(value: str) -> str:
    v = os.path.expanduser(value or "")
    if v and os.path.isdir(v):
        return os.path.join(v, "trinity_store.db")
    return v


def connect_ro(path: str):
    import sqlite3
    return sqlite3.connect("file:" + resolve_db(path).replace("\\", "/") + "?mode=ro", uri=True)


def connect_rw(path: str):
    import sqlite3
    return sqlite3.connect(resolve_db(path), timeout=30)


def load_write_guard():
    """按文件路径加载判据模块（避免 import `trinity` 包带来的 second_brain 初始化副作用）。"""
    import importlib.util
    p = os.path.join(REPO, "trinity", "memory", "corpus_write_guard.py")
    spec = importlib.util.spec_from_file_location("_corpus_write_guard_standalone", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_corpus_write_guard_standalone"] = mod
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────────────────────────────── 备份

def backup_store(src: str, dst: str) -> Dict[str, Any]:
    """sqlite online backup（只读源库）+ 完整性/行数校验。已存在则拒写（不覆盖历史备份）。"""
    import sqlite3
    src_f = resolve_db(src)
    if os.path.exists(dst):
        return {"ok": False, "error": "REFUSE: destination already exists: %s" % dst}
    if not os.path.exists(src_f):
        return {"ok": False, "error": "REFUSE: source missing: %s" % src_f}
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    t0 = time.time()
    s = sqlite3.connect("file:" + src_f.replace("\\", "/") + "?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst, timeout=60)
    s.backup(d)
    d.close()
    s.close()
    v = verify_backup(dst)
    v.update({"path": dst, "bytes": os.path.getsize(dst), "elapsed_s": round(time.time() - t0, 1)})
    return v


def verify_backup(path: str) -> Dict[str, Any]:
    """备份可用性的硬校验：integrity_check 必须 ok，且 memories / audit_log 行数可读。"""
    import sqlite3
    try:
        con = sqlite3.connect("file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro",
                              uri=True)
        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        mem = con.execute("SELECT count(*) FROM memories").fetchone()[0]
        aud = con.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        con.close()
        return {"ok": ic == "ok", "integrity_check": ic, "memories": mem, "audit_log": aud}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}


# ───────────────────────────────────────────────── dedup 计划

def plan_dedup(rows: Sequence[Dict[str, Any]], sample_n: int = SAMPLE_N) -> Dict[str, Any]:
    """纯函数：给定行（含 memory_id/status/persona_id/agent_id/sha256_hash/content_hash/
    access_count/importance/created_at），给出"把哪些非规范副本标成 merged"的计划。

    规范副本选择（确定性、可复算）：access_count DESC → importance DESC → created_at DESC
    → memory_id ASC（最后一级只为消除并列，保证同一输入永远同一输出）。
    **只处理非 active 行**：active 内的重复组实测为 0（唯一索引生效），且改 active 面
    会直接影响检索，超出"回填历史"的范围 ⇒ 计划里显式记 0 行、并由 `--apply` 再拒一次。
    """
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key_hash = r.get("content_hash") or r.get("sha256_hash")
        if not key_hash:
            continue                      # 无指纹的行**不猜**：不参与去重计划
        groups[(str(r.get("persona_id") or ""), str(r.get("agent_id") or ""), str(key_hash))].append(r)

    def rank(r: Dict[str, Any]) -> Tuple[int, int, float, str, str]:
        # 第一级是 **active 优先**：否则在同组里挑出的规范副本可能是归档行，而把 active
        # 行当受害者 —— 那会直接动检索面（本工具的红线）。实测未加这一级时
        # `skipped_active_rows` 高达 4,193（计划"想动"4,193 条 active 行，被硬边界挡下）。
        active_first = 0 if str(r.get("status")) == "active" else 1
        return (active_first, -int(r.get("access_count") or 0), -float(r.get("importance") or 0.0),
                str(r.get("created_at") or ""), str(r.get("memory_id") or ""))

    actions: List[Dict[str, Any]] = []
    skipped_active = 0
    groups_with_dup = 0
    per_group_first: List[Dict[str, Any]] = []
    for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(members) < 2:
            continue
        groups_with_dup += 1
        members = sorted(members, key=rank)
        canonical = members[0]
        for victim in members[1:]:
            if str(victim.get("status")) == "active":
                skipped_active += 1        # 硬边界：不碰 active 面
                continue
            actions.append({
                "memory_id": victim.get("memory_id"),
                "new_status": "merged",
                "merged_into": canonical.get("memory_id"),
                "reason": "duplicate_sha256",
                "group_key": "%s|%s|%s" % (key[0], key[1], key[2][:16]),
                "old_status": victim.get("status"),
                "content_preview": str(victim.get("content") or "")[:100],
            })
        if actions:
            per_group_first.append(actions[-1])
    # 样例取**每个组的第一条**（去相关：否则 10 条样例可能全来自同一个大组）
    samples = per_group_first[:sample_n]
    # 跨命名空间（同指纹但 persona/agent 不同）的重复——**本计划刻意不碰**：
    # 不同生产者可能各自合法地保存同一段外部内容，跨库归并需要独立判据（交队长决定）。
    by_hash_only: Dict[str, int] = defaultdict(int)
    for r in rows:
        h = r.get("content_hash") or r.get("sha256_hash")
        if h:
            by_hash_only[str(h)] += 1
    total_redundant_by_hash = sum(c - 1 for c in by_hash_only.values() if c > 1)
    return {
        "job": "dedup",
        #: `actions` 是**完整计划**（apply 用），`samples` 只是给报告看的 10 条。
        #: 序列化时 `_public_plan()` 会把 actions 摘掉（26k 条会把报告撑爆）。
        "actions": actions,
        "would_change": len(actions),
        "groups_with_duplicates": groups_with_dup,
        "skipped_active_rows": skipped_active,
        "total_redundant_rows_by_hash": total_redundant_by_hash,
        "cross_namespace_rows_left": max(0, total_redundant_by_hash - len(actions)),
        "cross_namespace_caliber": ("`total_redundant_rows_by_hash` 按**指纹单独**分组；"
                                    "`would_change` 只按 (persona_id, agent_id, 指纹) 同键归并。"
                                    "两者之差 = 同内容分散在**不同生产者/人格**下的行数，"
                                    "本计划刻意不碰（跨命名空间归并需要独立判据）。"),
        "by_old_status": dict(Counter(a["old_status"] for a in actions)),
        "by_agent": dict(Counter(str(a.get("group_key", "")).split("|")[1] for a in actions).most_common(10)),
        "samples": samples,
        "write_scope": "仅非 active 行：status → 'merged' + merged_into（保留审计链，不删行）",
    }


def apply_dedup(con, plan: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """执行（或演练）dedup 计划。`dry_run=True` 时**不执行任何 UPDATE**。"""
    if dry_run:
        return {"executed": 0, "dry_run": True}
    n = 0
    for a in plan["actions"]:
        cur = con.execute(
            "UPDATE memories SET status=?, merged_into=?, updated_at=? "
            "WHERE memory_id=? AND status<>'active'",
            (a["new_status"], a["merged_into"],
             time.strftime("%Y-%m-%dT%H:%M:%S"), a["memory_id"]))
        n += cur.rowcount
    con.commit()
    return {"executed": n, "dry_run": False}


# ───────────────────────────────────────────────── tags 计划

def plan_tags(rows: Sequence[Dict[str, Any]], tag_map: Dict[str, List[str]],
              sample_n: int = SAMPLE_N) -> Dict[str, Any]:
    """纯函数：按**显式映射**给无标签行建议标签。

    工具**不发明标签**：`category` 不在 `tag_map` 里 ⇒ 记进 `unmapped_categories`，
    **不出建议**（宁可少打，也不把"猜的标签"写进生产库）。已经有标签的行一律跳过。
    """
    def tagless(r: Dict[str, Any]) -> bool:
        t = r.get("tags")
        return t is None or str(t).strip() in ("", "[]", "{}")

    actions: List[Dict[str, Any]] = []
    unmapped: Counter = Counter()
    for r in rows:
        if not tagless(r):
            continue
        cat = str(r.get("category") or "")
        if cat not in tag_map:
            unmapped[cat] += 1
            continue
        actions.append({
            "memory_id": r.get("memory_id"),
            "category": cat,
            "old_tags": r.get("tags"),
            "new_tags": list(tag_map[cat]),
            "status": r.get("status"),
            "content_preview": str(r.get("content") or "")[:100],
        })
    return {
        "job": "tags",
        "actions": actions,
        "would_change": len(actions),
        "unmapped_rows": sum(unmapped.values()),
        "unmapped_categories": dict(unmapped.most_common(15)),
        "mapped_categories": sorted(tag_map),
        "by_status": dict(Counter(str(a["status"]) for a in actions)),
        "samples": actions[:sample_n],
        "write_scope": "仅 tags 为空/NULL/'[]' 的行：tags → JSON 数组（映射表显式给出）",
    }


def apply_tags(con, plan: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"executed": 0, "dry_run": True}
    n = 0
    for a in plan["actions"]:
        cur = con.execute(
            "UPDATE memories SET tags=?, updated_at=? WHERE memory_id=?",
            (json.dumps(a["new_tags"], ensure_ascii=False),
             time.strftime("%Y-%m-%dT%H:%M:%S"), a["memory_id"]))
        n += cur.rowcount
    con.commit()
    return {"executed": n, "dry_run": False}


# ───────────────────────────────────────────────── guard 回放计划

def plan_guard(rows: Sequence[Dict[str, Any]], sample_n: int = SAMPLE_N,
               mode: str = "annotate") -> Dict[str, Any]:
    """把全库行回放进写入侧判据，报"会拦什么、误杀在哪"。

    * W1（dup_archived_copy）用**真实的 hash→status 集合**（本函数内自建索引）；
    * W3（derivative_requota）用**真实的同内容历史写入次数**（按归一化内容计数）；
    * 误杀检查：被 W1 拦下的行里，**有多少曾经被读过**（`access_count>0`）——若这些行
      本来就是"重复的垃圾"，被读过的比例应当极低；>5% 就必须人工复核。
    """
    guard = load_write_guard()
    by_hash: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        h = r.get("content_hash") or r.get("sha256_hash")
        if h:
            by_hash[str(h)].append(str(r.get("status") or ""))
    seen: Counter = Counter()
    counts: Counter = Counter()
    would_samples: List[Dict[str, Any]] = []
    blocked_but_read = 0
    blocked = 0
    for r in rows:
        content = str(r.get("content") or "")
        h = str(r.get("content_hash") or r.get("sha256_hash") or "")
        norm_key = "%s|%s" % (r.get("agent_id"), guard.norm_text(content))
        recent = seen[norm_key]
        d = guard.evaluate_before_store(
            content,
            producer=str(r.get("agent_id") or ""),
            content_hash=h,
            existing_statuses=by_hash.get(h, ()),
            recent_identical_writes=recent,
            mode=mode,
            log=False,
        )
        seen[norm_key] += 1
        if d.would_block:
            counts[d.code] += 1
            if int(r.get("access_count") or 0) > 0:
                blocked_but_read += 1
            if len(would_samples) < sample_n:
                would_samples.append({
                    "memory_id": r.get("memory_id"), "code": d.code,
                    "agent_id": r.get("agent_id"), "status": r.get("status"),
                    "access_count": r.get("access_count"), "detail": d.detail[:160],
                    "content_preview": content[:100],
                })
            if not d.allow:
                blocked += 1
    total = len(rows)
    would = sum(counts.values())
    return {
        "job": "guard",
        "mode": mode,
        "rows_replayed": total,
        "would_block": would,
        "would_block_rate": round(would / total, 4) if total else 0.0,
        "blocked_now": blocked,
        "by_code": dict(counts.most_common()),
        "false_positive_check": {
            "blocked_but_ever_read": blocked_but_read,
            "rate_in_would_block": round(blocked_but_read / would, 4) if would else 0.0,
            "caliber": ("被拦下的行里 access_count>0 的比例。W1 拦的是**同内容重复副本**，"
                        "历史读数是【这一组内容被读过】，不直接等于【这一行有价值】 ⇒ "
                        "该比例只作**上界**，需要人工复核的阈值由队长定。"),
        },
        "samples": would_samples,
    }


# ───────────────────────────────────────────────── CLI

def _load_rows(con, job: str, limit: int) -> List[Dict[str, Any]]:
    cols = {
        "dedup": "memory_id,status,persona_id,agent_id,sha256_hash,content_hash,"
                 "access_count,importance,created_at,substr(content,1,120) as content",
        "tags": "memory_id,status,category,tags,substr(content,1,120) as content",
        "guard": "memory_id,status,agent_id,access_count,sha256_hash,content_hash,content",
    }[job]
    sql = "select %s from memories" % cols
    if limit:
        sql += " limit %d" % int(limit)
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _require_backup(path: str, src: str) -> Tuple[bool, Dict[str, Any]]:
    """三重门的第二道：备份必须存在、integrity_check=ok、memories 行数 ≥ 源库。"""
    if not path:
        return False, {"error": "REFUSE: --apply 必须同时给 --backup <已校验的备份路径>"}
    if not os.path.exists(path):
        return False, {"error": "REFUSE: backup missing: %s" % path}
    v = verify_backup(path)
    if not v.get("ok"):
        return False, {"error": "REFUSE: 备份未通过校验", "verify": v}
    try:
        src_rows = connect_ro(src).execute("select count(*) from memories").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        return False, {"error": "REFUSE: 源库不可读: %s" % e}
    if int(v.get("memories") or 0) < int(src_rows):
        return False, {"error": "REFUSE: 备份比源库旧（%s < %s）"
                                % (v.get("memories"), src_rows), "verify": v}
    return True, {"verify": v, "source_rows": src_rows}


def _public_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """给报告用的计划视图：摘掉完整 `actions`（可能几万条），保留计数与 10 条样例。"""
    pub = {k: v for k, v in plan.items() if k != "actions"}
    if "actions" in plan:
        pub["actions_total_in_plan"] = len(plan["actions"])
        pub["actions_omitted_from_report"] = True
    return pub


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Trinity 语料回填（默认 dry-run）")
    ap.add_argument("job", choices=("dedup", "tags", "guard", "backup"))
    ap.add_argument("--db", default=os.environ.get("TRINITY_STORE", DEFAULT_DB))
    ap.add_argument("--apply", action="store_true", help="真的写库（需备份校验 + 令牌）")
    ap.add_argument("--backup", default="", help="--apply 时必填：已校验的备份文件")
    ap.add_argument("--backup-to", default="", help="backup 作业的输出路径")
    ap.add_argument("--confirm-token", default="")
    ap.add_argument("--tag-map", default="", help="tags 作业：JSON {category: [tags]}")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    args.db = resolve_db(args.db)

    if args.job == "backup":
        if not args.backup_to:
            print("REFUSE: backup 需要 --backup-to <path>", file=sys.stderr)
            return 2
        out = backup_store(args.db, args.backup_to)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("ok") else 2

    con = connect_ro(args.db)
    rows = _load_rows(con, args.job, args.limit)
    total = con.execute("select count(*) from memories").fetchone()[0]

    if args.job == "dedup":
        plan = plan_dedup(rows)
    elif args.job == "tags":
        tag_map: Dict[str, List[str]] = {}
        if args.tag_map:
            with open(args.tag_map, encoding="utf-8") as fh:
                tag_map = json.load(fh)
        plan = plan_tags(rows, tag_map)
    else:
        plan = plan_guard(rows, mode="on" if args.apply else "annotate")

    apply_requested = bool(args.apply) and args.job in ("dedup", "tags")
    gate = {"apply_requested": apply_requested, "dry_run": not apply_requested}
    if apply_requested:
        ok, why = _require_backup(args.backup, args.db)
        if not ok or args.confirm_token != APPLY_TOKEN:
            gate["refused"] = why if not ok else {
                "error": "REFUSE: --confirm-token 不等于 %s" % APPLY_TOKEN}
            apply_requested = False
            gate["dry_run"] = True
        else:
            gate["backup_check"] = why
            plan["_apply_result"] = (
                apply_dedup(connect_rw(args.db), plan, False) if args.job == "dedup"
                else apply_tags(connect_rw(args.db), plan, False))
    con.close()

    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "job": args.job,
        "db": args.db,
        "db_rows_total": total,
        "rows_scanned": len(rows),
        "readonly": not apply_requested,
        **gate,
        "plan": _public_plan(plan),
    }
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "plan"}, ensure_ascii=False, indent=1))
    print("\n[plan] would_change=%s" % plan.get("would_change",
                                                plan.get("would_block")))
    for it in plan.get("samples", []):
        print("  - %s" % json.dumps(it, ensure_ascii=False)[:220])
    if args.json:
        print("-> %s" % args.json)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/corpus_backfill_dryrun.py::<module>")
    raise SystemExit(main())
