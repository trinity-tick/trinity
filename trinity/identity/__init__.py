"""
Trinity Identity Package
=========================
Multi-Anchor Identity architecture (arXiv 2604.09588).

Distributes agent identity across four anchor types:
- identity_files: Core personality, values, behavioral rules
- procedural_patterns: Decision templates, workflows
- episodic_keys: Key memory snapshots
- value_specifications: Value constraints, safety boundaries

Exports:
    IdentityManager: Core identity management engine.
    HybridRouter: Intelligent RAG+RLM query router.
    IdentityAnchor: Anchor data model.
    IdentityProfile: Reconstructed identity profile.
    IdentityBundle: Exportable identity package.
    ANCHOR_TYPES: Map of anchor type labels.
"""

from .identity_manager import IdentityManager, ANCHOR_TYPES, ANCHOR_WEIGHTS, DRIFT_SEVERITY
from .anchor_types import IdentityAnchor, IdentityProfile, IdentityBundle, TemporalAnchor
from .hybrid_router import HybridRouter, QueryType
from .rlm_router import RLMRouter, RouteResult

__all__ = [
    "IdentityManager",
    "HybridRouter",
    "QueryType",
    "RLMRouter",
    "RouteResult",
    "IdentityAnchor",
    "IdentityProfile",
    "IdentityBundle",
    "TemporalAnchor",
    "ANCHOR_TYPES",
    "ANCHOR_WEIGHTS",
    "DRIFT_SEVERITY",
]

__version__ = "8.7.0"
