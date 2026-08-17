"""
A2A Security Module — AgentCard Signing, Capability Authorization, Task Permissions.

Provides RSA-based cryptographic signing for Agent Cards, capability-level
access control (whitelist-based authorization), and task-level permission
management aligned with the Anthropic MCP security model.

Key components:
  - AgentCardSigner: RSA-2048 sign/verify for AgentCard integrity
  - CapabilityAuth: agent capability whitelist with explicit authorization
  - TaskPermission: per-task ACL with creator/assignee/guest roles
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from trinity.a2a.agent_card import AgentCard

logger = logging.getLogger(__name__)

# ── RSA Key Utilities ────────────────────────────────────────────────────

_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_AVAILABLE = True
except ImportError:
    logger.warning("cryptography not installed; RSA signing disabled. Run: pip install cryptography")


def _load_private_key(path: str):
    """Load an RSA private key from a PEM file."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library not installed")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def _load_public_key(path: str):
    """Load an RSA public key from a PEM file."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library not installed")
    with open(path, "rb") as f:
        return serialization.load_pem_public_key(f.read(), backend=default_backend())


# ── AgentCardSigner ──────────────────────────────────────────────────────

class AgentCardSigner:
    """RSA-based cryptographic signing and verification for Agent Cards.

    Provides asymmetric (RSA-2048) signing so that any agent in the
    federated network can independently verify a card's authenticity
    without sharing a symmetric secret.

    Usage::

        signer = AgentCardSigner()
        signer.generate_key_pair("/path/to/keys")
        sig = signer.sign(card, "/path/to/keys/private.pem")
        valid = signer.verify(card, sig, "/path/to/keys/public.pem")
    """

    KEY_SIZE = 2048
    HASH_ALGORITHM = "SHA-256"

    @staticmethod
    def generate_key_pair(output_dir: str) -> Dict[str, str]:
        """Generate an RSA-2048 key pair and save to *output_dir*.

        Creates two PEM files:
          - ``{output_dir}/private.pem`` (PKCS#8, no password)
          - ``{output_dir}/public.pem``  (SubjectPublicKeyInfo)

        Args:
            output_dir: Directory path that must already exist.

        Returns:
            Dict with ``private_key_path`` and ``public_key_path``.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography library not installed. Run: pip install cryptography")
        os.makedirs(output_dir, exist_ok=True)

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=AgentCardSigner.KEY_SIZE,
            backend=default_backend(),
        )

        private_path = os.path.join(output_dir, "private.pem")
        public_path = os.path.join(output_dir, "public.pem")

        with open(private_path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        with open(public_path, "wb") as f:
            f.write(private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ))

        logger.info("RSA-%d key pair generated in %s", AgentCardSigner.KEY_SIZE, output_dir)
        return {
            "private_key_path": private_path,
            "public_key_path": public_path,
            "key_size": AgentCardSigner.KEY_SIZE,
            "algorithm": "RSA-SHA256",
        }

    @staticmethod
    def get_card_hash(card: AgentCard) -> str:
        """Compute the SHA-256 hex digest of an AgentCard (signature-excluded payload).

        Args:
            card: The AgentCard to hash.

        Returns:
            64-character lowercase hex digest.
        """
        payload = json.dumps(
            card.to_dict(include_signature=False), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def sign(card: AgentCard, private_key_path: str) -> str:
        """Sign an AgentCard with the RSA private key at *private_key_path*.

        The signature covers ``SHA-256(card.to_dict(include_signature=False))``.

        Args:
            card: AgentCard to sign.
            private_key_path: Path to the RSA private key PEM file.

        Returns:
            Hex-encoded RSA signature string.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed. Run: pip install cryptography")

        private_key = _load_private_key(private_key_path)
        card_hash_bytes = bytes.fromhex(AgentCardSigner.get_card_hash(card))

        signature = private_key.sign(
            card_hash_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return signature.hex()

    @staticmethod
    def verify(card: AgentCard, signature: str, public_key_path: str) -> bool:
        """Verify an AgentCard's RSA signature.

        Args:
            card: AgentCard whose signature is being verified.
            signature: Hex-encoded RSA signature.
            public_key_path: Path to the RSA public key PEM file.

        Returns:
            True if the signature is valid, False otherwise.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed. Run: pip install cryptography")

        try:
            public_key = _load_public_key(public_key_path)
            card_hash_bytes = bytes.fromhex(AgentCardSigner.get_card_hash(card))
            signature_bytes = bytes.fromhex(signature)

            public_key.verify(
                signature_bytes,
                card_hash_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception as e:
            logger.debug("RSA signature verification failed: %s", e)
            return False


# ── CapabilityAuth ───────────────────────────────────────────────────────

class CapabilityAuth:
    """Agent-capability authorization with whitelist-based access control.

    Default policy (aligned with Anthropic MCP): a newly registered agent
    has **zero** capabilities — every capability must be explicitly granted
    via ``authorize()``.

    Usage::

        auth = CapabilityAuth()
        auth.register_policy("agent-1", allowed={"search", "summarize"})
        ok = auth.authorize("agent-1", "search")   # True
        ok = auth.authorize("agent-1", "delete")   # False
    """

    def __init__(self):
        self._lock = threading.RLock()
        # agent_id → set of explicitly allowed capabilities
        self._policies: Dict[str, Set[str]] = {}

    def register_policy(self, agent_id: str, allowed_capabilities: Optional[List[str]] = None) -> Dict[str, Any]:
        """Register (or overwrite) the capability whitelist for *agent_id*.

        If *allowed_capabilities* is None / empty, the agent starts with
        zero capabilities — every access must be explicitly granted later.

        Args:
            agent_id: Agent identifier.
            allowed_capabilities: Initial list of allowed capability strings.

        Returns:
            Dict with ``agent_id``, ``capability_count``, ``policy``.
        """
        caps = set(allowed_capabilities or [])
        with self._lock:
            self._policies[agent_id] = caps
        logger.info("Capability policy registered for '%s': %d allowed", agent_id, len(caps))
        return {
            "agent_id": agent_id,
            "capability_count": len(caps),
            "policy": "whitelist",
            "status": "registered",
        }

    def authorize(self, agent_id: str, requested_capability: str) -> bool:
        """Check whether *agent_id* is authorized for *requested_capability*.

        Args:
            agent_id: Agent requesting the capability.
            requested_capability: The capability string being requested.

        Returns:
            True if the capability is in the agent's whitelist.
        """
        with self._lock:
            allowed = self._policies.get(agent_id, set())
        return requested_capability in allowed

    def grant_capability(self, agent_id: str, capability: str) -> Dict[str, Any]:
        """Explicitly grant a capability to an agent.

        If *agent_id* doesn't have a policy yet, one is created automatically
        with only *capability* as allowed.

        Args:
            agent_id: Agent to authorize.
            capability: Capability string to grant.

        Returns:
            Dict with ``agent_id``, ``capability``, ``status``.
        """
        with self._lock:
            if agent_id not in self._policies:
                self._policies[agent_id] = set()
            self._policies[agent_id].add(capability)
        logger.info("Capability '%s' granted to agent '%s'", capability, agent_id)
        return {"agent_id": agent_id, "capability": capability, "status": "authorized"}

    def revoke_capability(self, agent_id: str, capability: str) -> Dict[str, Any]:
        """Revoke a previously granted capability.

        Args:
            agent_id: Agent whose capability is being revoked.
            capability: Capability string to remove.

        Returns:
            Dict with ``agent_id``, ``capability``, ``status``, ``was_present``.
        """
        with self._lock:
            caps = self._policies.get(agent_id, set())
            was_present = capability in caps
            caps.discard(capability)
        logger.info("Capability '%s' %s agent '%s'", capability,
                     "revoked from" if was_present else "was not present for", agent_id)
        return {
            "agent_id": agent_id,
            "capability": capability,
            "status": "revoked" if was_present else "not_found",
            "was_present": was_present,
        }

    def get_effective_capabilities(self, agent_id: str) -> List[str]:
        """Return the sorted list of capabilities currently authorized for *agent_id*.

        Args:
            agent_id: Agent identifier.

        Returns:
            Sorted list of capability strings (empty if no policy exists).
        """
        with self._lock:
            return sorted(self._policies.get(agent_id, set()))

    def get_agent_policy(self, agent_id: str) -> Dict[str, Any]:
        """Get the full policy snapshot for diagnostics.

        Returns:
            Dict with ``agent_id``, ``capabilities``, ``count``.
        """
        caps = self.get_effective_capabilities(agent_id)
        return {"agent_id": agent_id, "capabilities": caps, "count": len(caps)}


# ── TaskPermission ───────────────────────────────────────────────────────

@dataclass
class TaskACL:
    """Access control list for a single A2A task."""
    task_id: str
    creator: str           # agent that created the task
    assignee: str          # agent the task is assigned to
    # Additional agents explicitly granted access
    guests: Set[str] = field(default_factory=set)
    # Parent / superior agents that can override
    superiors: Set[str] = field(default_factory=set)


class TaskPermission:
    """Task-level permission manager for A2A cross-agent tasks.

    Controls which agents can create, read, or cancel specific tasks
    using role-based access with creator / assignee / guest / superior roles.

    Usage::

        tp = TaskPermission()
        tp.can_create_task("alice", "bob")            # True
        tp.register_task("task-1", "alice", "bob")
        tp.can_read_task("alice", "task-1")           # True (creator)
        tp.can_read_task("charlie", "task-1")         # False
        tp.grant_task_access("task-1", "charlie")
        tp.can_read_task("charlie", "task-1")         # True
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._acls: Dict[str, TaskACL] = {}

    # ── Task Registration (called by TaskManager) ────────────────────

    def register_task(self, task_id: str, creator: str, assignee: str,
                      superiors: Optional[List[str]] = None) -> TaskACL:
        """Register a new task ACL entry.

        Called by TaskManager when a task is created.

        Args:
            task_id: Unique task identifier.
            creator: Agent that created the task.
            assignee: Agent the task is assigned to.
            superiors: Optional list of superior agent IDs.

        Returns:
            The newly created TaskACL.
        """
        acl = TaskACL(
            task_id=task_id,
            creator=creator,
            assignee=assignee,
            superiors=set(superiors or []),
        )
        with self._lock:
            self._acls[task_id] = acl
        logger.debug("ACL registered for task %s: creator=%s assignee=%s", task_id, creator, assignee)
        return acl

    # ── Permission Checks ───────────────────────────────────────────

    def can_create_task(self, from_agent: str, to_agent: str) -> bool:
        """Check whether *from_agent* can create a task targeting *to_agent*.

        Default policy: any registered agent can create a task for any other
        agent — **unless** the target agent has been globally locked or the
        source agent is explicitly banned.  (Future extension point.)

        Args:
            from_agent: Originating agent ID.
            to_agent: Target agent ID.

        Returns:
            True if task creation is allowed.
        """
        # Baseline: allow all inter-agent task creation.
        # Future: check against global deny-lists, rate limits, etc.
        logger.debug("Task creation check: %s → %s — allowed", from_agent, to_agent)
        return True

    def can_read_task(self, agent_id: str, task_id: str) -> bool:
        """Check whether *agent_id* can read *task_id*.

        Allowed if the agent is the **creator**, the **assignee**,
        a **superior**, or an explicitly granted **guest**.

        Args:
            agent_id: Agent requesting read access.
            task_id: Task identifier.

        Returns:
            True if read access is permitted.
        """
        with self._lock:
            acl = self._acls.get(task_id)
        if acl is None:
            logger.warning("Task %s ACL not found — denying read", task_id)
            return False
        allowed = (
            agent_id == acl.creator
            or agent_id == acl.assignee
            or agent_id in acl.guests
            or agent_id in acl.superiors
        )
        if not allowed:
            logger.warning("Read denied for agent '%s' on task %s", agent_id, task_id)
        return allowed

    def can_cancel_task(self, agent_id: str, task_id: str) -> bool:
        """Check whether *agent_id* can cancel *task_id*.

        Allowed if the agent is the **creator** or a **superior**.
        The assignee **cannot** cancel their own tasks by default —
        only the creator (or superior) has cancellation authority.

        Args:
            agent_id: Agent requesting cancellation.
            task_id: Task identifier.

        Returns:
            True if cancellation is permitted.
        """
        with self._lock:
            acl = self._acls.get(task_id)
        if acl is None:
            logger.warning("Task %s ACL not found — denying cancel", task_id)
            return False
        allowed = agent_id == acl.creator or agent_id in acl.superiors
        if not allowed:
            logger.warning("Cancel denied for agent '%s' on task %s (creator=%s)",
                           agent_id, task_id, acl.creator)
        return allowed

    # ── Explicit Grant / Revoke ─────────────────────────────────────

    def grant_task_access(self, task_id: str, agent_id: str) -> Dict[str, Any]:
        """Explicitly grant *agent_id* guest access to *task_id*.

        Args:
            task_id: Task identifier.
            agent_id: Agent to grant access to.

        Returns:
            Dict with ``task_id``, ``agent_id``, ``status``.
        """
        with self._lock:
            acl = self._acls.get(task_id)
            if acl is None:
                return {"task_id": task_id, "agent_id": agent_id,
                        "status": "error", "detail": "Task not found"}
            acl.guests.add(agent_id)
        logger.info("Task %s: guest access granted to '%s'", task_id, agent_id)
        return {"task_id": task_id, "agent_id": agent_id, "status": "granted"}

    def revoke_task_access(self, task_id: str, agent_id: str) -> Dict[str, Any]:
        """Revoke guest access for *agent_id* on *task_id*.

        Cannot revoke the creator or assignee.

        Args:
            task_id: Task identifier.
            agent_id: Agent to revoke access from.

        Returns:
            Dict with ``task_id``, ``agent_id``, ``status``.
        """
        with self._lock:
            acl = self._acls.get(task_id)
            if acl is None:
                return {"task_id": task_id, "agent_id": agent_id,
                        "status": "error", "detail": "Task not found"}
            if agent_id in (acl.creator, acl.assignee):
                return {"task_id": task_id, "agent_id": agent_id,
                        "status": "error", "detail": "Cannot revoke creator or assignee"}
            was_present = agent_id in acl.guests
            acl.guests.discard(agent_id)
        logger.info("Task %s: guest access revoked from '%s'", task_id, agent_id)
        return {"task_id": task_id, "agent_id": agent_id,
                "status": "revoked" if was_present else "not_found"}

    def get_task_acl(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get the ACL snapshot for *task_id*.

        Returns:
            Dict with all ACL fields, or None if not found.
        """
        with self._lock:
            acl = self._acls.get(task_id)
        if acl is None:
            return None
        return {
            "task_id": acl.task_id,
            "creator": acl.creator,
            "assignee": acl.assignee,
            "guests": sorted(acl.guests),
            "superiors": sorted(acl.superiors),
        }


# ── Module-Level Convenience Singletons ────────────────────────────────

_signer: Optional[AgentCardSigner] = None
_capability_auth: Optional[CapabilityAuth] = None
_task_permission: Optional[TaskPermission] = None


def get_signer() -> AgentCardSigner:
    global _signer
    if _signer is None:
        _signer = AgentCardSigner()
    return _signer


def get_capability_auth() -> CapabilityAuth:
    global _capability_auth
    if _capability_auth is None:
        _capability_auth = CapabilityAuth()
    return _capability_auth


def get_task_permission() -> TaskPermission:
    global _task_permission
    if _task_permission is None:
        _task_permission = TaskPermission()
    return _task_permission
