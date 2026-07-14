#!/usr/bin/env python3
"""
LongMemEval Dataset Ingestor

从 HuggingFace 加载 xiaowu0162/longmemeval 数据集（500题6类），
逐 session 解析 haystack，构造符合 Trinity second_brain 格式的记忆条目，
并导出为 JSONL 格式供后续摄入。

6个类别：
  knowledge-update, multi-session-reasoning, temporal-reasoning,
  single-session-user, single-session-assistant, single-session-preference

Usage:
  python longmemeval_ingestor.py --output ingested_memories.jsonl
  python longmemeval_ingestor.py --output ingested_memories.jsonl --split test
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


CATEGORIES = [
    "knowledge-update",
    "multi-session-reasoning",
    "temporal-reasoning",
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LongMemEval Dataset Ingestor — 解析数据集并导出为 Trinity JSONL 格式"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="ingested_memories.jsonl",
        help="输出的 JSONL 文件路径 (默认: ingested_memories.jsonl)",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        choices=["test", "train", "validation"],
        help="数据集 split (默认: test)",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="最大摄入样本数，用于调试 (默认: 全部)",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="是否在输出中包含原始 haystack 文本（调试用）",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="禁用进度显示",
    )
    return parser.parse_args()


def load_dataset(split: str = "test") -> Any:
    """
    从 HuggingFace 加载 LongMemEval 数据集。
    返回 datasets.Dataset 对象。
    """
    print(f"[Ingestor] 正在从 HuggingFace 加载 xiaowu0162/longmemeval (split={split}) ...")
    try:
        from datasets import load_dataset as hf_load
    except ImportError:
        print("[ERROR] 缺少依赖: pip install datasets", file=sys.stderr)
        sys.exit(1)

    ds = hf_load("xiaowu0162/longmemeval", split=split)
    print(f"[Ingestor] 加载完成，共 {len(ds)} 条样本")
    return ds


def parse_session_haystack(
    haystack: List[Dict[str, Any]],
    sample_id: int,
) -> List[Dict[str, Any]]:
    """
    将一个样本的 haystack（多 session 对话列表）解析为 Trinity 格式的记忆条目。

    LongMemEval haystack 结构：
      [{"session_id": "sess_X", "messages": [{"role": "user/assistant", "content": "..."}, ...]}, ...]

    返回: List[dict]，每个 dict 是一条记忆条目，含:
      - session_id
      - timestamp (ISO 8601)
      - content (组装后的对话文本)
      - category (从样本级 category 获取，需额外传入)
      - turn_index (对话轮次)
    """
    memories = []
    for session_block in haystack:
        sess_id = session_block.get("session_id", f"unknown_{sample_id}")
        messages = session_block.get("messages", [])

        # 将整个 session 组装为一条记忆（也可按 turn 拆分，这里合并为一个 session 级记忆）
        dialog_lines = []
        for mi, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            dialog_lines.append(f"[{role}] {content}")

        full_content = "\n".join(dialog_lines)
        # 使用 session 中的第一个时间戳或 sample 级时间戳
        ts = messages[0].get("timestamp", None) if messages else None
        if ts is None:
            ts = datetime(2024, 1, 1).isoformat() + "Z"

        memory_entry = {
            "session_id": sess_id,
            "timestamp": ts,
            "content": full_content,
            "turn_count": len(messages),
            "source_sample_id": sample_id,
        }
        memories.append(memory_entry)
    return memories


def ingest_dataset(
    dataset: Any,
    max_samples: Optional[int] = None,
    include_raw: bool = False,
    show_progress: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    摄入整个数据集，返回 (memory_entries, stats)。

    stats 包含各 category 的样本计数。
    """
    all_memories: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {"total_samples": 0, "total_memories": 0}
    for cat in CATEGORIES:
        stats[cat] = 0

    total = len(dataset)
    if max_samples:
        total = min(total, max_samples)

    for i, sample in enumerate(dataset):
        if max_samples and i >= max_samples:
            break

        sample_id = i
        haystack = sample.get("haystack", [])
        category = sample.get("category", "unknown")

        # 统计
        stats["total_samples"] += 1
        if category in stats:
            stats[category] += 1

        # 解析 haystack
        memories = parse_session_haystack(haystack, sample_id)

        # 注入 category 和 question/answer 信息
        for mem in memories:
            mem["category"] = category
            mem["question"] = sample.get("question", "")
            mem["answer"] = sample.get("answer", "")
            mem["answer_session_ids"] = sample.get("answer_session_ids", [])
            if include_raw:
                mem["_raw_haystack"] = haystack

        all_memories.extend(memories)
        stats["total_memories"] += len(memories)

        if show_progress and (i + 1) % 50 == 0:
            print(f"  [进度] {i + 1}/{total} 样本已处理 ...")

    if show_progress:
        print(f"  [完成] 共摄入 {stats['total_samples']} 样本 → {stats['total_memories']} 条记忆")

    return all_memories, stats


def export_jsonl(memories: List[Dict[str, Any]], output_path: str) -> None:
    """导出记忆条目为 JSONL 文件。"""
    print(f"[Ingestor] 正在导出 JSONL → {output_path}")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for mem in memories:
            f.write(json.dumps(mem, ensure_ascii=False) + "\n")
    file_size = os.path.getsize(output_path)
    print(f"[Ingestor] 导出完成: {len(memories)} 条记忆, {file_size:,} 字节")


def print_stats(stats: Dict[str, int]) -> None:
    """打印统计信息。"""
    print("\n" + "=" * 60)
    print("  LongMemEval 摄入统计")
    print("=" * 60)
    print(f"  总样本数:    {stats['total_samples']}")
    print(f"  总记忆条数:  {stats['total_memories']}")
    print(f"  平均记忆/样本: {stats['total_memories'] / max(stats['total_samples'], 1):.1f}")
    print("-" * 60)
    for cat in CATEGORIES:
        count = stats.get(cat, 0)
        bar = "█" * max(1, count // 5)
        print(f"  {cat:35s}  {count:4d}  {bar}")
    print("=" * 60)


def main() -> None:
    args = parse_args()

    # 1. 加载数据集
    dataset = load_dataset(split=args.split)

    # 2. 摄入
    show_progress = not args.no_progress
    memories, stats = ingest_dataset(
        dataset,
        max_samples=args.max_samples,
        include_raw=args.include_raw,
        show_progress=show_progress,
    )

    # 3. 导出
    export_jsonl(memories, args.output)

    # 4. 统计
    print_stats(stats)

    # 5. 输出路径信息
    abs_path = os.path.abspath(args.output)
    print(f"\n[Ingestor] JSONL 文件路径: {abs_path}")


if __name__ == "__main__":
    main()
