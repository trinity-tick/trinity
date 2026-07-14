#!/usr/bin/env python3
"""
LongMemEval End-to-End QA Evaluator

使用 LongMemEval 官方的 reader prompt 和 judge prompt 进行端到端 QA 评测。
支持模拟检索/记忆注入模式，按类别输出 QA accuracy。

Reader Prompt 用于生成答案（基于检索到的记忆上下文），
Judge Prompt 用于评分（对比生成答案与标准答案）。

Usage:
  python longmemeval_qa_evaluator.py \
      --data ingested_memories.jsonl \
      --retrieval-results retrieval_output.json \
      --output qa_report.json

需要 OpenAI-compatible API 来调用 LLM（reader 和 judge 角色）。
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


CATEGORIES = [
    "knowledge-update",
    "multi-session-reasoning",
    "temporal-reasoning",
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
]

# ============================================================
# LongMemEval Official Prompts
# 参考: https://github.com/xiaowu0162/longmemeval
# ============================================================

READER_SYSTEM_PROMPT = (
    "You are a helpful assistant with access to a long-term memory system. "
    "Below are relevant memories retrieved from your memory store. "
    "Use ONLY the information provided in these memories to answer the user's question. "
    "If the memories do not contain enough information to answer the question, "
    "say 'I don't have enough information to answer this question.' "
    "Do not make up or infer information not present in the memories."
)

READER_USER_TEMPLATE = """Here are the relevant memories:

{memories}

---

Question: {question}

Answer:"""

JUDGE_SYSTEM_PROMPT = (
    "You are an impartial evaluator tasked with judging the correctness of an AI assistant's "
    "response to a question. You will be given the question, the correct answer (ground truth), "
    "and the assistant's response. Your job is to determine whether the assistant's response "
    'is CORRECT or INCORRECT.\n\n'
    "Criteria for correctness:\n"
    "- The assistant's answer must convey the same core information as the ground truth answer.\n"
    "- Minor wording differences, spelling variations, or formatting differences are acceptable.\n"
    "- If the assistant refuses to answer or says it doesn't have enough information when the "
    "answer is indeed available in the context, mark as INCORRECT.\n"
    "- If the assistant hallucinates or provides information not present in the ground truth, "
    "mark as INCORRECT.\n\n"
    "Output ONLY one word: CORRECT or INCORRECT."
)

JUDGE_USER_TEMPLATE = """Question: {question}

Ground Truth Answer: {ground_truth}

Assistant's Response: {assistant_response}

Judgment (CORRECT or INCORRECT):"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LongMemEval QA Evaluator — 端到端 QA 准确率评测"
    )
    parser.add_argument(
        "--data",
        type=str,
        required=True,
        help="摄入后的 JSONL 数据文件路径",
    )
    parser.add_argument(
        "--retrieval-results",
        type=str,
        required=True,
        help="检索结果 JSON（含 retrieved_session_ids）",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
        help="OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("OPENAI_API_KEY", ""),
        help="API key",
    )
    parser.add_argument(
        "--reader-model",
        type=str,
        default="gpt-4o-mini",
        help="用于生成答案的模型 (reader)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default="gpt-4o",
        help="用于评分的模型 (judge)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="qa_report.json",
        help="QA 评测报告输出路径",
    )
    parser.add_argument(
        "--report-md",
        type=str,
        default=None,
        help="Markdown 报告输出路径",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="检索 top-k 条记忆用于生成答案 (默认: 5)",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="最大评测样本数 (调试用)",
    )
    parser.add_argument(
        "--sleep-interval",
        type=float,
        default=0.5,
        help="API 调用间隔（秒）",
    )
    return parser.parse_args()


def load_data_for_qa(jsonl_path: str) -> Dict[int, Dict[str, Any]]:
    """
    加载 JSONL 数据，按 sample_id 聚合全部信息。
    返回: {sample_id: {"category", "question", "answer", "answer_session_ids",
                       "session_map": {session_id: content}, ...}}
    """
    print(f"[QA-Eval] 加载数据: {jsonl_path}")
    sample_map: Dict[int, Dict[str, Any]] = {}

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            mem = json.loads(line)
            sid = mem.get("source_sample_id", -1)
            if sid == -1:
                continue
            if sid not in sample_map:
                sample_map[sid] = {
                    "category": mem.get("category", "unknown"),
                    "question": mem.get("question", ""),
                    "answer": mem.get("answer", ""),
                    "answer_session_ids": mem.get("answer_session_ids", []),
                    "session_map": {},
                }
            sess_id = mem.get("session_id", "")
            sample_map[sid]["session_map"][sess_id] = mem.get("content", "")

    print(f"[QA-Eval] 加载了 {len(sample_map)} 个样本")
    return sample_map


