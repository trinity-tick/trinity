#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`metadata.source_file` 缺口审计 + 门控回填（修「doc 域只占 13%」的根因）。

## 背景（2026-10-06 实测）

doc 域评测的 target 是 `metadata.source_file` 的基名。全库 active 27,380 行里只有
**3,569 行（13.0%）**有它 —— 于是无作用域检索的 top-k 被另外 87% 填满（实测 69% 槽位空基名），
doc 域召回几乎为 0。

**但缺口里有一部分是真数据缺口，不是设计如此**：
| 分类 | 行数 |
|---|---|
| 无 `source_file` 且**无** `source_uri` | 17,027（perception/procedural/episodic/session… **本来就不是文档**） |
| 无 `source_file` 但**有** `source_uri` | **6,784（kb_harvested 6,729 等 —— 它们是真实文件的行！）** |

这些行的 `metadata` 形如 `['source_uri','mtime','size','ext']` —— **有路径却没有 `source_file` 这个键**，
而文档级设施（`doc_retrieval_eval` / `/knowledge/sources` / page-tree 分组）读的正是 `source_file`。
⇒ 把它们补上，source_file 覆盖率会从 13.0% 升到约 37.8%，**6,784 条文档行重新对文档级设施可见**。

## 纪律
- **默认 dry-run**：不加 `--apply` 只报数、绝不写。
- **只补 metadata 键**，不改 `content`、不改任何其它列；**已存在 `source_file` 的行不动**（幂等）。
- **写入前落回滚清单**到 `output/`（§12），含 memory_id + 旧 metadata，可逆。
- **§13.5 写入侧静音行**：本工具不碰检索侧开关。
- 路径基线：以文件名为准（与 `doc_retrieval_eval.basename` 同义）。

用法：
    python scripts/backfill_source_file.py                 # 审计 + dry-run
    python scripts/backfill_source_file.py --json
    python scripts/backfill_source_file.py --apply         # 真正写（需显式）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/backfill_source_file.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")
MISSING = "COALESCE(json_extract(metadata,'$.source_file'),'')=''"
#: 2026-10-06 实测修正：**候选筛选必须同时看列 `source_uri`**。
#: 初版只查 `metadata.source_uri` ⇒ dry-run 只报 **163** 个候选，
#: 而实际有列 `source_uri` 的行是 **6,784** —— 大量行的路径存在**列**里而非 metadata 里，
#: 初版把这些行**全漏掉了**（`plan_row` 本来就会回退到列，但筛选阶段就把它们排除了）。
#: 这类"筛选条件比回填逻辑窄"的漏数最危险：dry-run 会给出一个**偏小**的数，
#: 看起来"改动很小、很安全"，从而误导是否执行的判断。
HAS_URI = ("(COALESCE(json_extract(metadata,'$.source_uri'),'')<>'' "
           "OR COALESCE(source_uri,'')<>'')")


