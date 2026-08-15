"""Unit tests for trinity.a2a.security — AgentCardSigner / CapabilityAuth / TaskPermission."""

import os
import tempfile
import pytest

from trinity.a2a.security import (
    AgentCardSigner,
    CapabilityAuth,
    TaskPermission,
)
from trinity.a2a.agent_card import (
    AgentCard,
    generate_card,
    sign_card,
    verify_card,
)


class TestAgentCardSigner:
    """RSA key generation, signing, verification, card hashing."""

    def test_generate_key_pair_creates_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = AgentCardSigner.generate_key_pair(tmpdir)
            assert os.path.isfile(result["private_key_path"])
            assert os.path.isfile(result["public_key_path"])
            assert result["key_size"] == 2048
            assert result["algorithm"] == "RSA-SHA256"

    def test_sign_and_verify_roundtrip(self):
        """sign → verify round-trip must succeed with a valid key pair."""
        card = generate_card("agent-rsa", name="RSA Signer", capabilities=["search"])
        with tempfile.TemporaryDirectory() as tmpdir:
            keys = AgentCardSigner.generate_key_pair(tmpdir)
            priv = keys["private_key_path"]
            pub = keys["public_key_path"]

            signature = AgentCardSigner.sign(card, priv)
            assert isinstance(signature, str)
            assert len(signature) > 0

            valid = AgentCardSigner.verify(card, signature, pub)
            assert valid is True

    def test_verify_tampered_card_fails(self):
        """Verification must fail when the card content changes after signing."""
        card = generate_card("agent-tamper-rsa", name="Tamperer", capabilities=["search"])
        with tempfile.TemporaryDirectory() as tmpdir:
            keys = AgentCardSigner.generate_key_pair(tmpdir)
            priv = keys["private_key_path"]
            pub = keys["public_key_path"]

            signature = AgentCardSigner.sign(card, priv)

            # Tamper with the card
            card.description = "I've been tampered with!"
            valid = AgentCardSigner.verify(card, signature, pub)
            assert valid is False

    def test_verify_with_wrong_key_fails(self):
        """Verification must fail when using a different key pair."""
        card = generate_card("agent-wrong-key", name="Wrong Key", capabilities=["search"])
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            keys1 = AgentCardSigner.generate_key_pair(tmpdir1)
            keys2 = AgentCardSigner.generate_key_pair(tmpdir2)

            signature = AgentCardSigner.sign(card, keys1["private_key_path"])

            valid = AgentCardSigner.verify(card, signature, keys2["public_key_path"])
            assert valid is False

    def test_get_card_hash_is_deterministic(self):
        """get_card_hash must be stable for the same card content."""
        card = generate_card("agent-hash", name="Hash Test", capabilities=["a", "b"])
        h1 = AgentCardSigner.get_card_hash(card)
        h2 = AgentCardSigner.get_card_hash(card)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_cards_have_different_hashes(self):
        """Different card content must produce different hashes."""
        card_a = generate_card("agent-a", name="A", capabilities=["x"])
        card_b = generate_card("agent-b", name="B", capabilities=["y"])
        assert AgentCardSigner.get_card_hash(card_a) != AgentCardSigner.get_card_hash(card_b)

    def test_sign_with_bad_key_path_raises(self):
        """sign must raise when the private key path doesn't exist."""
        card = generate_card("bad-key", name="Bad")
        with pytest.raises((Exception,)):
            AgentCardSigner.sign(card, "/nonexistent/private.pem")


class TestCapabilityAuth:
    """Capability authorization: register, authorize, revoke, policy."""

    def test_register_policy_empty_by_default(self):
        auth = CapabilityAuth()
        result = auth.register_policy("agent-1")
        assert result["agent_id"] == "agent-1"
        assert result["capability_count"] == 0
        assert result["status"] == "registered"

    def test_register_policy_with_initial_caps(self):
        auth = CapabilityAuth()
        result = auth.register_policy("agent-2", allowed_capabilities=["search", "read"])
        assert result["capability_count"] == 2

    def test_unauthorized_capability_denied(self):
        """Default policy: new agents have zero capabilities."""
        auth = CapabilityAuth()
        auth.register_policy("agent-3")
        assert auth.authorize("agent-3", "search") is False
        assert auth.authorize("agent-3", "delete") is False

    def test_authorized_capability_passed(self):
        """After explicit grant, capability must be authorized."""
        auth = CapabilityAuth()
        auth.register_policy("agent-4", allowed_capabilities=["search"])
        assert auth.authorize("agent-4", "search") is True
        assert auth.authorize("agent-4", "delete") is False

    def test_grant_capability_adds_to_whitelist(self):
        auth = CapabilityAuth()
        auth.register_policy("agent-5")
        result = auth.grant_capability("agent-5", "search")
        assert result["status"] == "authorized"
        assert auth.authorize("agent-5", "search") is True

    def test_revoke_capability_removes_from_whitelist(self):
        auth = CapabilityAuth()
        auth.register_policy("agent-6", allowed_capabilities=["search", "read"])
        result = auth.revoke_capability("agent-6", "search")
        assert result["status"] == "revoked"
        assert result["was_present"] is True
        assert auth.authorize("agent-6", "search") is False
        assert auth.authorize("agent-6", "read") is True

    def test_revoke_nonexistent_capability(self):
        auth = CapabilityAuth()
        auth.register_policy("agent-7", allowed_capabilities=["read"])
        result = auth.revoke_capability("agent-7", "write")
        assert result["status"] == "not_found"
        assert result["was_present"] is False

    def test_get_effective_capabilities(self):
        auth = CapabilityAuth()
        auth.register_policy("agent-8", allowed_capabilities=["b", "c", "a"])
        caps = auth.get_effective_capabilities("agent-8")
        assert caps == ["a", "b", "c"]  # sorted

    def test_get_effective_capabilities_unknown_agent(self):
        auth = CapabilityAuth()
        caps = auth.get_effective_capabilities("nonexistent")
        assert caps == []

    def test_get_agent_policy(self):
        auth = CapabilityAuth()
        auth.register_policy("agent-9", allowed_capabilities=["search"])
        policy = auth.get_agent_policy("agent-9")
        assert policy["agent_id"] == "agent-9"
        assert policy["capabilities"] == ["search"]
        assert policy["count"] == 1

    def test_grant_auto_creates_policy(self):
        """grant_capability must auto-create policy if agent doesn't have one."""
        auth = CapabilityAuth()
        result = auth.grant_capability("agent-auto", "read")
        assert result["status"] == "authorized"
        assert auth.authorize("agent-auto", "read") is True


