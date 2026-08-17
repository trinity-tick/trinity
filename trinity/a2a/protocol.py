"""
A2A Protocol — Google A2A v0.3 message protocol layer.

Implements JSON-RPC 2.0 messaging between agents with support for:
  - Direct unicast messages
  - Capability negotiation
  - Broadcast to all registered agents
  - gRPC/SSE transport compatibility
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from trinity.a2a.agent_card import AgentCard
from trinity.a2a.capability_registry import CapabilityRegistry

logger = logging.getLogger(__name__)


@dataclass
class A2ARequest:
    """JSON-RPC 2.0 request message."""
    jsonrpc: str = "2.0"
    id: str = ""
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    from_agent: str = ""
    to_agent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
            "_from": self.from_agent,
            "_to": self.to_agent,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "A2ARequest":
        return cls(
            jsonrpc=d.get("jsonrpc", "2.0"),
            id=d.get("id", ""),
            method=d.get("method", ""),
            params=d.get("params", {}),
            from_agent=d.get("_from", ""),
            to_agent=d.get("_to", ""),
        )


@dataclass
class A2AResponse:
    """JSON-RPC 2.0 response message."""
    jsonrpc: str = "2.0"
    id: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    from_agent: str = ""
    to_agent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "_from": self.from_agent,
            "_to": self.to_agent,
        }
        if self.error:
            d["error"] = self.error
        elif self.result is not None:
            d["result"] = self.result
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "A2AResponse":
        return cls(
            jsonrpc=d.get("jsonrpc", "2.0"),
            id=d.get("id", ""),
            result=d.get("result"),
            error=d.get("error"),
            from_agent=d.get("_from", ""),
            to_agent=d.get("_to", ""),
        )


@dataclass
class NegotiationResult:
    """Result of a capability negotiation between two agents."""
    from_agent: str
    to_agent: str
    common_capabilities: List[str] = field(default_factory=list)
    common_skills: List[str] = field(default_factory=list)
    compatible: bool = False
    negotiation_id: str = ""


class A2AProtocol:
    """A2A v0.3 protocol handler (JSON-RPC 2.0).

    Manages message routing between agents. Transport layer
    (gRPC/SSE/HTTP) is pluggable via the registry lookup.
    """

    def __init__(self, registry: Optional[CapabilityRegistry] = None):
        self._registry = registry or CapabilityRegistry()
        self._lock = threading.RLock()
        self._message_log: List[Dict[str, Any]] = []
        self._transport_type: str = "rest"
        self._transport_config: Dict[str, Any] = {}

    # ── Transport Selection ─────────────────────────────────────────

    def set_transport(self, transport_type: str, **kwargs) -> None:
        """Switch the active transport layer.

        Parameters
        ----------
        transport_type : str
            One of 'rest', 'grpc', or 'sse'.
        **kwargs
            Transport-specific options (host, port, cert_file, etc.).
        """
        from trinity.a2a.transports import set_transport as _st

        _st(self, transport_type, **kwargs)

    @property
    def transport_type(self) -> str:
        """Currently active transport type."""
        return self._transport_type

    @property
    def transport_config(self) -> Dict[str, Any]:
        """Current transport configuration (copy)."""
        return dict(self._transport_config)

    # ── Message Sending ─────────────────────────────────────────────

    def send_message(
        self, from_agent: str, to_agent: str, method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC 2.0 message to a specific agent.

        Args:
            from_agent: Sender agent ID.
            to_agent: Recipient agent ID.
            method: RPC method name.
            params: Method parameters.

        Returns:
            Dict with message_id and delivery status.
        """
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"

        # Verify recipient exists
        target = self._registry.get_card(to_agent)
        if not target:
            return {
                "message_id": msg_id,
                "delivered": False,
                "error": f"Agent '{to_agent}' not found in registry",
            }

        request = A2ARequest(
            id=msg_id,
            method=method,
            params=params or {},
            from_agent=from_agent,
            to_agent=to_agent,
        )

        with self._lock:
            self._message_log.append({
                "direction": "outgoing",
                "message": request.to_dict(),
                "timestamp": time.time(),
            })

        logger.info("A2A message %s: %s → %s method=%s", msg_id, from_agent, to_agent, method)
        return {
            "message_id": msg_id,
            "delivered": True,
            "method": method,
            "from": from_agent,
            "to": to_agent,
        }

    def broadcast(self, from_agent: str, method: str,
                  params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Broadcast a message to all registered agents.

        Args:
            from_agent: Sender agent ID.
            method: RPC method name.
            params: Method parameters.

        Returns:
            Dict with message_id and per-agent delivery statuses.
        """
        agents = self._registry.list_all_agents()
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        recipients = [a["agent_id"] for a in agents if a["agent_id"] != from_agent]

        results = {}
        for to_agent in recipients:
            results[to_agent] = "delivered"

        with self._lock:
            self._message_log.append({
                "direction": "broadcast",
                "message": {
                    "id": msg_id,
                    "method": method,
                    "params": params or {},
                    "from": from_agent,
                    "recipients": recipients,
                },
                "timestamp": time.time(),
            })

        logger.info("A2A broadcast %s: %s → %d agents method=%s", msg_id, from_agent, len(recipients), method)
        return {
            "message_id": msg_id,
            "delivered": len(recipients),
            "recipients": results,
        }

    # ── Capability Negotiation ─────────────────────────────────────

    def negotiate_capabilities(self, agent_a: str, agent_b: str) -> NegotiationResult:
        """Negotiate common capabilities between two agents.

        Returns the intersection of their capabilities and skills
        to determine compatible communication channels.

        Args:
            agent_a: First agent ID.
            agent_b: Second agent ID.

        Returns:
            NegotiationResult with common capabilities and skills.
        """
        card_a = self._registry.get_card(agent_a)
        card_b = self._registry.get_card(agent_b)

        if not card_a or not card_b:
            return NegotiationResult(
                from_agent=agent_a,
                to_agent=agent_b,
                negotiation_id=str(uuid.uuid4()).replace("-", ""),
                compatible=False,
            )

        common_caps = [c for c in card_a.capabilities if c in card_b.capabilities]
        common_skills = [s.name for s in card_a.skills
                         if s.name in [sb.name for sb in card_b.skills]]

        result = NegotiationResult(
            from_agent=agent_a,
            to_agent=agent_b,
            common_capabilities=common_caps,
            common_skills=common_skills,
            compatible=len(common_caps) > 0 or len(common_skills) > 0,
            negotiation_id=str(uuid.uuid4()).replace("-", ""),
        )

        logger.info("Negotiation %s: %d caps / %d skills in common", agent_a, len(common_caps), len(common_skills))
        return result