def resolve_store(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("TRINITY_STORE")
    if env:
        return env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")
    return DEFAULT_STORE


def basename_of(x) -> str:
    """纯函数：路径 -> 基名（与 doc_retrieval_eval.basename 同义）。可单测。"""
    return os.path.basename(str(x).replace("\\", "/")) if x else ""


def plan_row(metadata, source_uri) -> "tuple[str, str]":
    """纯函数：决定这一行回填什么。返回 (new_source_file, reason)。可单测。

    - 已有 `source_file` ⇒ (`""`, "already_present")，**不动**；
    - 有 `metadata.source_uri` 能取到基名 ⇒ (基名, "ok:metadata.source_uri")；
    - 否则退化用列 `source_uri` ⇒ (基名, "ok:column.source_uri")；
    - 都取不到 ⇒ (`""`, 具体原因)。**原因分档，不合并**（§13.2）。
    """
    try:
        md = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
    except Exception:  # noqa: BLE001
        return "", "metadata_unparsable"
    if not isinstance(md, dict):
        return "", "metadata_not_dict"
    if str(md.get("source_file") or "").strip():
        return "", "already_present"
    b = basename_of(md.get("source_uri"))
    if b:
        return b, "ok:metadata.source_uri"
    b = basename_of(source_uri)
    if b:
        return b, "ok:column.source_uri"
    return "", "no_uri"


def audit(store: str) -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store}
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    try:
        # 2026-10-06（t74/I14）：原为 **lambda 赋值 + E731 抑制注释** ⇒ 改成 `def`（真修，不再靠抑制）。
        def q(s):
            return con.execute(s).fetchone()[0]
        total = q("SELECT COUNT(*) FROM memories WHERE status='active'")
        has_sf = q("SELECT COUNT(*) FROM memories WHERE status='active' AND NOT (%s)" % MISSING)
        miss = q("SELECT COUNT(*) FROM memories WHERE status='active' AND %s" % MISSING)
        cand = q("SELECT COUNT(*) FROM memories WHERE status='active' AND %s AND %s"
                 % (MISSING, HAS_URI))
        out.update(active_total=total, has_source_file=has_sf, missing_source_file=miss,
                   backfill_candidates=cand,
                   non_document=miss - cand)
        out["source_file_coverage_before"] = round(has_sf / float(total or 1), 4)
        out["source_file_coverage_after_est"] = round((has_sf + cand) / float(total or 1), 4)
        out["candidates_by_persona_category"] = [
            {"persona": r[0], "category": r[1], "n": r[2]}
            for r in con.execute(
                "SELECT persona_id, category, COUNT(*) n FROM memories "
                "WHERE status='active' AND %s AND %s GROUP BY 1,2 ORDER BY n DESC LIMIT 12"
                % (MISSING, HAS_URI))]
        # 逐行计划 + 原因分档
        reasons, samples = {}, []
        for mid, meta, uri in con.execute(
                "SELECT memory_id, metadata, source_uri FROM memories "
                "WHERE status='active' AND %s AND %s" % (MISSING, HAS_URI)):
            nb, why = plan_row(meta, uri)
            reasons[why] = reasons.get(why, 0) + 1
            if nb and len(samples) < 8:
                samples.append({"memory_id": mid, "source_file": nb, "why": why})
        out["plan_reasons"] = reasons
        out["samples"] = samples
        out["writable"] = sum(v for k, v in reasons.items() if k.startswith("ok:"))
    except Exception as e:  # noqa: BLE001
        con.close()
        out.update(verdict="INCONCLUSIVE",
                   error="audit failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    con.close()
    out["verdict"] = "OK"
    return out


def apply_backfill(store: str, limit: int = 0) -> dict:
    """真正写入。**只补 metadata.source_file**，先落回滚清单。"""
    a = audit(store)
    if a.get("verdict") != "OK":
        return a
    con = sqlite3.connect(store, timeout=120)
    con.execute("PRAGMA busy_timeout=110000")
    rows = con.execute(
        "SELECT memory_id, metadata, source_uri FROM memories "
        "WHERE status='active' AND %s AND %s" % (MISSING, HAS_URI)).fetchall()
    if limit:
        rows = rows[:limit]
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    rb = os.path.join(ROOT, "output", "backfill_source_file_%s.rollback.json"
                      % time.strftime("%Y%m%d_%H%M%S"))
    changed, skipped, manifest = 0, 0, []
    stat = {}
    try:
        for mid, meta, uri in rows:
            nb, why = plan_row(meta, uri)
            stat[why] = stat.get(why, 0) + 1
            if not nb:
                skipped += 1
                continue
            try:
                md = json.loads(meta) if isinstance(meta, str) else (meta or {})
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if not isinstance(md, dict):
                skipped += 1
                continue
            manifest.append({"memory_id": mid, "old_metadata": meta})
            md["source_file"] = nb
            cur = con.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                              (json.dumps(md, ensure_ascii=False), mid))
            changed += cur.rowcount
        con.commit()
    finally:
        con.close()
    with open(rb, "w", encoding="utf-8") as fh:
        json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
                   "changed": changed, "rows": manifest}, fh, ensure_ascii=False, indent=1)
    a.update(applied=True, changed=changed, skipped=skipped, apply_reasons=stat,
             rollback_manifest=rb)
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只 dry-run）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    store = resolve_store(a.store)
    r = apply_backfill(store, a.limit) if a.apply else audit(store)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("verdict") == "OK" else 2
    print("== metadata.source_file 缺口审计 ==")
    print("库：%s" % store)
    if r.get("verdict") != "OK":
        print("判定：%s %s" % (r.get("verdict"), r.get("error")))
        return 2
    print("active 总数 %d；有 source_file %d（%.1f%%）；缺 %d"
          % (r["active_total"], r["has_source_file"],
             100 * r["source_file_coverage_before"], r["missing_source_file"]))
    print("  其中**有 source_uri（真文档，可回填）** %d" % r["backfill_candidates"])
    print("  其中无 source_uri（本就非文档）       %d" % r["non_document"])
    print("可回填（解析得出基名）                %d" % r["writable"])
    print("回填后 source_file 覆盖估计：%.1f%% → %.1f%%"
          % (100 * r["source_file_coverage_before"], 100 * r["source_file_coverage_after_est"]))
    print()
    print("计划原因分档（§13.2 不合并）：%s" % r["plan_reasons"])
    print("按 persona/category：")
    for x in r["candidates_by_persona_category"][:6]:
        print("   %-14s %-16s %d" % (x["persona"], x["category"], x["n"]))
    print("样例：")
    for s in r["samples"][:5]:
        print("   %-28s -> %-46s (%s)" % (s["memory_id"][:28], s["source_file"][:46], s["why"]))
    print()
    if r.get("applied"):
        print("已写入 %d 行；跳过 %d；回滚清单 %s" % (r["changed"], r["skipped"],
                                                    r["rollback_manifest"]))
    else:
        print("**dry-run**：未写入任何行。要执行加 --apply（会先落回滚清单）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
