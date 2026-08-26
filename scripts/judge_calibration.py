#!/usr/bin/env python3
"""judge_calibration.py — 人类 vs judge 一致性校准（R8 P0-① 修裁判）。

从一份 QA records 中抽样 N 题，生成"人工判分表"（CSV/JSON），
人工标注 YES/NO 后与 judge3 判分对比，报告 Cohen's Kappa。

流程：
  1. 抽 N 题（默认 30，seed 42）→ 写 judge_calib_sample.json
     （question/expected/answer/judge_votes 预填——judge 判分可先跑）；
  2. 人工在 sample 文件里补 human_verdict（YES/NO/UNSURE）；
  3. 重跑本脚本 --human-file 计算 Kappa。

用法：
    python scripts/judge_calibration.py --records <qa_records.json> --n 30
      → 生成样本文件（含 judge 判分，需 judge3 先跑或内联）
    python scripts/judge_calibration.py --records ... --human-file sample.json
      → 计算 Kappa 报告
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

OUT_DIR = os.path.expanduser("~/.trinity/evolve")
DATA = r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json"


def _kappa(human: list, judge: list) -> dict:
    """Cohen's Kappa（二元 YES/NO；UNSURE 排除）。"""
    pairs = [(h, j) for h, j in zip(human, judge) if h in ("YES", "NO") and j in (True, False)]
    if not pairs:
        return {"error": "no comparable pairs (fill human_verdict first)"}
    agree = sum(1 for h, j in pairs if (h == "YES") == j)
    po = agree / len(pairs)
    # 边际
    h_yes = sum(1 for h, _ in pairs if h == "YES") / len(pairs)
    j_yes = sum(1 for _, j in pairs if j) / len(pairs)
    pe = h_yes * j_yes + (1 - h_yes) * (1 - j_yes)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {
        "n": len(pairs),
        "agreement": round(po, 3),
        "kappa": round(kappa, 3),
        "human_yes": round(h_yes, 3),
        "judge_yes": round(j_yes, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True, help="QA records JSON（records 列表）")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--human-file", default="",
                        help="已补 human_verdict 的样本文件（计算 Kappa）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    with open(args.records, encoding="utf-8") as f:
        blob = json.load(f)
    recs = blob.get("records", blob if isinstance(blob, list) else [])

    # 题目映射（真实题面）
    qmap = {}
    try:
        with open(DATA, encoding="utf-8") as f:
            for q in json.load(f):
                qmap[str(q.get("question_id"))] = str(q.get("question", ""))
    except Exception:
        pass

    # ── 计算 Kappa 模式 ──
    if args.human_file:
        with open(args.human_file, encoding="utf-8") as f:
            sample = json.load(f)
        human = [s.get("human_verdict", "UNSURE") for s in sample]
        judge = [bool(s.get("judge_correct", False)) for s in sample]
        res = _kappa(human, judge)
        verdict = "✅ 可靠" if res.get("kappa", 0) >= 0.6 else "⚠️ 需改进"
        print(f"Kappa 报告: {json.dumps(res, ensure_ascii=False)} → {verdict}")
        print("（kappa ≥0.6 可接受；<0.6 检查 rubric/样本）")
        return 0

    # ── 生成样本模式 ──
    random.seed(42)
    sample = random.sample(recs, min(args.n, len(recs)))
    items = []
    for r in sample:
        qid = r.get("question_id", "")
        items.append({
            "question_id": qid,
            "question_type": r.get("question_type", ""),
            "question": qmap.get(str(qid), ""),
            "expected": r.get("expected", "")[:250],
            "answer": r.get("answer", "")[:400],
            "judge_correct": None,  # 由 judge3 结果回填或人工对照
            "human_verdict": "",    # 人工填 YES/NO/UNSURE
        })
    out = args.out or os.path.join(OUT_DIR, f"judge_calib_sample_{datetime.now().strftime('%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"样本已生成 -> {out}")
    print(f"下一步：1) 用 judge3 判这批（或对照已有结果）2) 在文件里填 human_verdict "
          f"3) 重跑 --human-file 计算 Kappa")
    return 0


if __name__ == "__main__":
    sys.exit(main())
