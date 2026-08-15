"""Unit tests for trinity.a2a package — Agent Card / TaskManager / CapabilityRegistry."""

import json
import threading
import pytest

from trinity.a2a.agent_card import (
    AgentCard,
    SkillDef,
    sign_card,
    verify_card,
    generate_card,
)
from trinity.a2a.task_manager import TaskManager, TaskState, A2ATask
from trinity.a2a.capability_registry import CapabilityRegistry


class TestAgentCard:
    """AgentCard creation, serialization, signature."""

    def test_create_card_with_minimal_fields(self):
        card = AgentCard(agent_id="agent-1", name="Trinity Test")
        assert card.agent_id == "agent-1"
        assert card.capabilities == []

    def test_generate_card_includes_capabilities(self):
        card = generate_card("agent-abc", name="Tester", capabilities=["search", "summarize"])
        assert card.agent_id == "agent-abc"
        assert "search" in card.capabilities
        assert "summarize" in card.capabilities
        assert card.input_modes == ["json", "text"]

    def test_sign_and_verify_valid(self):
        card = generate_card("agent-sig", name="Signer")
        result = verify_card(card)
        assert result["valid"] is True

    def test_verify_tampered_card_fails(self):
        card = generate_card("agent-tamper", name="Tamperer")
        card.description = "Tampered!"
        result = verify_card(card)
        assert result["valid"] is False
        assert "mismatch" in result["detail"].lower()

    def test_from_dict_preserves_skills(self):
        card_dict = {
            "agent_id": "agent-d",
            "name": "Dict Agent",
            "version": "1.0.0",
            "capabilities": ["read"],
            "endpoints": {"health": "/health"},
            "skills": [{"name": "search", "description": "Full-text search"}],
            "input_modes": ["text"],
            "output_modes": ["text"],
            "security_level": "standard",
            "signed_card": "abc",
            "url": "http://localhost",
        }
        card = AgentCard.from_dict(card_dict)
        assert card.agent_id == "agent-d"
        assert card.capabilities == ["read"]
        assert len(card.skills) == 1
        assert card.skills[0].name == "search"

    def test_empty_agent_id_still_creates_card(self):
        card = AgentCard(agent_id="")
        assert card.agent_id == ""


class TestTaskManager:
    """TaskManager state machine lifecycle."""

    def test_create_task_returns_a2a_task(self, task_manager):
        task = task_manager.create_task("agent-a", "agent-b", {"method": "echo"})
        assert isinstance(task, A2ATask)
        assert task.status == "pending"
        assert task.from_agent == "agent-a"
        assert task.to_agent == "agent-b"
        assert task.task_id.startswith("task_")

    def test_pending_to_in_progress_to_completed(self, task_manager):
        t = task_manager.create_task("a", "b", {"x": 1})
        tid = t.task_id
        updated = task_manager.update_task(tid, "in_progress")
        assert updated is not None
        assert updated["status"] == "in_progress"
        completed = task_manager.update_task(tid, "completed", {"result": "ok"})
        assert completed is not None
        assert completed["status"] == "completed"

    def test_pending_to_cancelled(self, task_manager):
        t = task_manager.create_task("a", "b", {})
        result = task_manager.cancel_task(t.task_id)
        assert result is not None
        assert result["status"] == "cancelled"

    def test_pending_to_failed(self, task_manager):
        t = task_manager.create_task("a", "b", {})
        result = task_manager.update_task(t.task_id, "in_progress")
        assert result["status"] == "in_progress"
        failed = task_manager.update_task(t.task_id, "failed", {"error": "timeout"})
        assert failed["status"] == "failed"

    def test_invalid_transition_rejected(self, task_manager):
        """completed → in_progress should be rejected."""
        t = task_manager.create_task("a", "b", {})
        task_manager.update_task(t.task_id, "in_progress")
        task_manager.update_task(t.task_id, "completed")
        result = task_manager.update_task(t.task_id, "in_progress")
        assert result is None

    def test_query_nonexistent_task(self, task_manager):
        assert task_manager.query_task("nonexistent-999") is None

    def test_concurrent_task_creation(self, task_manager):
        """Multiple threads creating tasks should not corrupt state."""
        errors = []

        def creator():
            try:
                task_manager.create_task("a", "b", {"thread": threading.get_ident()})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=creator) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        stats = task_manager.get_stats()
        assert stats["total_created"] == 10


class TestCapabilityRegistry:
    """CapabilityRegistry agent registration, discovery, matching."""

    def test_register_agent_succeeds(self, capability_registry):
        card = generate_card("agent-x", name="X Agent", capabilities=["read", "write"])
        result = capability_registry.register_agent(card)
        assert result["status"] == "registered"

    def test_unregister_agent(self, capability_registry):
        card = generate_card("agent-y", name="Y Agent")
        capability_registry.register_agent(card)
        result = capability_registry.unregister_agent("agent-y")
        assert result["status"] == "unregistered"

    def test_unregister_nonexistent(self, capability_registry):
        result = capability_registry.unregister_agent("ghost-agent")
        assert result["status"] == "not_found"

    def test_list_all_agents(self, capability_registry):
        for i in range(3):
            capability_registry.register_agent(generate_card(f"agent-{i}"))
        agents = capability_registry.list_all_agents()
        assert len(agents) == 3

    def test_find_agent_by_capability(self, capability_registry):
        capability_registry.register_agent(generate_card("a1", capabilities=["search"]))
        capability_registry.register_agent(generate_card("a2", capabilities=["summarize"]))
        results = capability_registry.find_agent_by_capability("search")
        assert len(results) == 1
        assert results[0]["agent_id"] == "a1"

    def test_match_task_to_agent_scores(self, capability_registry):
        capability_registry.register_agent(generate_card("searcher", capabilities=["full_text_search"]))
        matches = capability_registry.match_task_to_agent("perform full_text_search on database")
        assert len(matches) >= 1
        assert matches[0]["agent_id"] == "searcher"

    def test_heartbeat_existing_agent(self, capability_registry):
        capability_registry.register_agent(generate_card("live-agent"))
        assert capability_registry.heartbeat("live-agent") is True

    def test_heartbeat_nonexistent_agent(self, capability_registry):
        assert capability_registry.heartbeat("dead-agent") is False

    def test_get_stats(self, capability_registry):
        capability_registry.register_agent(generate_card("s1", capabilities=["a", "b"]))
        capability_registry.register_agent(generate_card("s2", capabilities=["c"]))
        stats = capability_registry.get_stats()
        assert stats["total_agents"] == 2
        assert stats["total_capabilities"] == 3

    def test_get_card_not_found(self, capability_registry):
        assert capability_registry.get_card("nonexistent") is None
