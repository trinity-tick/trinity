#!/usr/bin/env python3
"""
Trinity — B3 多智能体治理 Demo（2026-08-15）
==============================================
2+ agent 协作 + 策略热切换：

  1. 注册 alpha / beta 两个 agent
  2. 默认策略（隔离）：beta 写记忆，alpha 读 beta → denied（隔离）
  3. 热切换策略（共享）：alpha 读 beta → allowed
  4. 委托动作：任一 agent delegate → allowed
  5. 审计汇总：denied/allowed 统计

用法：
    python scripts/governance_demo.py            # 本地引擎（不依赖 api）
    python scripts/governance_demo.py --api http://127.0.0.1:8001   # 走 API 注册/审计
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity B3 governance demo")
    parser.add_argument("--api", default="", help="Trinity API（注册/审计走 HTTP）")
    args = parser.parse_args()

    from trinity.governance import GovernanceEngine

    engine = GovernanceEngine()
    isolation_path = str(_TRINITY_ROOT / "trinity/governance/policies/isolation.yaml")
    engine.load_policy(isolation_path)

    print("== 1. 注册 agents（本地模拟 /a2a/agents/register）==")
    agents = ["alpha", "beta"]
    print("   registered:", agents)
    if args.api:
        import requests
        h = {"X-Agent-ID": "governance", "X-Agent-Role": "admin"}
        for a in agents:
            r = requests.post(f"{args.api}/a2a/agents/register",
                              json={"agent_id": a, "name": a, "capabilities": ["memory", "retrieval"]},
                              headers=h, timeout=15)
            print(f"   register {a}: {r.status_code}")

    print("\n== 2. 默认策略（隔离）==")
    d1 = engine.check("beta", "write", "beta")     # 同 agent → allowed
    d2 = engine.check("alpha", "read", "beta")     # 跨 agent → denied（隔离）
    print(f"   beta写beta: allow={d1['allow']} (期望 True)")
    print(f"   alpha读beta: allow={d2['allow']} (期望 False)")

    print("\n== 3. 热切换策略（共享 alpha↔beta）==")
    engine.clear_policies()
    shared = Path(_TRINITY_ROOT / "trinity/governance/policies/example.yaml")
    # 热切换：清空隔离策略 → 加载含共享/委托规则的 example.yaml
    engine.load_policy(str(shared))
    d3 = engine.check("alpha", "read", "beta")
    print(f"   alpha读beta: allow={d3['allow']} (共享规则优先级 → 期望 True)")

    print("\n== 4. 委托动作 ==")
    d4 = engine.check("gamma", "delegate", "alpha")
    print(f"   gamma委托alpha: allow={d4['allow']} (期望 True)")

    print("\n== 5. 审计汇总 ==")
    s = engine.summary()
    print(f"   policies={s['policies']} audit_entries={s['audit_entries']} denied={s['denied']}")

    ok = (d1["allow"] and not d2["allow"] and d3["allow"] and d4["allow"])
    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