class TestTaskPermission:
    """Task-level permissions: create, read, cancel, grant, revoke."""

    def test_can_create_task_default_true(self):
        tp = TaskPermission()
        assert tp.can_create_task("alice", "bob") is True
        assert tp.can_create_task("charlie", "dave") is True

    def test_creator_can_read_own_task(self):
        tp = TaskPermission()
        tp.register_task("t-1", "alice", "bob")
        assert tp.can_read_task("alice", "t-1") is True   # creator

    def test_assignee_can_read_task(self):
        tp = TaskPermission()
        tp.register_task("t-2", "alice", "bob")
        assert tp.can_read_task("bob", "t-2") is True     # assignee

    def test_stranger_cannot_read_task(self):
        """Agent C who is neither creator nor assignee cannot read."""
        tp = TaskPermission()
        tp.register_task("t-3", "alice", "bob")
        assert tp.can_read_task("charlie", "t-3") is False

    def test_creator_can_cancel_task(self):
        tp = TaskPermission()
        tp.register_task("t-4", "alice", "bob")
        assert tp.can_cancel_task("alice", "t-4") is True

    def test_assignee_cannot_cancel_task(self):
        """Assignees cannot cancel — only creators and superiors can."""
        tp = TaskPermission()
        tp.register_task("t-5", "alice", "bob")
        assert tp.can_cancel_task("bob", "t-5") is False

    def test_non_creator_cannot_cancel(self):
        tp = TaskPermission()
        tp.register_task("t-6", "alice", "bob")
        assert tp.can_cancel_task("charlie", "t-6") is False

    def test_superior_can_cancel_task(self):
        tp = TaskPermission()
        tp.register_task("t-7", "alice", "bob", superiors=["admin"])
        assert tp.can_cancel_task("admin", "t-7") is True

    def test_superior_can_read_task(self):
        tp = TaskPermission()
        tp.register_task("t-8", "alice", "bob", superiors=["admin"])
        assert tp.can_read_task("admin", "t-8") is True

    def test_grant_task_access(self):
        tp = TaskPermission()
        tp.register_task("t-9", "alice", "bob")
        assert tp.can_read_task("charlie", "t-9") is False
        result = tp.grant_task_access("t-9", "charlie")
        assert result["status"] == "granted"
        assert tp.can_read_task("charlie", "t-9") is True

    def test_revoke_task_access(self):
        tp = TaskPermission()
        tp.register_task("t-10", "alice", "bob")
        tp.grant_task_access("t-10", "charlie")
        assert tp.can_read_task("charlie", "t-10") is True
        result = tp.revoke_task_access("t-10", "charlie")
        assert result["status"] == "revoked"
        assert tp.can_read_task("charlie", "t-10") is False

    def test_cannot_revoke_creator(self):
        tp = TaskPermission()
        tp.register_task("t-11", "alice", "bob")
        result = tp.revoke_task_access("t-11", "alice")
        assert result["status"] == "error"
        assert "Cannot revoke creator" in result["detail"]

    def test_task_permission_isolation(self):
        """Agent A cannot read Agent B's tasks."""
        tp = TaskPermission()
        tp.register_task("t-a", "alice", "target_a")
        tp.register_task("t-b", "bob", "target_b")
        assert tp.can_read_task("alice", "t-a") is True
        assert tp.can_read_task("alice", "t-b") is False
        assert tp.can_read_task("bob", "t-b") is True
        assert tp.can_read_task("bob", "t-a") is False

    def test_get_task_acl(self):
        tp = TaskPermission()
        tp.register_task("t-12", "alice", "bob", superiors=["admin"])
        tp.grant_task_access("t-12", "charlie")
        acl = tp.get_task_acl("t-12")
        assert acl is not None
        assert acl["creator"] == "alice"
        assert acl["assignee"] == "bob"
        assert "charlie" in acl["guests"]
        assert "admin" in acl["superiors"]

    def test_get_task_acl_nonexistent(self):
        tp = TaskPermission()
        acl = tp.get_task_acl("nonexistent")
        assert acl is None

    def test_invalid_task_denied_read(self):
        """Reading a task with no ACL entry must be denied."""
        tp = TaskPermission()
        assert tp.can_read_task("anyone", "nonexistent-task") is False

    def test_invalid_task_denied_cancel(self):
        tp = TaskPermission()
        assert tp.can_cancel_task("anyone", "nonexistent-task") is False
