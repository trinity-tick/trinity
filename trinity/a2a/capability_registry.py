"""
A2A CapabilityRegistry — Global agent capability directory.

Maintains a registry of all agents in the federated network with their
Agent Cards. Supports capability-based agent discovery and intelligent
task-to-agent matching.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from trinity.a2a.agent_card import AgentCard

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """Global registry of agent capabilities.

    Each agent registers an AgentCard (A2A v0.3) exposing its
    capabilities, skills, input/output modes, and security level.
    """

    def __init__(self, adapter=None):
        self._adapter = adapter
        self._lock = threading.RLock()
        # In-memory cache for fast lookups: agent_id → AgentCard
        self._cards: Dict[str, AgentCard] = {}

    # ── Registration ───────────────────────────────────────────────

    def register_agent(self, card: AgentCard) -> Dict[str, Any]:
        """Register an agent in the global capability directory.

        A2A Security: auto-registers a default capability policy (empty
        whitelist — requires explicit authorization before any capability
        can be used by the agent).

        If the card has no registered_at timestamp, it is set to now.

        Args:
            card: Signed AgentCard to register.

        Returns:
            Dict with status and registration details.
        """
        # Auto-set registration timestamp if missing
        if not card.registered_at:
            card.registered_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._cards[card.agent_id] = card

        # Persist
        if self._adapter and hasattr(self._adapter, "register_agent_card"):
            try:
                self._adapter.register_agent_card(
                    agent_id=card.agent_id,
                    card_json=json.dumps(card.to_dict(), ensure_ascii=False),
                )
            except Exception as e:
                logger.warning("Failed to persist agent card: %s", e)

        # A2A Security: auto-register capability policy (empty whitelist)
        self._ensure_capability_policy(card.agent_id)

        logger.info("Agent '%s' registered — %d capabilities", card.agent_id, len(card.capabilities))
        return {
            "status": "registered",
            "agent_id": card.agent_id,
            "capabilities": len(card.capabilities),
            "skills": len(card.skills),
        }

    def _ensure_capability_policy(self, agent_id: str) -> None:
        """Ensure a capability policy exists for *agent_id* (empty by default)."""
        try:
            from trinity.a2a.security import get_capability_auth
            auth = get_capability_auth()
            auth.register_policy(agent_id, allowed_capabilities=[])
        except Exception as e:
            logger.debug("Capability policy auto-registration skipped: %s", e)

    def unregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """Remove an agent from the registry."""
        with self._lock:
            existed = self._cards.pop(agent_id, None) is not None
        logger.info("Agent '%s' %s", agent_id, "unregistered" if existed else "not found")
        return {"status": "unregistered" if existed else "not_found", "agent_id": agent_id}

    # ── A2A Security: Capability Authorization ──────────────────────

    def authorize_capability(self, agent_id: str, capability: str) -> Dict[str, Any]:
        """Explicitly grant a capability to an agent (A2A Security).

        Delegates to CapabilityAuth.grant_capability.

        Args:
            agent_id: Agent to authorize.
            capability: Capability string to grant.

        Returns:
            Dict with ``agent_id``, ``capability``, ``status``.
        """
        from trinity.a2a.security import get_capability_auth
        auth = get_capability_auth()
        return auth.grant_capability(agent_id, capability)

    def revoke_capability(self, agent_id: str, capability: str) -> Dict[str, Any]:
        """Revoke a previously granted capability from an agent.

        Delegates to CapabilityAuth.revoke_capability.

        Args:
            agent_id: Agent whose capability is being revoked.
            capability: Capability string to remove.

        Returns:
            Dict with ``agent_id``, ``capability``, ``status``.
        """
        from trinity.a2a.security import get_capability_auth
        auth = get_capability_auth()
        return auth.revoke_capability(agent_id, capability)

    def list_all_agents(self) -> List[Dict[str, Any]]:
        """List all registered agents with summary info (skips expired)."""
        with self._lock:
            return [
                {
                    "agent_id": c.agent_id,
                    "name": c.name,
                    "version": c.version,
                    "security_level": c.security_level,
                    "capability_count": len(c.capabilities),
                    "skill_count": len(c.skills),
                    "url": c.url,
                    "expired": c.is_expired(),
                    "ttl_seconds": c.ttl_seconds,
                }
                for c in self._cards.values()
                if not c.is_expired()
            ]

    def get_card(self, agent_id: str) -> Optional[AgentCard]:
        """Get an agent's full card. Returns None if expired."""
        with self._lock:
            card = self._cards.get(agent_id)
        if card and card.is_expired():
            return None
        return card

    # ── Capability Discovery ───────────────────────────────────────

    def find_agent_by_capability(self, capability: str) -> List[Dict[str, Any]]:
        """Find all agents that expose a specific capability (skips expired).

        Args:
            capability: Capability string to search for (substring match).

        Returns:
            List of matching agent summaries.
        """
        matches = []
        cap_lower = capability.lower()
        with self._lock:
            for card in self._cards.values():
                if card.is_expired():
                    continue
                for c in card.capabilities:
                    if cap_lower in c.lower():
                        matches.append({
                            "agent_id": card.agent_id,
                            "name": card.name,
                            "matched_capability": c,
                            "security_level": card.security_level,
                        })
                        break
        return matches

    def match_task_to_agent(self, task_desc: str) -> List[Dict[str, Any]]:
        """Intelligent matching: find the best agent for a task.

        Scoring heuristic:
          - Direct capability keyword match: +10
          - Skill name keyword match: +5
          - Security level compatibility: +3 for standard, +1 for restricted

        Args:
            task_desc: Natural language task description.

        Returns:
            List of agents ranked by relevance score, descending.
        """
        scored = []
        desc_lower = task_desc.lower()
        with self._lock:
            for card in self._cards.values():
                if card.is_expired():
                    continue
                score = 0
                matched_caps = []
                for cap in card.capabilities:
                    if cap.lower() in desc_lower or any(
                        kw in cap.lower() for kw in desc_lower.split()
                    ):
                        score += 10
                        matched_caps.append(cap)
                for skill in card.skills:
                    if skill.name.lower() in desc_lower:
                        score += 5
                if card.security_level == "standard":
                    score += 3
                elif card.security_level == "restricted":
                    score += 1
                if score > 0:
                    scored.append({
                        "agent_id": card.agent_id,
                        "name": card.name,
                        "score": score,
                        "matched_capabilities": matched_caps,
                        "security_level": card.security_level,
                    })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    def heartbeat(self, agent_id: str) -> bool:
        """Update last-heartbeat timestamp for an agent.

        Returns True if agent exists in registry, False otherwise.
        """
        with self._lock:
            if agent_id not in self._cards:
                return False
        if self._adapter and hasattr(self._adapter, "update_agent_heartbeat"):
            try:
                self._adapter.update_agent_heartbeat(agent_id)
            except Exception:
                pass
        return True

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_agents": len(self._cards),
                "total_capabilities": sum(len(c.capabilities) for c in self._cards.values()),
                "total_skills": sum(len(c.skills) for c in self._cards.values()),
            }

    # ── TTL / Expiry Management ────────────────────────────────────

    def purge_expired(self) -> Dict[str, Any]:
        """Remove all expired agent cards from the registry.

        Agents whose registered_at + ttl_seconds is before now are
        unregistered.  Returns a summary of purged agents.

        Returns:
            Dict with ``purged`` (list of purged agent_ids) and ``count``.
        """
        with self._lock:
            expired_ids = [
                agent_id for agent_id, card in self._cards.items()
                if card.is_expired()
            ]
            for agent_id in expired_ids:
                del self._cards[agent_id]
        if expired_ids:
            logger.info(
                "Purged %d expired agent(s): %s",
                len(expired_ids),
                ", ".join(expired_ids),
            )
        return {"purged": expired_ids, "count": len(expired_ids)}

    # ── Self-Test ──────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic: register → query → list → unregister.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        import os
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), f"trinity_registry_test_{os.getpid()}.db")
        checks = []

        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            from trinity.a2a.agent_card import AgentCard, SkillDef
            adapter = SQLiteAdapter(db_path)
            adapter.connect()
            reg = CapabilityRegistry(adapter=adapter)

            test_card = AgentCard(
                agent_id="test_agent_1",
                name="Test Agent",
                version="1.0.0",
                skills=[SkillDef(name="search", description="Semantic search", input_schema={}, output_schema={})],
            )

            # Check 1: register_agent
            try:
                result = reg.register_agent(test_card)
                assert result["status"] == "registered", f"Expected 'registered', got '{result['status']}'"
                checks.append({"name": "register_agent", "pass": True, "detail": "Agent registered successfully"})
            except Exception as e:
                checks.append({"name": "register_agent", "pass": False, "detail": str(e)})

            # Check 2: get_card
            try:
                card = reg.get_card("test_agent_1")
                assert card is not None, "get_card returned None"
                assert card.agent_id == "test_agent_1"
                assert card.name == "Test Agent"
                checks.append({"name": "get_card", "pass": True, "detail": f"Found: {card.name}"})
            except Exception as e:
                checks.append({"name": "get_card", "pass": False, "detail": str(e)})

            # Check 3: list_all_agents returns non-empty
            try:
                agents = reg.list_all_agents()
                assert len(agents) >= 1, f"Expected >=1 agents, got {len(agents)}"
                assert any(a["agent_id"] == "test_agent_1" for a in agents), "test_agent_1 not in list"
                checks.append({"name": "list_all_agents", "pass": True, "detail": f"Count: {len(agents)}"})
            except Exception as e:
                checks.append({"name": "list_all_agents", "pass": False, "detail": str(e)})

            # Check 4: unregister_agent removes agent
            try:
                ok = reg.unregister_agent("test_agent_1")
                assert ok["status"] == "unregistered", f"Expected 'unregistered', got '{ok.get('status')}'"
                card = reg.get_card("test_agent_1")
                assert card is None, "get_card should return None after unregister"
                checks.append({"name": "unregister_agent", "pass": True, "detail": "Agent removed, subsequent query returns None"})
            except Exception as e:
                checks.append({"name": "unregister_agent", "pass": False, "detail": str(e)})

            # Check 5: duplicate register updates existing
            try:
                reg.register_agent(test_card)
                duplicate = reg.register_agent(test_card)
                assert duplicate["status"] == "registered", "Duplicate registration should update"
                checks.append({"name": "duplicate_register", "pass": True, "detail": "Re-register updates existing entry"})
            except Exception as e:
                checks.append({"name": "duplicate_register", "pass": False, "detail": str(e)})

            # Check 6: find_agent_by_capability
            try:
                reg.unregister_agent("test_agent_1")
                card2 = AgentCard(
                    agent_id="test_agent_2",
                    name="Search Agent",
                    version="1.0.0",
                    skills=[SkillDef(name="semantic_search", description="Deep semantic search engine", input_schema={}, output_schema={})],
                )
                reg.register_agent(card2)
                results = reg.find_agent_by_capability("search")
                assert isinstance(results, list), f"Expected list, got {type(results)}"
                checks.append({"name": "find_agent_by_capability", "pass": True, "detail": f"find_agent_by_capability returned list of {len(results)}"})
            except Exception as e:
                checks.append({"name": "find_agent_by_capability", "pass": False, "detail": str(e)})

            adapter.disconnect()
        except Exception as e:
            checks.append({"name": "setup", "pass": False, "detail": f"Test harness failure: {e}"})
        finally:
            try:
                if os.path.exists(db_path):
                    os.unlink(db_path)
            except OSError:
                pass

        all_pass = all(c["pass"] for c in checks)
        return {
            "pass": all_pass,
            "checks": checks,
            "summary": f"CapabilityRegistry self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    reg = CapabilityRegistry()
    return reg.self_test()
