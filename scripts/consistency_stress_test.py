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

    # ── R2 收口（2026-10-06，t10）────────────────────────────────────────
    # 原先这里是 `from trinity.modules.second_brain.cb49_52 import GroundTruthEpisodes`
    # —— `cb49_52.py:455` 是 **declared-shadow 副本**（登记在
    # `capability_ledger.DECLARED_SHADOW_COPIES`），**不是**引擎真身
    # `engine_diagnostics.py:31`。后果有两条，是实测出来的：
    #   ① 读数与"引擎能力"无关（压测测的是影子实现）；
    #   ② 读数**不可复现**，且量级虚高。实测（n=8，PYTHONHASHSEED=0..7，
    #      见 D:\DSH官网\trinity-optimize-20261006\evidence\r2_before_after.json）：
    #        影子副本 0.40 ~ 0.50（均值 0.4563）
    #        引擎真身 0.10 ~ 0.15（均值 0.1375）
    #      ⇒ 旧读数把"引擎能力"报了约 **3.3 倍**。
    # 现指向引擎真身 —— 与 `engine_core.py:366` 实际构造的是**同一个类**、
    # 同一组参数（`short_term_size=20, context_window_extension=5, retrieval_depth=3`）。
    #
    # 参数映射（影子 → 引擎真身）：`short_term_capacity=20` ↔ `short_term_size=20`；
    # 影子的 `max_episodes=500` 在引擎真身里**没有对应参数**（引擎不设该上限），
    # 故直接丢弃，不做等价性假设。
    from trinity.modules.second_brain.engine_diagnostics import GroundTruthEpisodes

    # 回归哨兵（可失败）：压测**必须**指向引擎真身。谁把它改回影子副本，
    # 这里立刻失败 —— 避免"读数又没有判别力"再次静默发生。
    if GroundTruthEpisodes.__module__ != "trinity.modules.second_brain.engine_diagnostics":
        raise SystemExit(
            "R2 回归：一致性压测必须指向引擎真身 "
            "trinity.modules.second_brain.engine_diagnostics.GroundTruthEpisodes，"
            "当前是 %s.%s（declared-shadow 副本会把读数变成没有判别力的数字）"
            % (GroundTruthEpisodes.__module__, GroundTruthEpisodes.__qualname__))

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
    # 参数与 engine_core.py:366 的构造保持一致（= 引擎自己的默认值）。
    gt = GroundTruthEpisodes(short_term_size=20, context_window_extension=5,
                             retrieval_depth=3)
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
            # R2 收口（t10）：把「测的是哪个类」写进报告本身，读者不必翻源码。
            "source_class": "%s.%s" % (GroundTruthEpisodes.__module__,
                                       GroundTruthEpisodes.__qualname__),
            "declared_shadow_avoided": (
                "trinity.modules.second_brain.cb49_52.GroundTruthEpisodes"
                "（declared-shadow，非引擎真身；见 capability_ledger.DECLARED_SHADOW_COPIES）"),
            "reproducibility": (
                "实测（t10，n=8，PYTHONHASHSEED=0..7）：引擎真身 0.10~0.15；"
                "影子副本 0.40~0.50。两者都受集合迭代顺序影响"
                "（engine_diagnostics.py:219-223：keyword_index[kw] 是 set，"
                "sorted 稳定排序，同分时按集合迭代顺序破平）。"
                "所以单次读数带噪声，做前后比较时必须报 n 与区间，不得只报单点。"),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED", "<unset>"),
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
    # 2026-10-06（t10）：报告里含中文说明且以 ensure_ascii=False 打到 stdout；
    # Windows 控制台默认 GBK，遇到非 GBK 可编码字符会 UnicodeEncodeError
    # （实测：新增的 U+21D2 箭头字符直接把脚本打成 exit 1）。与本目录
    # channel_census.py:212 同做法：显式把 stdout 设为 utf-8 + errors=replace。
    #
    # 刻意**不**用 try/except 包裹：那会给本文件新增一处 `except: pass`，
    # 撞上 docs/SILENT_FAILURE_BUDGETS.json 的静默失败计数上限
    #（本文件 HEAD 计数为 0，实测改后仍为 0）。
    #
    # ⚠️ 用词禁忌（t10 实测踩过，务必保留这段）：本脚本源码里**不得**出现
    # `tests/unit/test_gate_wiring_coverage.py:43` 那个模块级正则常量所列的**任一
    # 关键词**（共 4 个：1 个英文 + 3 个中文，字面见该行 —— 连该常量**自己的名字**
    # 都含那个英文词，所以本文也刻意不写它的名字）。该判定是**全文件文本匹配**
    # ⇒ 注释里写一次，本脚本就会被当成"带该语义的门禁脚本"，从而要求接线或登记
    # （本注释初版正因写出其中一个词，让该用例立刻变红）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
