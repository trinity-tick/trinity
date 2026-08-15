"""
# status: experimental (2026-08-15 audit: lazy-loader for SecondBrain; pairs with
#   registry.py, not wired into runtime path - engine facade is the active path)
Optimized SecondBrain loader with lazy module loading.
Replaces the monolithic SecondBrainV636 constructor.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from trinity.modules.second_brain.registry import get_registry, ModuleRegistry
from trinity.modules.second_brain.guardian import GuardianChainV50
from trinity.modules.second_brain.guardian_retrieval import RetrievalSystemV47


class SecondBrainLoader:
    """Optimized loader for SecondBrain with selective lazy loading.

    Instead of instantiating all 122 modules at once, this loader only
    creates the ones needed for the current operation and defers the rest.
    """

    def __init__(self, lazy: bool = True):
        self._lazy = lazy
        self._registry = get_registry()
        self._guardian = None
        self._retrieval = None
        self._version = "v6.36"
        self._start_time = time.time()
        self._init_time_ms = 0.0

        t0 = time.time()

        # Always initialize lightweight systems
        self._guardian = GuardianChainV50()
        self._retrieval = RetrievalSystemV47()

        if not lazy:
            # Eager load all modules (original behavior)
            self._load_all()
        else:
            # Lazy: only load core path, rest deferred
            self._register_all()

        self._init_time_ms = (time.time() - t0) * 1000

    def _register_all(self):
        """Register all modules for lazy loading without instantiating."""
        r = self._registry

        # Core modules - register for lazy loading
        module_defs = [
            ("M101", "trinity.modules.second_brain.engine", "HippocampalComplementaryMemory",
             {"cache_capacity": 256, "beta": 0.5, "gamma_threshold": 0.85}),
            ("M102", "trinity.modules.second_brain.engine", "IdentityPreservingConsolidator",
             {"episodic_threshold": 10}),
            ("M103", "trinity.modules.second_brain.engine", "ReasoningDriftAuditor",
             {"drift_threshold": 0.15, "alert_threshold": 0.25}),
            ("M104", "trinity.modules.second_brain.engine", "ContextObjectManager",
             {"max_objects": 512}),
        ]
        for name, path, cls, kwargs in module_defs:
            r.register(name, path, cls, **kwargs)

        # CB modules - register for lazy loading
        cb_defs = [
            ("CB45", "trinity.modules.second_brain.engine", "ProgressiveCascade",
             {"l1_cache_size": 64, "recency_decay_lambda": 0.01}),
            ("CB46", "trinity.modules.second_brain.engine", "TemporalValidity", {}),
            ("CB47", "trinity.modules.second_brain.engine", "TokenEfficientMemory",
             {"total_budget": 7000, "reserved_for_response": 500}),
            ("CB48", "trinity.modules.second_brain.engine", "AgentNativeCuration",
             {"checkpoint_interval": 10}),
            ("CB49", "trinity.modules.second_brain.engine", "RelationalVersioning",
             {"semantic_similarity_threshold": 0.85}),
            ("CB50", "trinity.modules.second_brain.engine", "ContextualChunkIngestion", {}),
            ("CB51", "trinity.modules.second_brain.engine", "ObserverReflector",
             {"observer_token_threshold": 800, "reflector_token_threshold": 3000}),
            ("CB52", "trinity.modules.second_brain.engine", "GroundTruthEpisodes",
             {"short_term_size": 20, "context_window_extension": 5, "retrieval_depth": 3}),
        ]
        for name, path, cls, kwargs in cb_defs:
            r.register(name, path, cls, **kwargs)

    def _load_all(self):
        """Eagerly load all modules (original SecondBrainV636 behavior)."""
        from trinity.modules.second_brain.engine import SecondBrainV636
        sb = SecondBrainV636()
        # Access all attributes to force loading
        self._guardian = sb.guardian_chain
        self._retrieval = sb.retrieval

    @property
    def guardian_chain(self):
        return self._guardian

    @property
    def retrieval(self):
        return self._retrieval

    def __getattr__(self, name):
        # Try lazy registry
        if name in self._registry:
            return self._registry[name]
        raise AttributeError(f"SecondBrain has no module: {name}")

    def get_module(self, name: str):
        """Get a specific module by name, loading lazily if needed."""
        return self._registry.get(name)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "version": self._version,
            "lazy": self._lazy,
            "init_time_ms": self._init_time_ms,
            "guardian_levels": self._guardian.total if self._guardian else 0,
            "retrieval_channels": self._retrieval.total if self._retrieval else 0,
            "registry": self._registry.diagnostics() if self._lazy else {"eager": True},
        }
