#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 doc2query 的**留出问题**转成 doc 域评测可用的题集（Step B 扩样本）。

## 为什么要它：现有题集**结构性偏袒词法**

现有 `eval/doc_golden_set_auto.json` 的 query 由**文档正文第二长句**派生
（见 `AGENTS.md` 与 `scripts/docs_golden_auto.py` 的说明）。这种题集的 query 里
**天然含有被检索文档的原词** ⇒ 零依赖 BM25（B1/B2 臂）**几乎必然占优**。
2026-10-05 实测：该题集上 `B2 R@10=0.95` **高于**生产混合检索 `A R@10=0.8917`
—— 但这个"B2 胜 A"的结论**可能只是题集构造的产物**，不能直接用来决定"是否把 B2 提为默认"。

本工具换用**任务形状、无词法泄漏**的题集：doc2query 为每条记忆生成的
「它回答什么问题」问句里，**留出的那 1 条**（索引从未见过），
且生成时被明确要求**避免复述正文专有名词**。

## 口径对齐（关键，否则结论无效）

`doc_retrieval_eval.py` 的语料是 **`persona_id='trinity-docs'`**（仓内 `docs/*.md`），
而库内绝大多数冷行是 `persona='default'`（`kb_harvested` 6,046 条）。**两者是不同语料**。
⇒ 本工具**只取 `persona='trinity-docs'`**，并用其 `metadata.source_file` 作 target。
若不限定 persona，题集的 target 文档**不在评测语料内**，两把尺子混用会得出无效结论。

## 纪律
- **只读**：不改库。
- **§13.2**：解析不出 target 的行**按原因分档计数**，绝不静默丢弃。
- 产物落 `output/`（§12）。

用法：
    python scripts/build_heldout_golden.py --persona trinity-docs --limit 250
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
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/build_heldout_golden.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")


def resolve_store(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("TRINITY_STORE")
    if env:
        return env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")
    return DEFAULT_STORE


def basename_of(x) -> str:
    """纯函数：路径 -> 文档基名（与 `doc_retrieval_eval.py::basename` 同义）。可单测。"""
    return os.path.basename(str(x).replace("\\", "/")) if x else ""


def resolve_target(source_uri, metadata) -> "tuple[str, str]":
    """纯函数：从 (source_uri, metadata) 解析 target 文档基名。

    返回 (target, reason)。target 非空即成功；否则 reason 说明**为什么解析不出**
    （§13.2：分档计数，不许合并成 `skipped`）。可单测。
    """
    b = basename_of(source_uri)
    if b:
        return b, "ok:source_uri"
    if not metadata:
        return "", "no_metadata"
    try:
        md = json.loads(metadata) if isinstance(metadata, str) else metadata
    except Exception:  # noqa: BLE001
        return "", "metadata_unparsable"
    if not isinstance(md, dict):
        return "", "metadata_not_dict"
    b = basename_of(md.get("source_file") or md.get("source_uri"))
    if not b:
        return "", "no_source_file_in_metadata"
    return b, "ok:metadata"


def build(store: str, persona: str, limit: int, require_md: bool = True) -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store,
           "persona": persona, "limit": limit}
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    try:
        rows = con.execute(
            "SELECT h.memory_id, h.question, m.source_uri, m.metadata "
            "FROM memories_doc2query_holdout h JOIN memories m ON m.memory_id = h.memory_id "
            "WHERE m.persona_id=? AND m.status='active' AND h.question IS NOT NULL "
            "AND h.question<>'' LIMIT ?", (persona, limit)).fetchall()
        out["holdout_rows_scanned"] = len(rows)
        items, reasons, seen_q = [], {}, set()
        for mid, q, src, meta in rows:
            target, why = resolve_target(src, meta)
            if not target:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            if require_md and not target.lower().endswith(".md"):
                reasons["not_markdown"] = reasons.get("not_markdown", 0) + 1
                continue
            if q in seen_q:
                reasons["duplicate_query"] = reasons.get("duplicate_query", 0) + 1
                continue
            seen_q.add(q)
            items.append({"id": "hq%03d" % len(items), "type": "heldout-task",
                          "query": q, "target": target, "source_memory_id": mid})
        out["items"] = len(items)
        out["unresolved_by_reason"] = reasons
        out["distinct_targets"] = len({x["target"] for x in items})
        # 判据（可失败）：题集必须非空、且每条都有 .md target
        bad = [x["id"] for x in items if not x["target"].lower().endswith(".md")]
        out["bad_targets"] = len(bad)
        out["_items"] = items
    except Exception as e:  # noqa: BLE001
        con.close()
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    con.close()
    out["verdict"] = "OK" if (out["items"] > 0 and out["bad_targets"] == 0) else "FAILED"
    if out["verdict"] == "FAILED":
        out["error"] = "题集为空或存在非 .md target"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    store = resolve_store(a.store)
    r = build(store, a.persona, a.limit)
    items = r.pop("_items", [])
    if r.get("verdict") != "OK":
        print("判定：%s %s" % (r.get("verdict"), r.get("error")))
        return 2
    out = a.out or os.path.join(ROOT, "eval", "doc_golden_set_heldout.json")
    # 2026-10-05 实测：`doc_retrieval_eval.py:434` 读的是 `golden["items"]`
    # ⇒ 必须写**字典**形式（与 `doc_golden_set.json` / `doc_golden_set_auto.json` 一致）。
    # 初版写裸列表 ⇒ `TypeError: list indices must be integers` 当场崩。
    payload = {
        "provenance": {
            "built_by": "scripts/build_heldout_golden.py",
            "built_at": r["ts"],
            "persona": r["persona"],
            "source": "memories_doc2query_holdout —— doc2query **留出**问题"
                      "（建索引时用其余问题，测试用这一条 ⇒ 索引从未见过它）",
            "why_task_shaped": "生成时明确要求避免复述正文专有名词 ⇒ 相对 "
                               "doc_golden_set_auto（query 由正文第二长句派生、天然偏袒词法）"
                               "**词法泄漏更少**；用于检验『零依赖 BM25 胜生产混合检索』"
                               "是否只是题集构造的产物",
            "corpus": "persona_id='trinity-docs'（与 doc_retrieval_eval 的语料同源）",
            "metric": "文档级 R@k（target = metadata.source_file 基名）",
            "holdout_rows_scanned": r["holdout_rows_scanned"],
            "unresolved_by_reason": r["unresolved_by_reason"],
        },
        "items": items,
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== 留出问题题集（任务形状、无词法泄漏）==")
        print("库：%s；persona=%s" % (store, r["persona"]))
        print("扫描留出行 %d ⇒ 成题 %d 条（覆盖 %d 个不同文档）"
              % (r["holdout_rows_scanned"], r["items"], r["distinct_targets"]))
        print("解析失败分档（§13.2 不合并）：%s" % (r["unresolved_by_reason"] or "{}"))
        print("非 .md target：%d（必须 0）" % r["bad_targets"])
        print("产物：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
