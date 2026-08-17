#!/usr/bin/env python3
"""
Trinity — A2A 协作流水线 Demo（2026-08-15, V2 动作 C ②）
==========================================================
多 agent 真实协作端到端（治理策略 + 共享聚合池 + 身份）：

  1. 注册 3 个 agent（eng-dev / eng-qa / hr-recruiter），加载工程/HR 治理策略
  2. eng-dev 写入研发记忆 → 共享聚合池
  3. eng-qa 读 dev 记忆（部门内共享 ✅）
  4. hr-recruiter 读 eng 记忆（跨部门 → 应拒 ✅）
  5. eng-qa 读 eng-kb 知识库（跨部门只读 ✅）
  6. 聚合池检索跨 agent 命中

用法：
    python scripts/a2a_pipeline_demo.py                 # 本地引擎（不依赖 api）
    python scripts/a2a_pipeline_demo.py --api http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity A2A collaboration pipeline")
    parser.add_argument("--api", default="")
    args = parser.parse_args()

    from trinity.governance import GovernanceEngine
    from trinity.agents.aggregator import MemoryAggregator

    # ── 治理策略 ────────────────────────────────────────────────────
    policies_dir = _TRINITY_ROOT / "trinity/governance/policies/enterprise"
    gov = GovernanceEngine([
        str(policies_dir / "engineering.yaml"),
        str(policies_dir / "hr.yaml"),
    ])
    print("== 1. 治理策略加载 ==")
    print(f"   policies: {gov.summary()['policies']}")

    # ── 共享聚合池 ──────────────────────────────────────────────────
    agg = MemoryAggregator(persist_path=None)
    print("\n== 2. 多 agent 写入共享池 ==")

    # eng-dev 写研发记忆
    dv1 = agg.ingest("研发：新检索模块使用 PPR 图扩展提升召回", "eng-dev",
                     {"category": "research", "scope": "eng"})
    print(f"   eng-dev 写入: {dv1.content[:30]}")

    # eng-qa 写 QA 记忆
    dv2 = agg.ingest("QA：PPR 检索在 500q 上 R@5 提升到 0.99", "eng-qa",
                     {"category": "research", "scope": "eng"})
    print(f"   eng-qa 写入: {dv2.content[:30]}")

    # hr-recruiter 写 HR 记忆
    dv3 = agg.ingest("HR：候选人池新增 5 名后端工程师", "hr-recruiter",
                     {"category": "hiring", "scope": "hr"})
    print(f"   hr-recruiter 写入: {dv3.content[:30]}")

    # ── 治理检查：跨 agent 访问控制 ────────────────────────────────
    print("\n== 3. 治理检查（策略裁决）==")
    cases = [
        ("eng-qa", "read", "eng-dev", True,  "eng-qa 读 eng-dev（部门内 ✅）"),
        ("hr-recruiter", "read", "eng-dev", False, "hr 读 eng-dev（跨部门应拒 ✅）"),
        ("eng-qa", "read", "eng-kb", True,  "eng-qa 读知识库（跨部门只读 ✅）"),
        ("hr-recruiter", "write", "eng-kb", False, "hr 写知识库（应拒 ✅）"),
        ("hr-recruiter", "read", "hr-recruiter", True, "hr 读自己（✅）"),
    ]
    ok = True
    for s, a, t, expect, label in cases:
        d = gov.check(s, a, t)
        hit = d["allow"] == expect
        ok &= hit
        print(f"   {label}: allow={d['allow']} {'✅' if hit else '❌'}")

    # ── 共享池协作检索 ──────────────────────────────────────────────
    print("\n== 4. 共享池跨 agent 检索 ==")
    try:
        agg._rebuild_index()
    except Exception:
        pass
    results = agg.query({}, limit=5, mode="hybrid", query_text="PPR 检索 召回")
    print(f"   hybrid 检索 'PPR 检索 召回': {len(results)} 条")
    for r in results[:4]:
        print(f"     - [{r.source_agents}] {r.content[:40]}")

    # ── 聚合池统计 ──────────────────────────────────────────────────
    stats = agg.get_stats() if hasattr(agg, "get_stats") else {}
    print(f"\n== 5. 聚合池统计: {stats.get('total_memories', '?')} 条跨 agent 共享 ==")
    print(f"   agents: {list(agg._agent_index.keys()) if agg._agent_index else '?'}")

    ok = ok and len(agg._agent_index) >= 3
    print(f"\nRESULT: {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
