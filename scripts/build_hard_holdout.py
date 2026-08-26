# -*- coding: utf-8 -*-
"""build_hard_holdout.py — 生产难查询 holdout 集构建（二轮优化 Task 2）。

从生产大库抽"自包含事实"记忆（wms_knowledge/decision/ai_knowledge/knowledge/
optimization + 领域标签），用 LLM 生成**近义改写**难查询（不共享表层词），
过滤"不够难"的查询（词重叠 >= 40% 剔除），产出可复用评测集。

用法:
    python scripts/build_hard_holdout.py --n 120 --seed 42 [--out output/hard_holdout.json]

产物: {version, built_at, items: [{id, category, tags, fact, query, overlap}]}
"""
import argparse
import json
import os
import random
import re
import sys
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DEFAULT_OUT = os.path.join(ROOT, "output", "hard_holdout.json")

# 目标类目/标签（自包含事实密集的领域记忆，排除文档大块/基准语料）
TARGET_CATEGORIES = {"wms_knowledge", "decision", "ai_knowledge", "knowledge", "optimization", "insight"}
TARGET_TAGS = {"wms", "技术教训", "项目决策", "仓库管理", "供应链管理", "trinity"}
EXCLUDE_CATEGORIES = {"lme", "stress-test", "test", "imported", "doc:general", "doc:summary",
                      "doc:plan", "doc:ops", "doc:benchmark"}

GEN_SYSTEM = (
    "You are creating a hard retrieval benchmark. Given a FACT stored in a memory system, "
    "write a NATURAL user question that asks for exactly this fact, but:\n"
    "1. Do NOT copy the fact's distinctive words verbatim — paraphrase with synonyms, "
    "restructure, and use casual phrasing (e.g. ask '上次聊的那个仓库上架流程怎么弄来着' "
    "instead of 'WMS 上架作业使用 PDA 扫码确认库位').\n"
    "2. The question must be answerable ONLY from this fact.\n"
    "3. Reply with ONLY the question, no preamble."
)


def load_credentials(path=os.path.expanduser("~/.dsh/.credentials.yaml")):
    creds = {}
    if os.path.exists(path):
        raw = open(path, "r", encoding="utf-8-sig").read()
        for line in raw.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    return creds


def tokens(text):
    import jieba
    out = []
    for t in jieba.cut(text.lower()):
        t = t.strip()
        if len(t) >= 2 and not re.match(r"^[\W_]+$", t):
            out.append(t)
    out += [w for w in re.findall(r"[a-z0-9]{3,}", text.lower())]
    return set(out)


def overlap_ratio(q, fact):
    a, b = tokens(q), tokens(fact)
    if not a:
        return 1.0
    return len(a & b) / len(a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-overlap", type=float, default=0.4, help="查询-事实词重叠上限（难度门槛）")
    ap.add_argument("--min-len", type=int, default=60)
    ap.add_argument("--max-len", type=int, default=500)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    creds = load_credentials()
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: no DEEPSEEK_API_KEY / TRINITY_LLM_API_KEY")
        return 1

    # ── 1) 取生产 active 记忆并筛选 ──
    sys.path.insert(0, ROOT)
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")
    rows = []
    if mem._adapter:
        offset = 0
        while True:
            batch = mem._adapter.get_all_memories(limit=1000, offset=offset)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
    print(f"active memories: {len(rows)}")
    cands = []
    for r in rows:
        cat = r.get("category") or ""
        if cat in EXCLUDE_CATEGORIES or cat not in TARGET_CATEGORIES:
            tags_ok = any(str(t) in TARGET_TAGS for t in (r.get("tags") or []))
            if not tags_ok:
                continue
        content = (r.get("content") or "").strip()
        if not (args.min_len <= len(content) <= args.max_len):
            continue
        if "enc:v1" in content:
            continue
        # 去掉明显非事实块（markdown 标题/列表堆）
        cands.append({"memory_id": r["memory_id"], "category": cat or "general",
                      "tags": r.get("tags") or [], "fact": content})
    print(f"candidates: {len(cands)}")
    random.seed(args.seed)
    random.shuffle(cands)
    picked = cands[: args.n]
    if args.dry_run:
        for p in picked[:10]:
            print(f"  [{p['category']}] {p['fact'][:60]}")
        print(f"DRY-RUN: would generate {len(picked)} queries -> {args.out}")
        return 0

    # ── 2) LLM 生成难查询 + 硬度过滤 ──
    from trinity.llm.client import chat_completion

    def llm(system, user):
        resp = chat_completion(
            {"model": os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat"),
             "messages": [{"role": "system", "content": system},
                          {"role": "user", "content": user}],
             "temperature": 0.7, "max_tokens": 120},
            api_key=api_key, timeout=60,
        )
        return (resp.get("content") or "").strip().strip('"').strip()

    items = []
    t0 = time.time()
    for i, p in enumerate(picked):
        user = "FACT:\n" + p["fact"][:400] + "\n\nQuestion:"
        try:
            q = llm(GEN_SYSTEM, user)
        except Exception as exc:
            print(f"  [{i}] gen failed: {exc}")
            continue
        ov = overlap_ratio(q, p["fact"])
        if ov > args.max_overlap:
            print(f"  [{i}] too easy (overlap={ov:.2f}): {q[:50]}")
            continue
        items.append({
            "id": p["memory_id"],
            "category": p["category"],
            "tags": p["tags"],
            "fact": p["fact"],
            "query": q,
            "overlap": round(ov, 3),
        })
        if (i + 1) % 20 == 0:
            print(f"  [{i + 1}/{len(picked)}] kept={len(items)} ({time.time() - t0:.0f}s)")

    out = {
        "version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "n": len(items),
        "max_overlap": args.max_overlap,
        "items": items,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"HOLD OUT: {len(items)} hard queries -> {args.out} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