def load_retrieval_results(json_path: str) -> Dict[str, Any]:
    """加载检索结果。"""
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_sample_key(sample_id: int):
    for key in [f"sample_{sample_id}", str(sample_id), sample_id]:
        yield key


def build_memory_context(
    sample_info: Dict[str, Any],
    retrieval_entry: Optional[Dict[str, Any]],
    top_k: int,
) -> str:
    """
    根据检索结果组装记忆上下文。

    如果没有检索结果（模拟 oracle 模式），使用 answer_session_ids 作为 oracle 检索。
    """
    session_map = sample_info.get("session_map", {})
    retrieved_ids: List[str] = []

    if retrieval_entry:
        retrieved_ids = retrieval_entry.get("retrieved_session_ids", [])[:top_k]

    # 如果检索结果为空，回退到 oracle (answer_session_ids)
    if not retrieved_ids:
        retrieved_ids = sample_info.get("answer_session_ids", [])[:top_k]

    memory_blocks = []
    for i, sess_id in enumerate(retrieved_ids):
        content = session_map.get(sess_id, "")
        if content:
            memory_blocks.append(f"[Memory {i + 1}] (Session: {sess_id})\n{content}")

    if not memory_blocks:
        return "No relevant memories available."

    return "\n\n".join(memory_blocks)


def call_llm(
    messages: List[Dict[str, str]],
    model: str,
    api_base: str,
    api_key: str,
) -> str:
    """调用 OpenAI-compatible API。"""
    try:
        from openai import OpenAI
    except ImportError:
        print("[ERROR] 缺少依赖: pip install openai", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(base_url=api_base, api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        max_tokens=512,
    )
    return response.choices[0].message.content.strip()


def run_qa_evaluation(
    sample_map: Dict[int, Dict[str, Any]],
    retrieval_results: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """
    运行端到端 QA 评测:
    对每个样本 → 组装记忆上下文 → reader 生成答案 → judge 评分。
    """
    results: Dict[str, Any] = {
        "meta": {
            "reader_model": args.reader_model,
            "judge_model": args.judge_model,
            "top_k": args.top_k,
        },
        "per_sample": {},
        "summary": {},
    }

    correct_by_category: Dict[str, int] = defaultdict(int)
    total_by_category: Dict[str, int] = defaultdict(int)
    total_correct = 0
    total_evaluated = 0
    skipped = 0

    samples_to_eval = list(sample_map.items())
    if args.max_samples:
        samples_to_eval = samples_to_eval[: args.max_samples]

    for idx, (sample_id, sample_info) in enumerate(samples_to_eval):
        question = sample_info["question"]
        ground_truth = sample_info["answer"]
        category = sample_info["category"]

        # 查找检索结果
        retrieval_entry = None
        for key in _safe_sample_key(sample_id):
            if key in retrieval_results:
                retrieval_entry = retrieval_results[key]
                break

        # 组装记忆上下文
        memory_context = build_memory_context(sample_info, retrieval_entry, args.top_k)

        # === Reader: 生成答案 ===
        reader_messages = [
            {"role": "system", "content": READER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": READER_USER_TEMPLATE.format(
                    memories=memory_context, question=question
                ),
            },
        ]

        try:
            reader_answer = call_llm(
                reader_messages, args.reader_model, args.api_base, args.api_key
            )
        except Exception as e:
            print(f"  [WARN] sample {sample_id} reader 调用失败: {e}")
            skipped += 1
            continue

        time.sleep(args.sleep_interval)

        # === Judge: 评分 ===
        judge_messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": JUDGE_USER_TEMPLATE.format(
                    question=question,
                    ground_truth=ground_truth,
                    assistant_response=reader_answer,
                ),
            },
        ]

        try:
            judge_raw = call_llm(
                judge_messages, args.judge_model, args.api_base, args.api_key
            )
        except Exception as e:
            print(f"  [WARN] sample {sample_id} judge 调用失败: {e}")
            skipped += 1
            continue

        is_correct = "CORRECT" in judge_raw.upper()

        # 记录
        results["per_sample"][str(sample_id)] = {
            "category": category,
            "question": question,
            "ground_truth": ground_truth,
            "reader_answer": reader_answer,
            "judge_raw": judge_raw,
            "is_correct": is_correct,
        }

        total_by_category[category] += 1
        if is_correct:
            correct_by_category[category] += 1
            total_correct += 1
        total_evaluated += 1

        time.sleep(args.sleep_interval)

        if (idx + 1) % 10 == 0:
            acc = total_correct / total_evaluated if total_evaluated else 0
            print(f"  [QA 进度] {idx + 1}/{len(samples_to_eval)} | 当前 Acc: {acc:.2%}")

    # 构建摘要
    summary: Dict[str, Any] = {}
    for cat in CATEGORIES:
        t = total_by_category.get(cat, 0)
        c = correct_by_category.get(cat, 0)
        summary[cat] = {
            "total": t,
            "correct": c,
            "accuracy": round(c / t, 4) if t > 0 else None,
        }

    summary["overall"] = {
        "total": total_evaluated,
        "correct": total_correct,
        "accuracy": round(total_correct / total_evaluated, 4) if total_evaluated else None,
        "skipped": skipped,
    }

    results["summary"] = summary
    return results


