#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从**文档结构**（`metadata.section` 标题）生成任务形状题集 —— 换生成输入的复现。

## 为什么这算「换生成输入」，以及它**不是**什么

已有的三套题集里，`doc_golden_set_heldout.json` 的 query 由 **doc2query 留出问题**构成
（生成输入 = 记忆**正文**）。本工具换一个生成输入：**只给 LLM 该文档的章节标题列表**，
让它产出用户会问的任务形状问题。

⇒ 生成输入从「正文散文」换成「文档结构」。**但这仍不是完全独立的复现**：
查询依然源自被评测文档自身，**词法偏袒无法根除**。真正独立的复现需要
**人工撰写或外部来源**的查询集。此处如实标注为「不同生成输入」。

## 纪律
- **只读库**；只写 `eval/` 下的题集与 `output/` 下的证据。
- **可续跑**：逐篇把结果追加进 JSONL 缓存，重跑时跳过已完成的文档
  （实测教训：n=548 那次评测中途死掉、成果全丢）。
- **§13.2**：LLM 失败 / 解析失败 / 无章节，**分档计数**。
- 产物：`eval/doc_golden_set_section.json`（字典 + provenance，与既有题集同格式）。

用法：
    python scripts/build_section_golden.py --persona trinity-docs --per-doc 1
    python scripts/build_section_golden.py --persona trinity-docs --per-doc 1 --assemble-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/build_section_golden.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "output", "section_golden_cache.jsonl")


