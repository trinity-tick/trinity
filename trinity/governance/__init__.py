"""
Trinity Governance — 多智能体记忆治理策略（B3，2026-08-15）
=============================================================
YAML 策略驱动记忆隔离/共享/委托/审计。

策略 schema:
    policy:
      name: <str>
      description: <str>
      rules:
        - scope: isolated|shared|delegated
          subject: <agent_id | "*">      # 策略适用主体
          target:  <agent_id | "*">      # 访问目标（isolated 时忽略）
          action:  read|write|delegate|* # 允许的动作
          audit:   true|false            # 是否强制审计
      defaults:
        audit: true
        allow:  false                    # 未匹配规则的默认拒绝

用法：
    from trinity.governance import GovernanceEngine
    g = GovernanceEngine("trinity/governance/policies/example.yaml")
    g.check("agent-a", "read", "agent-b")   # -> {"allow": bool, "policy": ...}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.governance")

DEFAULT_POLICIES = Path(__file__).resolve().parent / "policies"


class Policy:
    """单条策略（含 rules 与 defaults）。"""

    def __init__(self, data: Dict[str, Any]):
        self.name = data.get("name", "unnamed")
        self.description = data.get("description", "")
        self.rules: List[Dict[str, Any]] = data.get("rules") or []
        self.defaults: Dict[str, Any] = data.get("defaults") or {"audit": True, "allow": False}

    def match(self, subject: str, action: str, target: str) -> Optional[Dict[str, Any]]:
        """返回匹配的规则（无匹配返回 None）。"""
        for r in self.rules:
            if self._rule_matches(r, subject, action, target):
                return r
        return None

    @staticmethod
    def _specificity(rule: Dict[str, Any]) -> int:
        """规则特异性：具体 subject/target/action（非通配）计分更高。"""
        return sum(1 for k in ("subject", "target", "action") if rule.get(k) not in (None, "*"))

    @staticmethod
    def _rule_matches(rule: Dict[str, Any], subject: str, action: str, target: str) -> bool:
        return (rule.get("subject", "*") in (subject, "*")
                and rule.get("action", "*") in (action, "*")
                and rule.get("target", "*") in (target, "*"))

    def decide(self, subject: str, action: str, target: str) -> Dict[str, Any]:
        # 最具体规则优先（具体共享/委托 > 通配隔离），否则用 defaults
        matched = [r for r in self.rules if self._rule_matches(r, subject, action, target)]
        if not matched:
            return {"allow": bool(self.defaults.get("allow", False)),
                    "policy": self.name, "rule": "defaults", "audit": bool(self.defaults.get("audit", True))}
        rule = max(matched, key=self._specificity)
        # isolated 规则：非同名 target 一律拒绝
        if rule.get("scope") == "isolated" and target != subject and target != "*":
            return {"allow": False, "policy": self.name, "rule": "isolated", "audit": bool(rule.get("audit", True))}
        return {"allow": True, "policy": self.name, "rule": rule.get("scope", "shared"),
                "audit": bool(rule.get("audit", self.defaults.get("audit", True)))}


class GovernanceEngine:
    """多策略治理引擎：按主体-动作-目标裁决 + 审计追踪。"""

    def __init__(self, policy_paths: Optional[List[str]] = None):
        self.policies: List[Policy] = []
        self.audit_log: List[Dict[str, Any]] = []
        if policy_paths:
            for p in policy_paths:
                self.load_policy(p)

    def load_policy(self, path: str) -> Policy:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        pol = Policy(data.get("policy", data))
        self.policies.append(pol)
        logger.info("policy loaded: %s (%d rules)", pol.name, len(pol.rules))
        return pol

    def clear_policies(self) -> None:
        """热切换：清空后加载新策略集。"""
        self.policies = []

    def check(self, subject: str, action: str, target: str,
              force_audit: bool = False) -> Dict[str, Any]:
        """裁决访问：按策略顺序取第一个匹配的决策。"""
        decision = {"allow": False, "policy": None, "rule": "no-policy", "audit": True}
        for pol in self.policies:
            d = pol.decide(subject, action, target)
            decision = d
            break  # 首个策略生效（策略顺序即优先级）
        if decision.get("audit") or force_audit:
            self.audit_log.append({
                "subject": subject, "action": action, "target": target,
                "allow": decision["allow"], "policy": decision["policy"],
                "rule": decision["rule"],
            })
        return decision

    def summary(self) -> Dict[str, Any]:
        return {
            "policies": [p.name for p in self.policies],
            "audit_entries": len(self.audit_log),
            "denied": sum(1 for e in self.audit_log if not e["allow"]),
        }