def generate_markdown_report(results: Dict[str, Any]) -> str:
    """生成 Markdown QA 评测报告。"""
    summary = results["summary"]
    meta = results["meta"]

    lines = []
    lines.append("# LongMemEval End-to-End QA Evaluation Report")
    lines.append("")
    lines.append(f"- **Reader Model**: {meta['reader_model']}")
    lines.append(f"- **Judge Model**: {meta['judge_model']}")
    lines.append(f"- **Top-K Memories**: {meta['top_k']}")
    lines.append(f"- **Overall Accuracy**: {summary['overall']['accuracy']}")
    lines.append(f"- **Total Evaluated**: {summary['overall']['total']}")
    lines.append(f"- **Skipped**: {summary['overall']['skipped']}")
    lines.append("")

    lines.append("## Per-Category Accuracy")
    lines.append("")
    lines.append("| Category | Total | Correct | Accuracy |")
    lines.append("|---|---|---|---|")

    for cat in CATEGORIES:
        cat_summary = summary.get(cat, {})
        total = cat_summary.get("total", 0)
        correct = cat_summary.get("correct", 0)
        acc = cat_summary.get("accuracy")
        acc_str = f"{acc:.2%}" if acc is not None else "-"
        lines.append(f"| {cat} | {total} | {correct} | {acc_str} |")

    overall = summary["overall"]
    lines.append(
        f"| **Overall** | {overall['total']} | {overall['correct']} | "
        f"{overall['accuracy']:.2%} |"
    )

    return "\n".join(lines)


def main() -> None:
    args = parse_args()

    if not args.api_key:
        print(
            "[ERROR] 请设置 OPENAI_API_KEY 环境变量或通过 --api-key 参数传入",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. 加载数据
    sample_map = load_data_for_qa(args.data)

    # 2. 加载检索结果
    retrieval_results = load_retrieval_results(args.retrieval_results)

    # 3. 运行 QA 评测
    results = run_qa_evaluation(sample_map, retrieval_results, args)

    # 4. 输出 JSON 报告
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[QA-Eval] JSON 报告已保存: {os.path.abspath(args.output)}")

    # 5. Markdown 报告
    if args.report_md:
        md = generate_markdown_report(results)
        with open(args.report_md, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[QA-Eval] Markdown 报告已保存: {os.path.abspath(args.report_md)}")

    # 6. 终端摘要
    summary = results["summary"]
    print("\n" + "=" * 60)
    print("  LongMemEval QA Accuracy Summary")
    print("=" * 60)
    for cat in CATEGORIES:
        s = summary.get(cat, {})
        acc = s.get("accuracy")
        acc_str = f"{acc:.2%}" if acc is not None else "-"
        print(f"  {cat:35s}  {s.get('correct', 0)}/{s.get('total', 0)}  Acc={acc_str}")
    overall = summary["overall"]
    print(f"  {'Overall':35s}  {overall['correct']}/{overall['total']}  "
          f"Acc={overall['accuracy']:.2%}")
    print("=" * 60)


if __name__ == "__main__":
    main()
