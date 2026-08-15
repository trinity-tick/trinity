"""Trinity — 治理引擎单元测试（B3, 2026-08-15）。

覆盖：
- 隔离默认：同 agent 允许、跨 agent 拒绝
- 规则特异性：具体共享规则覆盖通配隔离规则（不依赖规则顺序）
- 委托动作白名单
- defaults.allow=false 兜底拒绝
- 审计记录与 summary
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trinity.governance import GovernanceEngine, Policy

_ROOT = Path(__file__).resolve().parent.parent.parent
POLICIES = _ROOT / "trinity" / "governance" / "policies"


@pytest.fixture()
def engine() -> GovernanceEngine:
    return GovernanceEngine()


def test_isolated_same_agent_allowed(engine: GovernanceEngine) -> None:
    engine.load_policy(str(POLICIES / "isolation.yaml"))
    assert engine.check("beta", "write", "beta")["allow"] is True


def test_isolated_cross_agent_denied(engine: GovernanceEngine) -> None:
    engine.load_policy(str(POLICIES / "isolation.yaml"))
    assert engine.check("alpha", "read", "beta")["allow"] is False


def test_specific_shared_overrides_catchall_isolated(engine: GovernanceEngine) -> None:
    """最具体规则优先：具体 shared 规则必须覆盖通配 isolated 规则，
    且与 YAML 中的规则顺序无关（共享规则放在隔离规则之后也能生效）。"""
    engine.load_policy(str(POLICIES / "example.yaml"))
    assert engine.check("alpha", "read", "beta")["allow"] is True


def test_delegate_allowed(engine: GovernanceEngine) -> None:
    engine.load_policy(str(POLICIES / "example.yaml"))
    assert engine.check("gamma", "delegate", "alpha")["allow"] is True


def test_unlisted_action_default_deny(engine: GovernanceEngine) -> None:
    """example.yaml 未显式授权的动作（如 write 跨 agent）→ 隔离兜底拒绝。"""
    engine.load_policy(str(POLICIES / "example.yaml"))
    # alpha 写 beta 未被共享规则授权 → 命中通配 isolated → deny
    assert engine.check("alpha", "write", "beta")["allow"] is False


def test_no_policy_default_deny(engine: GovernanceEngine) -> None:
    assert engine.check("x", "read", "y")["allow"] is False


def test_audit_log_and_summary(engine: GovernanceEngine) -> None:
    engine.load_policy(str(POLICIES / "example.yaml"))
    engine.check("alpha", "read", "beta")      # allow
    engine.check("gamma", "read", "delta")     # deny
    s = engine.summary()
    assert s["audit_entries"] == 2
    assert s["denied"] == 1


def test_policy_match_most_specific() -> None:
    """Policy.decide 必须返回最具体规则的决策（specificity 优先）。"""
    pol = Policy({
        "name": "t",
        "rules": [
            {"scope": "isolated", "subject": "*", "target": "*", "action": "*"},
            {"scope": "shared", "subject": "alpha", "target": "beta", "action": "read"},
        ],
    })
    d = pol.decide("alpha", "read", "beta")
    assert d["allow"] is True
    assert d["rule"] == "shared"


def test_hot_swap(engine: GovernanceEngine) -> None:
    """热切换：隔离 → 共享，同一次访问从 deny 变 allow。"""
    engine.load_policy(str(POLICIES / "isolation.yaml"))
    assert engine.check("alpha", "read", "beta")["allow"] is False
    engine.clear_policies()
    engine.load_policy(str(POLICIES / "example.yaml"))
    assert engine.check("alpha", "read", "beta")["allow"] is True
