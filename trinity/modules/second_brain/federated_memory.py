# -*- coding: utf-8 -*-
"""
# status: reserve (2026-09 EXECUTION 163)
Trinity Second Brain — Federated Memory Learning (P1-7).

Privacy-preserving cross-node memory aggregation using federated
averaging (FedAvg) with differential privacy. Enables distributed
Trinity instances to collaboratively improve memory models without
sharing raw data.

Key components:
  - FederatedMemoryModel: Local model with gradient computation.
  - PrivacyBudget: Epsilon-delta DP accounting.
  - FederatedAggregator: Weighted FedAvg with secure aggregation.

Usage::

    from trinity.modules.second_brain.federated_memory import (
        FederatedMemoryModel, FederatedAggregator, PrivacyBudget
    )

    model = FederatedMemoryModel(dim=128)
    gradients = model.compute_gradients(local_data)

    agg = FederatedAggregator(privacy_budget=PrivacyBudget(epsilon=1.0, delta=1e-5))
    global_model = agg.aggregate([
        (node_a_gradients, 100),
        (node_b_gradients, 200),
    ])
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── Privacy Budget ────────────────────────────────────────────────────────


@dataclass
class PrivacyBudget:
    """Differential privacy (epsilon, delta) budget tracker.

    Tracks privacy consumption across federation rounds to ensure
    the total privacy budget is not exceeded.

    Attributes:
        epsilon: Privacy loss parameter (lower = more private, typical 0.1-10).
        delta: Failure probability (typical 1e-5 to 1e-7).
        total_epsilon: Accumulated epsilon across rounds.
        total_delta: Accumulated delta across rounds.
    """

    epsilon: float = 1.0
    delta: float = 1e-5
    total_epsilon: float = 0.0
    total_delta: float = 0.0

    def consume(self, round_epsilon: float, round_delta: float = 0.0) -> bool:
        """Attempt to consume privacy budget for a round.

        Args:
            round_epsilon: Epsilon cost for this round.
            round_delta: Delta cost for this round.

        Returns:
            True if budget available, False if exceeded.
        """
        if self.total_epsilon + round_epsilon > self.epsilon:
            logger.warning("Privacy budget exceeded: total_epsilon=%.4f + %.4f > %.4f",
                          self.total_epsilon, round_epsilon, self.epsilon)
            return False
        if self.total_delta + round_delta > self.delta:
            logger.warning("Delta budget exceeded")
            return False

        self.total_epsilon += round_epsilon
        self.total_delta += round_delta
        return True

    def reset(self) -> None:
        """Reset accumulated consumption."""
        self.total_epsilon = 0.0
        self.total_delta = 0.0

    @property
    def remaining(self) -> Tuple[float, float]:
        """Return remaining (epsilon, delta)."""
        return (
            max(0.0, self.epsilon - self.total_epsilon),
            max(0.0, self.delta - self.total_delta),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "epsilon_budget": self.epsilon,
            "delta_budget": self.delta,
            "consumed_epsilon": round(self.total_epsilon, 6),
            "consumed_delta": round(self.total_delta, 10),
            "remaining_epsilon": round(self.remaining[0], 6),
            "remaining_delta": round(self.remaining[1], 10),
        }


# ── Federated Memory Model ────────────────────────────────────────────────


@dataclass
class FederatedMemoryModel:
    """Local memory model for federated learning.

    Represents a local node's memory parameters as a vector that can be
    shared (after DP noise) and aggregated across nodes.

    Attributes:
        dim: Model dimension (embedding size).
        weights: Current parameter vector.
        node_id: Unique node identifier.
        sample_count: Number of local samples used for training.
    """

    dim: int = 128
    weights: np.ndarray = field(default_factory=lambda: np.zeros(128, dtype=np.float32))
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sample_count: int = 0
    version: int = 0

    def __post_init__(self):
        if len(self.weights) != self.dim:
            self.weights = np.zeros(self.dim, dtype=np.float32)
            # Initialize with small random values
            rng = np.random.RandomState(int(hashlib.md5(self.node_id.encode()).hexdigest()[:8], 16))
            self.weights = rng.randn(self.dim).astype(np.float32) * 0.01

    # ── Gradient Computation ─────────────────────────────────────────

    def compute_gradients(
        self,
        local_data: List[Dict[str, Any]],
        learning_rate: float = 0.01,
    ) -> np.ndarray:
        """Compute local gradients from training data.

        Simulates gradient computation: for each sample, computes a
        dummy gradient vector based on content hash.

        Args:
            local_data: List of memory items with 'content' key.
            learning_rate: SGD learning rate.

        Returns:
            Gradient vector (dim,).
        """
        if not local_data:
            return np.zeros(self.dim, dtype=np.float32)

        gradients = np.zeros(self.dim, dtype=np.float32)
        for item in local_data:
            content = item.get("content", "")
            # Deterministic gradient from content
            seed = int(hashlib.md5(content.encode()).hexdigest()[:8], 16) % (2 ** 31)
            rng = np.random.RandomState(seed)
            grad = rng.randn(self.dim).astype(np.float32) * learning_rate
            gradients += grad

        gradients /= max(len(local_data), 1)
        self.sample_count = len(local_data)
        return gradients

    def apply_gradients(self, gradients: np.ndarray) -> None:
        """Apply gradient update to local weights.

        Args:
            gradients: Gradient vector to apply.
        """
        if len(gradients) != self.dim:
            raise ValueError(f"Gradient dim mismatch: {len(gradients)} != {self.dim}")
        self.weights -= gradients
        self.version += 1

    def set_weights(self, new_weights: np.ndarray) -> None:
        """Replace local weights (e.g., after global aggregation).

        Args:
            new_weights: New weight vector.
        """
        if len(new_weights) != self.dim:
            raise ValueError(f"Weight dim mismatch: {len(new_weights)} != {self.dim}")
        self.weights = new_weights.copy()
        self.version += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "dim": self.dim,
            "sample_count": self.sample_count,
            "version": self.version,
            "weights_norm": float(np.linalg.norm(self.weights)),
        }


# ── Differential Privacy ──────────────────────────────────────────────────


def add_gaussian_noise(
    gradients: np.ndarray,
    sensitivity: float = 1.0,
    epsilon: float = 1.0,
    delta: float = 1e-5,
) -> np.ndarray:
    """Add Gaussian noise for (epsilon, delta)-differential privacy.

    Uses the Gaussian mechanism: noise ~ N(0, sigma^2) where
    sigma = sensitivity * sqrt(2 * ln(1.25/delta)) / epsilon.

    Args:
        gradients: Raw gradient vector.
        sensitivity: L2 sensitivity of the gradient computation.
        epsilon: Privacy budget epsilon.
        delta: Privacy budget delta.

    Returns:
        Noisy gradient vector with DP guarantee.
    """
    if epsilon <= 0:
        return gradients.copy()

    sigma = sensitivity * math.sqrt(2 * math.log(1.25 / delta)) / epsilon
    noise = np.random.normal(0, sigma, size=gradients.shape).astype(np.float32)
    return gradients + noise


def clip_gradients(gradients: np.ndarray, max_norm: float = 1.0) -> np.ndarray:
    """Clip gradient L2 norm to bound sensitivity.

    Args:
        gradients: Raw gradient vector.
        max_norm: Maximum allowed L2 norm.

    Returns:
        Clipped gradient vector.
    """
    norm = np.linalg.norm(gradients)
    if norm > max_norm:
        return gradients * (max_norm / norm)
    return gradients.copy()


# ── Federated Aggregator ──────────────────────────────────────────────────


class FederatedAggregator:
    """Federated averaging (FedAvg) with differential privacy.

    Collects gradient updates from multiple nodes, applies weighted
    averaging, and produces a global model update. Supports secure
    aggregation via DP noise injection.

    Usage::

        agg = FederatedAggregator(privacy_budget=PrivacyBudget(epsilon=2.0))
        global_weights = agg.aggregate([
            (node_a_gradients, 100),   # (gradients, sample_count)
            (node_b_gradients, 200),
        ])
    """

    def __init__(
        self,
        privacy_budget: Optional[PrivacyBudget] = None,
        clip_norm: float = 1.0,
        round_id: Optional[str] = None,
    ):
        self._privacy = privacy_budget or PrivacyBudget()
        self._clip_norm = clip_norm
        self._round_id = round_id or uuid.uuid4().hex[:8]
        self._lock = threading.RLock()
        self._history: List[Dict[str, Any]] = []
        self._round_count: int = 0

    # ── Aggregation ─────────────────────────────────────────────────

    def aggregate(
        self,
        node_updates: List[Tuple[np.ndarray, int]],
        round_epsilon: Optional[float] = None,
    ) -> np.ndarray:
        """Perform weighted federated averaging.

        Steps:
          1. Check privacy budget.
          2. Clip each node's gradients.
          3. Compute weighted average (by sample count).
          4. Add DP noise to the aggregate.

        Args:
            node_updates: List of (gradient_vector, sample_count) tuples.
            round_epsilon: Epsilon for this round (default: privacy.epsilon).

        Returns:
            Aggregated global gradient vector.
        """
        eps = round_epsilon if round_epsilon is not None else self._privacy.epsilon

        if not self._privacy.consume(eps):
            logger.error("Privacy budget exhausted — aggregation denied")
            raise RuntimeError("Privacy budget exhausted")

        if not node_updates:
            return np.zeros(self._privacy.epsilon, dtype=np.float32)[:1]  # No-op

        dim = len(node_updates[0][0])
        total_samples = sum(count for _, count in node_updates)

        # Weighted FedAvg
        aggregated = np.zeros(dim, dtype=np.float32)
        for gradients, sample_count in node_updates:
            clipped = clip_gradients(gradients, self._clip_norm)
            weight = sample_count / max(total_samples, 1)
            aggregated += clipped * weight

        # DP noise injection
        noisy = add_gaussian_noise(
            aggregated,
            sensitivity=self._clip_norm,
            epsilon=eps,
            delta=self._privacy.delta,
        )

        # Record history
        with self._lock:
            self._round_count += 1
            self._history.append({
                "round": self._round_count,
                "round_id": self._round_id,
                "nodes": len(node_updates),
                "total_samples": total_samples,
                "epsilon_consumed": eps,
                "gradient_norm": float(np.linalg.norm(noisy)),
                "timestamp": time.time(),
            })

        logger.info(
            "FedAvg round %d: %d nodes, %d samples, epsilon=%.4f, "
            "norm=%.4f (noise sigma applied)",
            self._round_count, len(node_updates), total_samples,
            eps, np.linalg.norm(noisy)
        )

        return noisy

    def secure_aggregate(
        self,
        node_updates: List[Tuple[np.ndarray, int, bytes]],
        round_epsilon: Optional[float] = None,
    ) -> np.ndarray:
        """Secure aggregation with integrity verification.

        Each node provides a hash commitment alongside its gradient.
        The aggregator verifies integrity before averaging.

        Args:
            node_updates: List of (gradients, sample_count, commitment_hash).
            round_epsilon: Epsilon for this round.

        Returns:
            Aggregated gradient vector.
        """
        verified_updates = []
        for gradients, count, commitment in node_updates:
            expected = hashlib.sha256(gradients.tobytes()).digest()
            if expected == commitment:
                verified_updates.append((gradients, count))
            else:
                logger.warning("Commitment mismatch for node — update rejected")

        if not verified_updates:
            raise ValueError("No verified updates after integrity check")

        return self.aggregate(verified_updates, round_epsilon=round_epsilon)

    # ── History & Stats ──────────────────────────────────────────────

    @property
    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "round_id": self._round_id,
                "round_count": self._round_count,
                "privacy": self._privacy.to_dict(),
                "clip_norm": self._clip_norm,
                "total_nodes": sum(h["nodes"] for h in self._history),
                "total_samples": sum(h["total_samples"] for h in self._history),
            }


# ── Federation Orchestrator ───────────────────────────────────────────────


class FederationOrchestrator:
    """Orchestrates federated learning rounds across nodes.

    Manages the lifecycle: local training → secure aggregation →
    global model update → distribution.

    Usage::

        orch = FederationOrchestrator(dim=128)
        orch.register_node("node-a", sample_count=1000)
        orch.register_node("node-b", sample_count=500)
        result = orch.run_round()
    """

    def __init__(self, dim: int = 128, epsilon: float = 2.0, delta: float = 1e-5):
        self._dim = dim
        self._aggregator = FederatedAggregator(
            privacy_budget=PrivacyBudget(epsilon=epsilon, delta=delta),
        )
        self._global_model = FederatedMemoryModel(dim=dim, node_id="global")
        self._nodes: Dict[str, FederatedMemoryModel] = {}
        self._node_samples: Dict[str, int] = {}
        self._lock = threading.RLock()

    def register_node(self, node_id: str, sample_count: int = 0) -> FederatedMemoryModel:
        """Register a node for federation.

        Args:
            node_id: Unique node identifier.
            sample_count: Number of local samples.

        Returns:
            The node's local model.
        """
        with self._lock:
            model = FederatedMemoryModel(dim=self._dim, node_id=node_id)
            self._nodes[node_id] = model
            self._node_samples[node_id] = sample_count
            return model

    def unregister_node(self, node_id: str) -> bool:
        with self._lock:
            return self._nodes.pop(node_id, None) is not None

    def run_round(
        self,
        local_data: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        round_epsilon: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute one federation round.

        Each registered node:
          1. Trains on local data → gradients
          2. Clips + DP noise → private gradients
          3. Submits to aggregator

        Aggregator:
          a. Weighted FedAvg
          b. DP noise on aggregate
          c. Updates global model

        Args:
            local_data: Dict[node_id → list of memory items].
            round_epsilon: Epsilon for this round.

        Returns:
            Round result dict.
        """
        with self._lock:
            updates = []
            for node_id, model in self._nodes.items():
                data = (local_data or {}).get(node_id, [])
                gradients = model.compute_gradients(data)

                # DP: clip + noise
                clipped = clip_gradients(gradients)
                noisy = add_gaussian_noise(
                    clipped,
                    epsilon=round_epsilon or self._aggregator._privacy.epsilon,
                    delta=self._aggregator._privacy.delta,
                )

                sample_count = len(data) or self._node_samples.get(node_id, 1)
                updates.append((noisy, sample_count))

            # Global aggregation
            global_grad = self._aggregator.aggregate(updates, round_epsilon=round_epsilon)
            self._global_model.apply_gradients(global_grad)

            # Distribute global weights to nodes
            for model in self._nodes.values():
                model.set_weights(self._global_model.weights)

            return {
                "round": self._aggregator._round_count,
                "nodes": len(updates),
                "global_version": self._global_model.version,
                "privacy": self._aggregator._privacy.to_dict(),
                "gradient_norm": float(np.linalg.norm(global_grad)),
            }

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "nodes_registered": len(self._nodes),
                "aggregator": self._aggregator.stats,
                "global_model": self._global_model.to_dict(),
            }


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module self-test."""
    results: Dict[str, Any] = {"module": "trinity.modules.second_brain.federated_memory", "tests": {}}

    # Test 1: PrivacyBudget
    try:
        budget = PrivacyBudget(epsilon=2.0, delta=1e-5)
        assert budget.consume(0.5)
        assert budget.total_epsilon == 0.5
        assert budget.remaining == (1.5, 1e-5)
        assert not budget.consume(2.0)  # Would exceed
        results["tests"]["privacy_budget"] = "PASS"
    except Exception as e:
        results["tests"]["privacy_budget"] = f"FAIL: {e}"

    # Test 2: FederatedMemoryModel gradient computation
    try:
        model = FederatedMemoryModel(dim=128)
        assert model.dim == 128
        assert len(model.weights) == 128

        data = [
            {"content": "memory item 1"},
            {"content": "memory item 2"},
            {"content": "memory item 3"},
        ]
        gradients = model.compute_gradients(data)
        assert gradients.shape == (128,)
        assert model.sample_count == 3
        assert model.version == 0

        model.apply_gradients(gradients)
        assert model.version == 1
        results["tests"]["model_gradients"] = "PASS"
    except Exception as e:
        results["tests"]["model_gradients"] = f"FAIL: {e}"

    # Test 3: DP noise functions
    try:
        raw = np.ones(128, dtype=np.float32)
        noisy = add_gaussian_noise(raw, epsilon=1.0, delta=1e-5)
        assert noisy.shape == raw.shape
        assert not np.allclose(noisy, raw)  # Should differ

        clipped = clip_gradients(np.ones(128, dtype=np.float32) * 10.0, max_norm=1.0)
        assert np.linalg.norm(clipped) <= 1.01
        results["tests"]["dp_noise"] = "PASS"
    except Exception as e:
        results["tests"]["dp_noise"] = f"FAIL: {e}"

    # Test 4: FederatedAggregator basic
    try:
        agg = FederatedAggregator(privacy_budget=PrivacyBudget(epsilon=2.0))
        node_a = np.random.randn(128).astype(np.float32) * 0.1
        node_b = np.random.randn(128).astype(np.float32) * 0.1

        result = agg.aggregate([
            (node_a, 100),
            (node_b, 200),
        ])
        assert result.shape == (128,)
        assert len(agg.history) == 1
        results["tests"]["aggregator_basic"] = "PASS"
    except Exception as e:
        results["tests"]["aggregator_basic"] = f"FAIL: {e}"

    # Test 5: Secure aggregation with commitments
    try:
        agg2 = FederatedAggregator(privacy_budget=PrivacyBudget(epsilon=3.0))
        g = np.ones(64, dtype=np.float32) * 0.5
        commitment = hashlib.sha256(g.tobytes()).digest()

        result = agg2.secure_aggregate([(g, 50, commitment)])
        assert result.shape == (64,)
        results["tests"]["secure_aggregate"] = "PASS"
    except Exception as e:
        results["tests"]["secure_aggregate"] = f"FAIL: {e}"

    # Test 6: Bad commitment rejection
    try:
        agg3 = FederatedAggregator(privacy_budget=PrivacyBudget(epsilon=3.0))
        g = np.ones(64, dtype=np.float32) * 0.5
        bad_hash = b"\x00" * 32

        try:
            agg3.secure_aggregate([(g, 50, bad_hash)])
            results["tests"]["bad_commitment"] = "FAIL: should have rejected"
        except ValueError:
            results["tests"]["bad_commitment"] = "PASS"
    except Exception as e:
        results["tests"]["bad_commitment"] = f"FAIL: {e}"

    # Test 7: FederationOrchestrator round
    try:
        orch = FederationOrchestrator(dim=64, epsilon=2.0)
        orch.register_node("node-a", sample_count=100)
        orch.register_node("node-b", sample_count=200)

        local_data = {
            "node-a": [{"content": f"a-{i}"} for i in range(10)],
            "node-b": [{"content": f"b-{i}"} for i in range(20)],
        }

        result = orch.run_round(local_data)
        assert result["nodes"] == 2
        assert result["global_version"] >= 1
        results["tests"]["orchestrator_round"] = "PASS"
    except Exception as e:
        results["tests"]["orchestrator_round"] = f"FAIL: {e}"

    # Test 8: Privacy budget exhaustion
    try:
        budget2 = PrivacyBudget(epsilon=0.1, delta=1e-5)
        budget2.consume(0.05)
        budget2.consume(0.05)
        assert not budget2.consume(0.01)  # Already at 0.1
        budget2.reset()
        assert budget2.total_epsilon == 0.0
        results["tests"]["budget_exhaustion"] = "PASS"
    except Exception as e:
        results["tests"]["budget_exhaustion"] = f"FAIL: {e}"

    passed = sum(1 for v in results["tests"].values() if "PASS" in str(v))
    total = len(results["tests"])
    results["summary"] = f"{passed}/{total} PASS"
    return results


if __name__ == "__main__":
    import sys
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if all("PASS" in str(v) for v in result["tests"].values()) else 1)
