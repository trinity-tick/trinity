#!/usr/bin/env python3
"""
Trinity — A3 长程一致性压测（2026-08-15）
============================================
跨会话"身份漂移 + 事实一致性"压测，对齐业界长程一致性方案：

  1. 身份稳定性：IdentityPreservingConsolidator 多次 consolidate 后
     identity_hash byte-equal（固化不改身份）；manifest 变更才触发漂移。
  2. Ground-truth 回放：GroundTruthEpisodes 摄取带事实标签的跨会话 episode，
     查询命中率（短程/长程混合）。
  3. 漂移检测：修改 manifest（capabilities）→ hash 变化被捕获；不变时稳定。

规模：可配 N 会话 × M 事件（默认 20 会话 × 50 事件 = 1,000 事件），
报告含 token 近似（约 25 token/事件 → 25k token；--large 模式 100k token）。

用法：
    python scripts/consistency_stress_test.py
    python scripts/consistency_stress_test.py --sessions 50 --events 100
    python scripts/consistency_stress_test.py --output .trinity/logs/consistency_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("consistency_stress")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

_TOPICS = [
    "SmartCos WMS 网关端口 8080", "订单波次释放流程", "库存盘点差异处理",
    "京东物流对接字段", "旺店通商品同步", "前端构建部署漂移修复",
    "权限 RBAC 角色配置", "数据看板指标定义", "出库单生成规则", "容器健康检查策略",
]
_ACTIONS = ["配置", "修复", "优化", "排查", "验证", "回滚", "上线", "评审"]


def _gen_event(seed: int) -> dict:
    rng = random.Random(seed)
    topic = _TOPICS[rng.randrange(len(_TOPICS))]
    action = _ACTIONS[rng.randrange(len(_ACTIONS))]
    return {
        "event_id": f"evt_{seed:x}",
        "content": f"[会话事件] {action} {topic} (编号 {seed})",
        "confidence": round(rng.uniform(0.4, 0.95), 2),
        "timestamp": time.time() + seed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity A3 long-horizon consistency stress")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--events", type=int, default=50)
    parser.add_argument("--output", default=os.path.expanduser("~/.trinity/logs/consistency_report.json"))
    args = parser.parse_args()

    from trinity.modules.second_brain.engine_memory_core import IdentityPreservingConsolidator
    from trinity.modules.second_brain.cb49_52 import GroundTruthEpisodes

    N_SESSIONS = args.sessions
    M_EVENTS = args.events
    t0 = time.time()

    # ── 1. 身份稳定性 ────────────────────────────────────────────
    consolidator = IdentityPreservingConsolidator(episodic_threshold=10)
    consolidator.set_identity_manifest({
        "agent_id": "dsh-stress", "version": "1.0", "capabilities": ["memory", "retrieval", "plan"],
    })
    pre_hash = consolidator.get_identity_hash()
    consolidations = 0
    hash_stable = True
    events_total = 0
    for s in range(N_SESSIONS):
        for e in range(M_EVENTS):
            consolidator.add_episodic_event(_gen_event(s * 10000 + e))
            events_total += 1
            if consolidator.should_trigger_consolidation():
                record = consolidator.consolidate()
                if record is not None:
                    consolidations += 1
                    if consolidator.get_identity_hash() != pre_hash:
                        hash_stable = False
    post_hash = consolidator.get_identity_hash()
    identity_stable = hash_stable and (pre_hash == post_hash)
    semantic_count = len(getattr(consolidator, "semantic_store", {}))

    # 漂移检测：manifest 变更 → hash 变化
    consolidator.set_identity_manifest({
        "agent_id": "dsh-stress", "version": "1.1", "capabilities": ["memory", "retrieval", "plan", "act"],
    })
    drifted_hash = consolidator.get_identity_hash()
    drift_detected = drifted_hash != post_hash

    # ── 2. Ground-truth 回放准确率 ───────────────────────────────
    gt = GroundTruthEpisodes(short_term_capacity=20, max_episodes=500)
    total_episodes = 0
    hits = 0
    queries = 0
    for s in range(N_SESSIONS):
        turns = []
        for e in range(5):
            ev = _gen_event(s * 10000 + e)
            turns.append({"role": "assistant", "content": ev["content"]})
        gt.ingest_episode(f"ep_{s}", turns, metadata={"category": "general", "session": s})
        total_episodes += 1
        # 用本会话事实做查询
        q = _TOPICS[s % len(_TOPICS)]
        res = gt.retrieve(q, top_k=5)
        res_list = res.get("results", res) if isinstance(res, dict) else res
        # 命中 = 目标 episode（含该事实的会话）被召回
        content_hit = any(
            (r.get("episode_id") if isinstance(r, dict) else getattr(r, "episode_id", None)) == f"ep_{s}"
            for r in res_list
        )
        hits += 1 if content_hit else 0
        queries += 1

    gt_accuracy = hits / max(1, queries)

    elapsed = time.time() - t0
    approx_tokens = events_total * 25

    report = {
        "benchmark": "A3 consistency-stress",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "scale": {"sessions": N_SESSIONS, "events_total": events_total, "approx_tokens": approx_tokens},
        "identity": {
            "pre_hash": pre_hash[:16], "post_hash": post_hash[:16],
            "stable_across_consolidations": identity_stable,
            "consolidations": consolidations,
            "semantic_records": semantic_count,
            "drift_detected_on_manifest_change": drift_detected,
        },
        "ground_truth": {
            "episodes_ingested": total_episodes,
            "queries": queries,
            "recall_hit_rate": round(gt_accuracy, 4),
        },
        "elapsed_seconds": round(elapsed, 2),
    }

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info("report written: %s", args.output)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
