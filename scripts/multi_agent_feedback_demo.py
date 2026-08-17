# -*- coding: utf-8 -*-
"""多智能体共享记忆 + 反馈闭环 —— 完整演示。

链路:
  1) agent-alpha 写入工作记忆（引擎库，agent_id=agent-alpha）
  2) agent-beta  无过滤共享检索（引擎 47 通道，跨 agent 可见）
  3) agent-gamma 对共享记忆提交反馈评分（/evolution/feedback）
  4) 进化引擎跑一轮（/evolution/cycle/run），反馈进入进化状态
  5) 汇总: 共享可见性 / 反馈记录 / 进化状态 / 隔离对照（带 agent_id 过滤应不可见）
  6) 清理演示记忆
"""
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
UNIQ = int(time.time())


def headers(agent: str) -> dict:
    return {"X-Agent-ID": agent, "X-Agent-Role": "admin"}


def main() -> None:
    tok = f"ma-demo-{UNIQ}"
    content = f"{tok} 多智能体演示记忆: 团队共享的 WMS 库位优化方案已评审通过，周五上线"
    print("=" * 60)
    print("多智能体共享记忆 + 反馈闭环演示")
    print("=" * 60)

    # 1) alpha 写入
    print("\n[1] agent-alpha 写入工作记忆")
    r = requests.post(f"{API}/memories", json={
        "content": content, "agent_id": "agent-alpha",
        "tags": ["ma-demo", "wms"], "importance": 0.8, "category": "shared_test",
    }, headers=headers("agent-alpha"), timeout=30)
    w = r.json()
    mid = w.get("memory_id")
    print(f"    -> {r.status_code} | memory_id={mid} | error={w.get('error')} | pushed={w.get('pushed_memories')}")
    assert mid, "write failed"

    try:
        # 2) beta 共享检索（无过滤，用英文唯一 token 保证 FTS 可靠召回）
        print("\n[2] agent-beta 共享检索（无 agent 过滤）")
        q = f"ma-demo-{UNIQ}"
        r2 = requests.post(f"{API}/memory/search/hybrid", json={
            "query": q, "top_k": 3, "strategy": "rrf",
        }, headers=headers("agent-beta"), timeout=30)
        res = r2.json().get("results", [])
        hit = any(m.get("memory_id") == mid for m in res)
        print(f"    -> hits={len(res)} | 命中 alpha 记忆: {hit}")
        for m in res[:2]:
            print(f"       - {m.get('memory_id','')[:16]} {(m.get('content_preview') or '')[:40]}")

        # 2b) 隔离对照（带 agent_id 过滤 → 不应命中）
        r2b = requests.post(f"{API}/memory/search/hybrid", json={
            "query": q, "top_k": 3, "strategy": "rrf",
            "agent_id": "agent-beta",
        }, headers=headers("agent-beta"), timeout=30)
        res_b = r2b.json().get("results", [])
        hit_b = any(m.get("memory_id") == mid for m in res_b)
        print(f"    -> 隔离对照（agent_id=beta 过滤）命中 alpha 记忆: {hit_b}（应 False）")

        # 3) gamma 反馈评分
        print("\n[3] agent-gamma 对共享记忆提交反馈")
        r3 = requests.post(f"{API}/evolution/feedback", json={
            "memory_id": mid, "agent_id": "agent-gamma", "rating": 5,
            "comment": "共享记忆质量验证：内容准确且可复用", "context": "ma-demo",
        }, headers=headers("agent-gamma"), timeout=30)
        fb = r3.json()
        print(f"    -> {r3.status_code} | feedback_id={fb.get('feedback_id')} | status={fb.get('status')}")

        # 4) 进化轮
        print("\n[4] 进化引擎跑一轮")
        r4 = requests.post(f"{API}/evolution/cycle/run", headers=headers("agent-alpha"), timeout=180)
        evo = r4.json() if r4.status_code == 200 else {"raw": r4.text[:200]}
        print(f"    -> {r4.status_code} | {json.dumps(evo, ensure_ascii=False)[:200]}")

        # 5) 汇总状态
        print("\n[5] 汇总")
        st = requests.get(f"{API}/evolution/stats", headers=headers("agent-alpha"), timeout=30).json()
        print(f"    进化: cycles={st.get('evolution', {}).get('total_cycles')} | "
              f"feedback 状态见 evolution_state")
        al = requests.get(f"{API}/evolution/quality-alerts", headers=headers("agent-alpha"), timeout=30).json()
        print(f"    quality-alerts: {al}")
    finally:
        # 6) 清理
        requests.delete(f"{API}/memories/{mid}", headers=headers("agent-alpha"), timeout=15)
        print(f"\n[6] 已清理演示记忆 {mid}")

    print("\n[OK] 演示完成：写入 → 跨 agent 共享可见 → 反馈评分 → 进化轮 → 隔离对照")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
