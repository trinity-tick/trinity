#!/usr/bin/env python3
"""build_private_holdout.py — 私有留出子集生成（R8 P1-①，2026-08-24）。

背景（评测方法论调研）：公开 LongMemEval-S 500 题已被多篇论文训练过
（污染风险），且 R@5 0.992 在"喂入历史"设定下饱和（无区分度）。
自进化闭环的 A/B 采纳样本必须是**私有改写版**——同一批题改写问法/
干扰项，防污染防饱和。

设计：
  - 从 500 题随机抽 N 题（默认 100，seed 42）；
  - 用 LLM（DeepSeek）改写：保留 haystack（事实上下文）不变，
    改写 question 措辞（同义/换问法）+ 可选干扰；expected 不变；
  - 输出 benchmark/private_holdout.json：
    {version: 1, built_at, note, questions: [{question_id(改写前缀),
      original_id, question(改写), answer(同), question_type,
      haystack_sessions, haystack_session_ids, haystack_dates}]}
  - 自进化闭环（evolve_signal/evolve_ab）用 --data 指向它即切私有集。

用法：
    python scripts/build_private_holdout.py --n 100 --out benchmark/private_holdout.json
    python scripts/build_private_holdout.py --dry-run   # 只抽样不改写
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SRC = r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json"
DEFAULT_OUT = os.path.join(REPO, "benchmark", "private_holdout.json")


def _llm_rewrite(question: str, key: str) -> str:
    """改写问法（同义保留语义）；无 key 时原样返回。"""
    try:
        cred = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
        api_key = None
        for line in cred.splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                api_key = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        if not api_key:
            return question
        prompt = (
            "Rewrite the following memory retrieval question. Requirements:\n"
            "1. Semantically equivalent (answer unchanged);\n"
            "2. Rephrase it (different wording/structure/angle), do not repeat verbatim;\n"
            "3. Keep the question type (temporal/preference/multi-hop/single/multi-session);\n"
            "4. **CRITICAL: output in the SAME LANGUAGE as the original question** "
            "(if the original is English, output must be English; "
            "if Chinese, output must be Chinese). "
            "The haystack content is in the original language, so a rewritten question "
            "in a different language will fail retrieval.\n"
            "5. Output only the rewritten question, no explanation.\n"
            f"Original question: {question}"
        )
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 200,
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        out = body["choices"][0]["message"]["content"].strip()
        return out if out and len(out) > 5 else question
    except Exception:
        return question


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="只抽样不改写")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-rewrite", action="store_true",
                        help="不改写（仅划分私有子集——可用于快速验证）")
    args = parser.parse_args()

    if not os.path.exists(SRC):
        print(f"source not found: {SRC}")
        return 1
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    random.seed(args.seed)
    sample = random.sample(data, min(args.n, len(data)))
    print(f"抽样 {len(sample)}/{len(data)} 题（seed={args.seed}）")

    questions = []
    for i, q in enumerate(sample):
        qid = q["question_id"]
        new_qid = f"priv_{qid}"
        question = str(q.get("question", ""))
        if not args.dry_run and not args.skip_rewrite:
            question = _llm_rewrite(question, str(qid))
        questions.append({
            "question_id": new_qid,
            "original_id": str(qid),
            "question": question,
            "answer": str(q.get("answer", "")),
            "question_type": q.get("question_type", ""),
            # 2026-08-25（遗留修复）：补 question_date——源数据有（temporal 题型
            # RouteReasoner 依赖它做时间线排序），此前 build 丢弃导致私有集
            # temporal 题缺日期上下文。
            "question_date": q.get("question_date", ""),
            # 2026-08-25（检索指标）：补 answer_session_ids——R@5 检索评测的
            # ground truth（源数据 500/500 有），此前 build 丢弃导致私有集
            # 0/100，无法做确定性检索指标 A/B。
            "answer_session_ids": q.get("answer_session_ids", []),
            "haystack_sessions": q.get("haystack_sessions", []),
            "haystack_session_ids": q.get("haystack_session_ids", []),
            "haystack_dates": q.get("haystack_dates", []),
        })

    if args.dry_run:
        print(f"dry-run: 将生成 {len(questions)} 题私有子集 -> {args.out}")
        return 0

    out = {
        "version": 1,
        "built_at": datetime.now().isoformat(),
        "note": "私有留出子集（R8 P1-①）：公开 500 题污染/饱和防御，自进化 A/B 专用",
        "rewritten": not args.skip_rewrite,
        "source": os.path.basename(SRC),
        "questions": questions,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"written -> {args.out} ({len(questions)} 题, rewritten={not args.skip_rewrite})")
    # 抽查
    if questions:
        print(f"  样例: {questions[0]['question'][:80]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
