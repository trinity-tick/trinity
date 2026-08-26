# -*- coding: utf-8 -*-
"""run_pagetree_summaries.py — 页树节点摘要生成（Phase 2，增量，便宜模型）。

借鉴 PageIndex 的"双模型分工"：索引结构免费（纯元数据），只有节点摘要
花 LLM（deepseek-chat，基本模型足够）。增量执行：只补 summary 为空的簇，
默认每轮上限 --limit 个（控制成本与运行时长），供维护链每日调用。

用法:
    python scripts/run_pagetree_summaries.py                 # 生产大库，增量 20 簇
    python scripts/run_pagetree_summaries.py --limit 5       # 冒烟
    python scripts/run_pagetree_summaries.py --tree PATH     # 指定页树文件
    python scripts/run_pagetree_summaries.py --dry-run       # 只统计待补簇

产物: 更新 <store>/pagetree.json 的 cluster.summary 字段（含 summary_ts）。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DEFAULT_TREE = os.path.expanduser("~/.trinity/store/pagetree.json")
DEFAULT_CHECKPOINT = os.path.expanduser("~/.trinity/automation/checkpoints/pagetree_summaries.json")

SUMMARY_SYSTEM = (
    "You are an expert librarian building a topic index for a long-term memory system. "
    "For the given memory page (cluster), write a concise page summary (<=60 tokens, "
    "in the same language as the samples): what topic this page covers and what kind of "
    "facts live here. Output ONLY the summary, no preamble."
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


def summarize_cluster(llm, cat: str, node: dict) -> str:
    tags = ", ".join(t for t, _ in node.get("tags", [])[:8]) or "-"
    samples = "\n---\n".join(s[:300] for s in node.get("sample", [])[:3]) or "(no samples)"
    user = (
        f"Category: {cat}\nCluster title: {node.get('title', '')}\nTags: {tags}\n"
        f"Sample contents:\n{samples}\n\nSummary:"
    )
    out = llm(SUMMARY_SYSTEM, user)
    return (out or "").strip()[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=DEFAULT_TREE)
    ap.add_argument("--limit", type=int, default=20, help="本轮最多补摘要的簇数")
    ap.add_argument("--min-count", type=int, default=2, help="簇记忆数下限（过小簇不值得摘要）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat"))
    # Codex 借鉴 Phase 3（2026-08-26）：checkpoint/resume——done/failed 记录，
    # 中断重跑跳过已完成；--retry-failed 重试失败项（默认跳过失败项）。
    ap.add_argument("--checkpoint-file", default=DEFAULT_CHECKPOINT)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-checkpoint", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.tree):
        print(f"PAGETREE-SUMMARIES SKIP: tree not found at {args.tree} (build first)")
        return 0

    creds = load_credentials()
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("PAGETREE-SUMMARIES SKIP: no DEEPSEEK_API_KEY / TRINITY_LLM_API_KEY")
        return 0

    data = json.load(open(args.tree, encoding="utf-8"))
    clusters = data.get("clusters", {})

    # ── checkpoint/resume（Codex 借鉴 Phase 3）──
    checkpoint = {"done": [], "failed": []}
    if not args.no_checkpoint and os.path.exists(args.checkpoint_file):
        try:
            with open(args.checkpoint_file, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
        except Exception:
            checkpoint = {"done": [], "failed": []}
    skip = set(checkpoint.get("done", []))
    if not args.retry_failed:
        skip |= set(checkpoint.get("failed", []))
    elif checkpoint.get("failed"):
        print(f"  retry-failed: {len(checkpoint.get('failed', []))} items")

    todo = sorted(
        [c for c in clusters.values()
         if not (c.get("summary") or "").strip()
         and c.get("stats", {}).get("count", 0) >= args.min_count
         and c.get("node_id") not in skip],
        key=lambda c: -c.get("stats", {}).get("count", 0),
    )
    print(f"PAGETREE-SUMMARIES: clusters={len(clusters)} empty={len([c for c in clusters.values() if not (c.get('summary') or '').strip()])} "
          f"todo={len(todo)} limit={args.limit} (checkpoint done={len(skip - set(checkpoint.get('failed', [])))})")
    if args.dry_run:
        for c in todo[: args.limit]:
            print(f"  would summarize: {c['node_id']} ({c['stats']['count']} mems)")
        return 0

    from trinity.llm.client import chat_completion, resolve_model_for

    model = resolve_model_for("summarize", args.model)  # Codex 借鉴 Phase 3：任务分级路由
    if model != args.model:
        print(f"  model routing: summarize -> {model}")

    def llm(system, user):
        resp = chat_completion(
            {"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}],
             "temperature": 0.2, "max_tokens": 120},
            api_key=api_key,
            timeout=60,
        )
        return resp.get("content", "")

    done = 0
    failed = 0
    t0 = time.time()

    def _save_checkpoint():
        if args.no_checkpoint:
            return
        try:
            os.makedirs(os.path.dirname(args.checkpoint_file), exist_ok=True)
            with open(args.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    for node in todo[: args.limit]:
        try:
            summary = summarize_cluster(llm, node.get("category", ""), node)
            if not summary:
                failed += 1
                checkpoint.setdefault("failed", []).append(node["node_id"])
                _save_checkpoint()
                continue
            node["summary"] = summary
            node["summary_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            node["summary_model"] = args.model
            done += 1
            checkpoint.setdefault("done", []).append(node["node_id"])
            checkpoint["failed"] = [f for f in checkpoint.get("failed", []) if f != node["node_id"]]
            _save_checkpoint()
            print(f"  [ok] {node['node_id']}: {summary[:70]}")
        except Exception as exc:
            failed += 1
            checkpoint.setdefault("failed", []).append(node["node_id"])
            _save_checkpoint()
            print(f"  [err] {node['node_id']}: {exc}")
        if done + failed >= args.limit:
            break

    if done:
        with open(args.tree, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    print(f"PAGETREE-SUMMARIES done: {done} summarized, {failed} failed, "
          f"{len(todo) - done - failed} remaining, {time.time() - t0:.1f}s "
          f"(checkpoint: done={len(checkpoint.get('done', []))} failed={len(checkpoint.get('failed', []))})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
