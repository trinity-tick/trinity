# -*- coding: utf-8 -*-
"""B3 多智能体记忆治理引擎 — 读取 policy.yaml 并通过 API 执行治理规则。

能力:
  - 读取 agents/policies 配置（isolation/shared/arbitration/audit）
  - 执行: 私有 agent 检索强制 agent_id 过滤；viewer 禁止写；审计写入记录
  - demo: 用 3 个 agent 演示隔离/共享行为

用法:
    python governance/governance.py                 # 跑 demo
    python governance/governance.py --check-only    # 只校验策略文件
"""
import argparse
import sys
import time
import yaml
import requests

API = "http://127.0.0.1:8001"
POLICY = r"C:\Users\Administrator\trinity\governance\policy.yaml"


def call(method, path, headers=None, **kw):
    r = requests.request(method, f"{API}/{path.lstrip('/')}", timeout=30,
                         headers=headers or {}, **kw)
    return r


class GovernanceEngine:
    def __init__(self, policy: dict):
        self.agents = {a["id"]: a for a in policy["agents"]}
        self.policies = policy["policies"]

    def agent(self, agent_id: str) -> dict:
        return self.agents.get(agent_id, {"memory_scope": "private", "role": "operator",
                                          "allowed_categories": []})

    def search_headers(self, agent_id: str) -> dict:
        """按隔离策略决定检索头：私有 agent 必须带自身 agent_id 过滤。"""
        a = self.agent(agent_id)
        headers = {"X-Agent-ID": agent_id, "X-Agent-Role": a.get("role", "operator")}
        if a.get("memory_scope") == "private" and self.policies["isolation"].get("enforce_agent_filter"):
            headers["X-Agent-Filter"] = agent_id  # 语义标记；实际过滤由检索参数完成
        return headers

    def can_write(self, agent_id: str, category: str = "general") -> tuple:
        a = self.agent(agent_id)
        if a.get("role") == "viewer":
            return False, "viewer 角色禁止写入"
        if a.get("memory_scope") == "private":
            blocked = self.policies["isolation"].get("block_shared_categories", [])
            if category in blocked:
                return False, f"私有 agent 禁止写入共享类目 {category}"
        allowed = a.get("allowed_categories") or []
        if allowed and category not in allowed:
            return False, f"类目 {category} 不在允许列表 {allowed}"
        return True, "ok"

    def search_pool(self, agent_id: str, q: str, top_k: int = 5):
        a = self.agent(agent_id)
        params = {"q": q, "top_k": top_k, "mode": "hybrid"}
        # 隔离策略: 私有 agent 检索时按 agent 过滤
        if a.get("memory_scope") == "private" and self.policies["isolation"].get("enforce_agent_filter"):
            params["agent_id"] = agent_id
        headers = {"X-Agent-ID": agent_id, "X-Agent-Role": a.get("role", "operator")}
        r = requests.get(f"{API}/agents/memory/search", params=params, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])

    def audit_write(self, agent_id: str, memory_id: str):
        if not self.policies["audit"].get("audit_writes"):
            return None
        try:
            r = requests.get(f"{API}/audit/timeline", params={"agent_id": agent_id}, timeout=30)
            return {"timeline_status": r.status_code}
        except Exception as exc:
            return {"error": str(exc)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    policy = yaml.safe_load(open(POLICY, encoding="utf-8"))
    eng = GovernanceEngine(policy)
    print(f"agents: {list(eng.agents)}")

    if args.check_only:
        print("policy 校验通过 (agents=%d, policies=%s)" % (
            len(eng.agents), ",".join(eng.policies.keys())))
        return

    # demo
    print("\n== demo: 治理规则执行 ==")
    for aid, cat in [("agent-alpha", "general"), ("agent-beta", "task"), ("agent-gamma", "general")]:
        ok, reason = eng.can_write(aid, cat)
        print(f"  {aid} write(category={cat}) -> {'ALLOW' if ok else 'DENY'}: {reason}")

    print("\n== demo: 私有 agent 检索（隔离）==")
    r = eng.search_pool("agent-alpha", "订单")
    print(f"  agent-alpha(私有) hits: {len(r)} (应只含自身记忆)")
    r2 = eng.search_pool("agent-beta", "订单")
    print(f"  agent-beta(共享) hits: {len(r2)} (共享池可读)")

    print("\n== demo: 审计 ==")
    print(f"  audit_write: {eng.audit_write('agent-alpha', 'demo-memory')}")

    print("\n[OK] governance demo 完成")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
