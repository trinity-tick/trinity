"""
Trinity A2A Protocol Enhancement Package — Google A2A v0.3 + MCP.

Agent-to-Agent communication hub enabling Trinity to serve as the
communication backbone for federated agent networks.

Key components:
  - AgentCard: self-describing capability manifest (A2A v0.3)
  - TaskManager: cross-agent task lifecycle (state machine)
  - CapabilityRegistry: global agent capability directory
  - A2AProtocol: JSON-RPC 2.0 messaging + capability negotiation
"""

from trinity.a2a.agent_card import (
    AgentCard,
    SkillDef,
    discover_capabilities,
    generate_card,
    sign_card,
    verify_card,
)
from trinity.a2a.capability_registry import CapabilityRegistry
from trinity.a2a.protocol import (
    A2AProtocol,
    A2ARequest,
    A2AResponse,
    NegotiationResult,
)
from trinity.a2a.task_manager import TaskManager, TaskState, A2ATask
from trinity.a2a.security import (
    AgentCardSigner,
    CapabilityAuth,
    TaskPermission,
)
from trinity.a2a.adapters import MarvisAdapter
from trinity.a2a.ed25519_signer import (
    Ed25519Signer,
    SigningAlgorithm,
    SigningBridge,
    x509Certificate,
    x509CertificateChain,
)

__all__ = [
    "AgentCard",
    "SkillDef",
    "discover_capabilities",
    "generate_card",
    "sign_card",
    "verify_card",
    "CapabilityRegistry",
    "A2AProtocol",
    "A2ARequest",
    "A2AResponse",
    "NegotiationResult",
    "TaskManager",
    "TaskState",
    "A2ATask",
    "AgentCardSigner",
    "CapabilityAuth",
    "TaskPermission",
    "MarvisAdapter",
    "Ed25519Signer",
    "SigningAlgorithm",
    "SigningBridge",
    "x509Certificate",
    "x509CertificateChain",
]

__version__ = "8.2.0"
