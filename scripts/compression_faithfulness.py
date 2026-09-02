#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩忠实度评测（2026-09-01，大脑化层1：巩固质量要有证明）

流程（真实 LLM，构造性评测集）：
  1. 取当前库中最近高 importance 的 N 条 active 记忆（默认 20）
  2. 用 MemoryCompressor(real LLM) 按批次压缩（同一批 2-5 条 → 1 条摘要）
  3. 对每条摘要：从 parent 原文提取"关键句"（jieba 分句 + 长度过滤）
  4. 打分：摘要对关键句的保留率（归一化子串匹配，保守下界）+ LLM-judge 兜底
  5. 输出 faithfulness 分数 + 明细 JSON（.trinity/bench-results/compression-faithfulness-<ts>.json）

退出码：0=评测完成（含分数），不设阈值（先建立基线）。
用法: python scripts/compression_faithfulness.py [--limit 20] [--batch 5] [--dry-run]
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import time


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())

JUDGE_SYS = (
    "You are a strict faithfulness judge for memory compression. Given an ORIGINAL "
    "memory and a COMPRESSED SUMMARY that summarizes a batch of memories, decide whether "
    "the summary preserves at least ONE key fact from the ORIGINAL (paraphrase allowed). "
    "Reply with exactly YES or NO."
)


def judge_faithful(llm, summary: str, parent: str) -> bool:
    if not summary.strip() or not parent.strip():
        return False
    user = ("ORIGINAL:\n" + parent[:600] + "\n\nCOMPRESSED SUMMARY:\n" + summary[:800]
            + "\n\nDoes the summary preserve at least one key fact from ORIGINAL? Reply YES or NO.")
    try:
        out = llm(JUDGE_SYS, user).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False



def split_sentences(text: str):
    parts = re.split(r"[。！？!?\n；;]", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 8]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--store", default=os.path.expanduser("~/.trinity/store/trinity_store.db"))
    args = ap.parse_args()

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ROOT)
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

    import sqlite3
    conn = sqlite3.connect(args.store, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, content FROM memories WHERE status='active' "
        "AND length(content) > 60 ORDER BY importance DESC, created_at DESC LIMIT ?",
        (args.limit,)).fetchall()
    conn.close()
    if not rows:
        print("FAITHFULNESS: no memories found")
        return 0
    print("FAITHFULNESS: candidates=%d" % len(rows))
    if args.dry_run:
        return 0

    from trinity.daemon.memory_compressor import MemoryCompressor, create_llm_compress_callable

    creds = {}
    try:
        raw = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
        for line in raw.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    except Exception:
        pass
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("FAITHFULNESS: no LLM key — cannot run real compression")
        return 1
    llm = create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key, model=os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat"), timeout=60)
    comp = MemoryCompressor(llm_callable=llm, pg_adapter=None)

    results = []
    t0 = time.time()
    batch = []
    for r in rows:
        batch.append({"memory_id": r["memory_id"], "content": r["content"]})
        if len(batch) >= args.batch:
            results.append(list(batch))
            batch = []
    if batch:
        results.append(batch)

    details = []
    kept = total = 0
    covered_parents = 0
    total_parents = 0
    for bi, b in enumerate(results, 1):
        try:
            res = comp.compress_batch(b, memory_type="general")
        except Exception as e:  # noqa: BLE001
            print("  batch %d ERR: %s" % (bi, str(e)[:80]))
            continue
        if not res.compressed:
            continue
        summary = res.compressed.content
        sn = normalize(summary)
        for parent in b:
            sentences = split_sentences(parent["content"])
            if not sentences:
                continue
            total_parents += 1
            total += len(sentences)
            hits = [s for s in sentences if normalize(s) and normalize(s) in sn]
            kept += len(hits)
            judge_ok = False
            if not hits and llm is not None:
                judge_ok = judge_faithful(llm, summary, parent["content"])  # paraphrase 级判定
            if hits or judge_ok:
                covered_parents += 1
            details.append({
                "parent_id": parent["memory_id"][:16],
                "sentences": len(sentences),
                "kept": len(hits),
                "covered": bool(hits),
                "judge_ok": judge_ok,
                "ratio": round(len(hits) / len(sentences), 3),
            })
    faithful = round(kept / total, 4) if total else 0.0
    coverage = round(covered_parents / total_parents, 4) if total_parents else 0.0
    report = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "candidates": len(rows),
        "batches": len(results),
        "parents": total_parents,
        "covered_parents": covered_parents,
        "coverage": coverage,  # 子串+judge 综合
        "judged_ok": sum(1 for d in details if d.get("judge_ok")),
        "key_sentences": total,
        "kept": kept,
        "sentence_retention": faithful,
        "note": "coverage=每 parent≥1句关键内容被摘要保留(批量压缩正确指标)；sentence_retention=全句保留率(保守下界)；coverage 0.7+ 可接受",
        "details": details[:40],
        "elapsed_s": round(time.time() - t0, 1),
    }
    out_dir = os.path.join(os.path.expanduser("~"), ".trinity", "bench-results")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(out_dir, "compression-faithfulness-%s.json" % ts), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("FAITHFULNESS: %s (coverage %.1f%% parents=%d kept %d/%d sentences) -> %s" %
          ("PASS" if coverage >= 0.7 else "LOW", coverage * 100, covered_parents, kept, total,
           os.path.join(out_dir, "compression-faithfulness-%s.json" % ts)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