def resolve_store(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("TRINITY_STORE")
    if env:
        return env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")
    return os.path.join(os.path.expanduser("~"), ".trinity",
                        "store-restored", "trinity_store.db")


def basename_of(x) -> str:
    return os.path.basename(str(x).replace("\\", "/")) if x else ""


def load_sections(store: str, persona: str) -> dict:
    """{doc_basename: [section_titles...]}（去重保序，只读）。"""
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    docs = defaultdict(list)
    seen = defaultdict(set)
    try:
        for rel, sec in con.execute(
                "SELECT json_extract(metadata,'$.source_file') rel, "
                "json_extract(metadata,'$.section') sec FROM memories "
                "WHERE persona_id=? AND status='active' "
                "AND json_extract(metadata,'$.source_file') IS NOT NULL "
                "AND COALESCE(json_extract(metadata,'$.section'),'')<>'' "
                "ORDER BY rel, created_at", (persona,)):
            b = basename_of(rel)
            s = (sec or "").strip()
            if b and s and s not in seen[b]:
                seen[b].add(s)
                docs[b].append(s)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/build_section_golden.py::load_sections")
    con.close()
    return dict(docs)


def parse_questions(raw: str, k: int) -> list:
    """把 LLM 输出解析成 k 个问题。纯函数、可单测。

    容忍 JSON 数组 / 编号列表 / 纯行；去重、长度过滤。**解析不出就返回空**（由调用方计数）。
    """
    if not raw:
        return []
    txt = raw.strip()
    out = []
    m = re.search(r"\[.*\]", txt, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                out = [str(x).strip() for x in arr]
        except Exception:  # noqa: BLE001
            out = []
    if not out:
        for line in txt.splitlines():
            s = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|\(\d+\))\s*", "", line).strip()
            if s and not s.startswith("{") and not s.startswith("["):
                out.append(s)
    seen, clean = set(), []
    for q in out:
        q = q.strip().strip('"').strip("'").strip()
        q = re.sub(r"^\s*\d+[.、)]\s*", "", q).strip()
        if len(q) < 6 or len(q) > 200 or q in seen:
            continue
        seen.add(q)
        clean.append(q)
    return clean[:k]


PROMPT = (
    "下面是一个技术文档的**章节标题**列表（只有标题，没有正文）。\n"
    "请写 {k} 个**中文问句**，代表用户会提出的**任务/意图**，而这份文档恰好能回答它。\n"
    "要求：\n"
    "1. 问句要描述**用户想做什么**，不要是标题的复述或关键词堆砌；\n"
    "2. **不要直接照抄标题里的专有名词**，尽量用意图描述；\n"
    "3. 每个 10~40 字，彼此不重复。\n"
    "只输出 JSON 数组，不要解释。\n\n标题：\n{titles}"
)


def gen_for_doc(titles: list, k: int) -> list:
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import brain_cycle  # noqa: E402
    body = "\n".join("- " + t for t in titles[:40])
    raw = brain_cycle.llm(PROMPT.format(k=k, titles=body), max_tokens=500)
    return parse_questions(raw, k)


def load_cache() -> dict:
    done = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                    if r.get("doc"):
                        done[r["doc"]] = r
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/build_section_golden.py::load_cache")
    return done


def append_cache(rec: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def assemble(persona: str, items: list) -> str:
    out = os.path.join(ROOT, "eval", "doc_golden_set_section.json")
    payload = {"provenance": {
        "built_by": "scripts/build_section_golden.py",
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "persona": persona,
        "generation_input": "**文档结构**（metadata.section 章节标题列表），**不是正文**",
        "why": "换生成输入的复现：已有题集的 query 由 doc2query 留出问题（生成自**正文**）构成；"
               "本题集只用标题列表生成 ⇒ 生成输入不同",
        "NOT_fully_independent": "查询仍源自被评测文档自身 ⇒ **词法偏袒无法根除**；"
                                 "真正的独立复现需人工撰写或外部来源的查询集",
        "corpus": "persona_id='trinity-docs'（与 doc_retrieval_eval 同语料）",
        "metric": "文档级 R@k（target = metadata.source_file 基名）",
    }, "items": items}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--persona", default="trinity-docs")
    ap.add_argument("--per-doc", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="0=全部文档")
    ap.add_argument("--assemble-only", action="store_true")
    ap.add_argument("--reset-cache", action="store_true")
    a = ap.parse_args()
    store = resolve_store(a.store)
    if a.reset_cache and os.path.exists(CACHE):
        os.remove(CACHE)
    docs = load_sections(store, a.persona)
    names = sorted(docs)
    if a.limit:
        names = names[: a.limit]
    stats = {"ok": 0, "llm_error": 0, "no_questions": 0, "already_cached": 0,
             "no_sections": 0}
    if not a.assemble_only:
        done = load_cache()
        for i, d in enumerate(names, 1):
            if d in done:
                stats["already_cached"] += 1
                continue
            titles = docs.get(d) or []
            if not titles:
                stats["no_sections"] += 1
                continue
            try:
                qs = gen_for_doc(titles, a.per_doc)
            except Exception:  # noqa: BLE001  §13.2 分档
                stats["llm_error"] += 1
                append_cache({"doc": d, "questions": [], "error": "llm_error"})
                continue
            if not qs:
                stats["no_questions"] += 1
                append_cache({"doc": d, "questions": [], "error": "no_questions"})
                continue
            stats["ok"] += 1
            append_cache({"doc": d, "questions": qs, "n_sections": len(titles)})
            if i % 20 == 0:
                print("  ... %d/%d" % (i, len(names)))
    # 组装
    done = load_cache()
    items = []
    for d in sorted(done):
        for j, q in enumerate(done[d].get("questions") or []):
            items.append({"id": "sec%04d" % len(items), "type": "section-task",
                          "query": q, "target": d})
    out = assemble(a.persona, items)
    print("== 从文档结构生成题集 ==")
    print("库：%s；persona=%s" % (store, a.persona))
    print("文档 %d；本轮 ok=%d（已缓存 %d / LLM失败 %d / 无题 %d / 无章节 %d）"
          % (len(names), stats["ok"], stats["already_cached"], stats["llm_error"],
             stats["no_questions"], stats["no_sections"]))
    print("成题 %d 条，覆盖 %d 个文档" % (len(items), len({x['target'] for x in items})))
    print("产物：%s" % out)
    return 0 if items else 2


if __name__ == "__main__":
    sys.exit(main())
