"""
A2A Agent Card — Google A2A v0.3 Agent Card data structure.

The Agent Card is the self-describing manifest that each agent exposes
for capability discovery in the agent federated network. Cards carry
a SHA-256 signature for tamper resistance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Symmetric key for card signing (auto-generated on first use)
_CARD_SIGNING_KEY: Optional[bytes] = None


def _get_or_create_key() -> bytes:
    global _CARD_SIGNING_KEY
    if _CARD_SIGNING_KEY is None:
        _CARD_SIGNING_KEY = hashlib.sha256(str(time.time_ns()).encode()).digest()
    return _CARD_SIGNING_KEY


@dataclass
class SkillDef:
    """Definition of a single skill exposed by an agent."""
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30000
    requires_human: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillDef":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentCard:
    """Google A2A v0.3 Agent Card.

    Self-describing manifest for agent capability discovery.
    Supports TTL-based expiration for dynamic agent networks.
    """
    agent_id: str
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    capabilities: List[str] = field(default_factory=list)
    endpoints: Dict[str, str] = field(default_factory=dict)
    skills: List[SkillDef] = field(default_factory=list)
    input_modes: List[str] = field(default_factory=list)
    output_modes: List[str] = field(default_factory=list)
    security_level: str = "standard"   # standard / elevated / restricted
    signed_card: str = ""              # SHA-256 HMAC signature
    url: str = ""                      # optional discovery URL

    # ── TTL / Expiry (A2A v0.3 extension) ──────────────────────────
    registered_at: str = ""            # ISO 8601 registration timestamp
    ttl_seconds: int = 86400           # Time-to-live in seconds (default 24h)

    def is_expired(self) -> bool:
        """Check if the agent card has exceeded its TTL.

        Returns True if the card has been registered longer than ttl_seconds.
        Cards without a registered_at timestamp are considered not expired.
        """
        if not self.registered_at:
            return False
        try:
            from datetime import datetime, timezone, timedelta
            reg_time = datetime.fromisoformat(self.registered_at.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - reg_time).total_seconds()
            return elapsed > self.ttl_seconds
        except (ValueError, AttributeError):
            return False

    def refresh(self) -> None:
        """Reset the registration timestamp to now, extending the TTL."""
        from datetime import datetime, timezone
        self.registered_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self, include_signature: bool = True) -> Dict[str, Any]:
        """Serialize to dict. Excludes signature when signing."""
        d = {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.capabilities,
            "endpoints": self.endpoints,
            "skills": [s.to_dict() for s in self.skills],
            "input_modes": self.input_modes,
            "output_modes": self.output_modes,
            "security_level": self.security_level,
            "url": self.url,
            "registered_at": self.registered_at,
            "ttl_seconds": self.ttl_seconds,
        }
        if include_signature and self.signed_card:
            d["signed_card"] = self.signed_card
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentCard":
        skills_data = d.get("skills", [])
        skills = [SkillDef.from_dict(s) if isinstance(s, dict) else s for s in skills_data]
        return cls(
            agent_id=d.get("agent_id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            version=d.get("version", "1.0.0"),
            capabilities=d.get("capabilities", []),
            endpoints=d.get("endpoints", {}),
            skills=skills,
            input_modes=d.get("input_modes", []),
            output_modes=d.get("output_modes", []),
            security_level=d.get("security_level", "standard"),
            signed_card=d.get("signed_card", ""),
            url=d.get("url", ""),
            registered_at=d.get("registered_at", ""),
            ttl_seconds=d.get("ttl_seconds", 86400),
        )


def generate_card(
    agent_id: str,
    name: str = "",
    capabilities: Optional[List[str]] = None,
    skills: Optional[List[SkillDef]] = None,
    url: str = "",
    ttl_seconds: int = 86400,
) -> AgentCard:
    """Generate an Agent Card from known agent information.

    Args:
        agent_id: Unique agent identifier.
        name: Human-readable agent name.
        capabilities: List of capability strings.
        skills: List of SkillDef objects.
        url: Agent's canonical discovery URL.
        ttl_seconds: Time-to-live in seconds (default 24h).

    Returns:
        A new AgentCard with signature and registration timestamp.
    """
    from datetime import datetime, timezone as tz

    card = AgentCard(
        agent_id=agent_id,
        name=name or agent_id,
        description=f"Agent {agent_id}",
        version="1.0.0",
        capabilities=capabilities or [],
        skills=skills or [],
        input_modes=["json", "text"],
        output_modes=["json", "text"],
        endpoints={
            "a2a": f"{url}/a2a" if url else "",
            "health": f"{url}/health" if url else "",
        },
        url=url,
        registered_at=datetime.now(tz.utc).isoformat(),
        ttl_seconds=ttl_seconds,
    )
    return sign_card(card)


def sign_card(card: AgentCard) -> AgentCard:
    """Sign an Agent Card with HMAC-SHA256 for tamper resistance.

    Returns the card with signed_card field populated.
    """
    key = _get_or_create_key()
    payload = json.dumps(card.to_dict(include_signature=False), sort_keys=True).encode("utf-8")
    card.signed_card = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return card


def verify_card(card: AgentCard) -> Dict[str, Any]:
    """Verify an Agent Card's HMAC signature.

    Returns:
        Dict with 'valid' (bool) and 'detail' (str).
    """
    if not card.signed_card:
        return {"valid": False, "detail": "No signature present"}
    key = _get_or_create_key()
    payload = json.dumps(card.to_dict(include_signature=False), sort_keys=True).encode("utf-8")
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if hmac.compare_digest(card.signed_card, expected):
        return {"valid": True, "detail": "Signature verified"}
    return {"valid": False, "detail": "Signature mismatch — card may be tampered"}


def discover_capabilities(agent_id: str, registry=None) -> List[str]:
    """Query an agent's capability list from the registry.

    Args:
        agent_id: Target agent identifier.
        registry: Optional CapabilityRegistry instance.

    Returns:
        List of capability strings. Empty if agent not found.
    """
    if registry is None:
        return []
    card = registry.get_card(agent_id)
    if card is None:
        logger.warning("Agent '%s' not found in registry", agent_id)
        return []
    return card.capabilities
