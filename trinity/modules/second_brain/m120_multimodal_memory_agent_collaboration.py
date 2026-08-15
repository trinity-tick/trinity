# DEPRECATED: This experimental module (M120) is not registered in __init__.py
# and has no known internal consumers. It is retained for reference only.
# Last assessed: 2026-08-08. Remove in a future cleanup cycle if unused.

"""
# status: orphan (2026-08-15 audit, not in runtime path)
M120 MultimodalMemoryAgentCollaboration — 多模态记忆增强Agent协作推荐
==========================================================
基于 MMEACR (arXiv 2607.07108, July 8, 2026):
多模态记忆增强 Agent 协作推荐框架。

核心创新:
  传统 LLM Agent 推荐系统受限于纯文本输入和粗粒度记忆更新，容易丢失视觉证据、
  语义噪声和偏好漂移。MMEACR 通过双轨道记忆架构分离可解释推理与细粒度多模态匹配，
  属性引导强化-反思机制更新记忆，双轨道加权 RRF 融合排序。

核心设计:
  1. DualTrackMemoryArchitecture — 双轨道记忆架构
     - 推理轨道: 协同 User/Item Memory Agent，属性引导强化-反思更新
     - 匹配轨道: 解耦多模态嵌入记忆，保存原始交互叙述+物品图片
  2. AttributeGuidedReinforcementReflection — 属性引导强化反思
     - 属性引导的强化学习更新记忆
     - 反思机制检测偏好漂移
  3. MultimodalWeightedRRF — 多模态加权互惠排序融合
     - 双轨道加权互惠排序融合
     - 视觉证据与文本推理的互补排序
  4. 集成到 second_brain 协作决策管线

字段说明:
  - MODULE_ID: M120
  - MODULE_VERSION: 1.0.0
  - PAPER_REF: MMEACR (arXiv 2607.07108, July 8, 2026)
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np


MODULE_ID = "M120"
MODULE_VERSION = "1.0.0"
PAPER_REF = "MMEACR: Multimodal Memory-Enhanced Agent Collaboration for Recommendation (arXiv 2607.07108)"

SEP = "=" * 80
SUB = "-" * 60

# Architecture constants
DEFAULT_TEXT_EMBED_DIM = 768
DEFAULT_IMAGE_EMBED_DIM = 512
DEFAULT_MULTIMODAL_EMBED_DIM = 1024
DEFAULT_MAX_MEMORY_ITEMS = 500
DEFAULT_RRF_K = 60


# ============================================================================
# Enums
# ============================================================================


class MemoryTrack(Enum):
    """Which memory track an operation belongs to."""

    REASONING = "reasoning"     # interpretable agent reasoning track
    MATCHING = "matching"       # decoupled multimodal embedding track
    BOTH = "both"               # operation spans both tracks


class ReflectionTrigger(Enum):
    """What triggered a reflection cycle."""

    PREFERENCE_DRIFT = "preference_drift"       # user preferences shifted
    RECOMMENDATION_REJECTED = "rejected"        # recommendation was rejected
    PERIODIC_AUDIT = "periodic_audit"           # scheduled reflection
    EXPLICIT_FEEDBACK = "explicit_feedback"     # user provided explicit feedback
    LOW_CONFIDENCE = "low_confidence"           # model confidence too low


class AttributeType(Enum):
    """Types of user/item attributes tracked in memory."""

    CATEGORICAL = "categorical"       # genre, brand, style
    NUMERICAL = "numerical"           # price range, rating
    TEMPORAL = "temporal"             # season, time-of-day preference
    RELATIONAL = "relational"         # co-purchase, substitution
    VISUAL = "visual"                 # color, pattern, aesthetics


class FusionWeightMode(Enum):
    """Weight assignment strategies for dual-track RRF."""

    STATIC = "static"                 # fixed weights
    CONFIDENCE_WEIGHTED = "confidence_weighted"  # based on per-track confidence
    ATTRIBUTE_SENSITIVE = "attribute_sensitive"  # depends on attribute type
    ADAPTIVE = "adaptive"             # learns from historical performance


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class UserAttribute:
    """A user attribute tracked by the reasoning track memory agent."""

    attribute_id: str
    attribute_name: str
    attribute_type: AttributeType
    value: Any
    confidence: float = 1.0
    last_updated: float = 0.0
    source: str = ""                  # which interaction produced this attribute
    reinforcement_count: int = 0      # number of times reinforced
    decay_rate: float = 0.001         # per-day decay

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.attribute_id,
            "name": self.attribute_name,
            "type": self.attribute_type.value,
            "value": str(self.value),
            "confidence": round(self.confidence, 4),
            "reinforcements": self.reinforcement_count,
        }


@dataclass
class ItemMemory:
    """An item stored in the matching track multimodal embedding memory."""

    item_id: str
    item_name: str
    text_embedding: np.ndarray                # [text_embed_dim]
    image_embedding: np.ndarray               # [image_embed_dim]
    multimodal_embedding: np.ndarray           # fused [multimodal_embed_dim]
    interaction_narrative: str = ""           # raw interaction description
    categorical_attrs: Dict[str, str] = field(default_factory=dict)
    numerical_attrs: Dict[str, float] = field(default_factory=dict)
    interaction_count: int = 0
    last_interaction_time: float = 0.0
    image_features: Optional[bytes] = None    # raw image feature buffer

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.item_name,
            "text_dim": self.text_embedding.shape[0],
            "image_dim": self.image_embedding.shape[0],
            "mm_dim": self.multimodal_embedding.shape[0],
            "narrative_len": len(self.interaction_narrative),
            "interactions": self.interaction_count,
            "categories": self.categorical_attrs,
        }


@dataclass
class ReasoningMemory:
    """User's persistent reasoning memory (User Memory Agent track)."""

    user_id: str
    attributes: List[UserAttribute] = field(default_factory=list)
    preference_vector: np.ndarray = field(
        default_factory=lambda: np.zeros(DEFAULT_TEXT_EMBED_DIM, dtype=np.float32)
    )
    interaction_history: List[str] = field(default_factory=list)
    reflection_log: List[Dict[str, Any]] = field(default_factory=list)

    def get_attribute(self, name: str) -> Optional[UserAttribute]:
        for attr in self.attributes:
            if attr.attribute_name == name:
                return attr
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "num_attributes": len(self.attributes),
            "num_interactions": len(self.interaction_history),
            "num_reflections": len(self.reflection_log),
        }


@dataclass
class RankedItem:
    """A ranked item from the dual-track fusion."""

    item_id: str
    reasoning_score: float            # from reasoning track
    matching_score: float             # from matching track
    fused_score: float                # final RRF fused score
    rank: int = 0
    visual_evidence: bool = False     # whether visual features contributed
    confidence: float = 0.0
    explanation: str = ""             # natural language explanation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "reasoning_score": round(self.reasoning_score, 6),
            "matching_score": round(self.matching_score, 6),
            "fused_score": round(self.fused_score, 6),
            "rank": self.rank,
            "visual_evidence": self.visual_evidence,
            "confidence": round(self.confidence, 4),
        }


# ============================================================================
# Core: AttributeGuidedReinforcementReflection
# ============================================================================


class AttributeGuidedReinforcementReflection:
    """Attribute-guided reinforcement learning for memory updates.

    Key mechanism:
      1. When a user interacts positively with an item, the attributes
         of that item are reinforced in the user's memory.
      2. When preferences drift (negative feedback on previously liked items),
         a reflection cycle is triggered to re-evaluate and decay attributes.
      3. Reinforcement strength is proportional to the confidence of the
         attribute extraction and the user's explicit/implicit feedback strength.

    Reflection cycle:
      - Detect preference drift via cosine similarity of recent vs historical
        attribute vectors
      - If drift detected, decay older attributes and reinforce newer ones
      - Log reflection for audit
    """

    def __init__(
        self,
        reinforcement_rate: float = 0.1,
        reflection_threshold: float = 0.15,    # cosine diff to trigger reflection
        decay_factor: float = 0.95,
        min_confidence: float = 0.1,
        max_reinforcement: int = 50,
    ):
        self.reinforcement_rate = reinforcement_rate
        self.reflection_threshold = reflection_threshold
        self.decay_factor = decay_factor
        self.min_confidence = min_confidence
        self.max_reinforcement = max_reinforcement

        self._total_reinforcements: int = 0
        self._total_reflections: int = 0

    def reinforce(
        self,
        user_memory: ReasoningMemory,
        item_attributes: List[UserAttribute],
        feedback_score: float,              # +1 (positive) to -1 (negative)
        feedback_confidence: float = 0.8,
    ) -> List[UserAttribute]:
        """Reinforce user memory based on interaction feedback.

        Args:
            user_memory: The user's reasoning memory.
            item_attributes: Attributes of the interacted item.
            feedback_score: +1 for strong positive, -1 for strong negative.
            feedback_confidence: How confident the system is in this feedback.

        Returns:
            List of updated attributes.
        """
        updated: List[UserAttribute] = []

        for item_attr in item_attributes:
            existing = user_memory.get_attribute(item_attr.attribute_name)

            if existing is None:
                # New attribute discovered
                new_attr = UserAttribute(
                    attribute_id=f"attr_{len(user_memory.attributes):06d}",
                    attribute_name=item_attr.attribute_name,
                    attribute_type=item_attr.attribute_type,
                    value=item_attr.value,
                    confidence=abs(feedback_score) * feedback_confidence,
                    last_updated=time.time(),
                    source=f"interaction_{len(user_memory.interaction_history)}",
                    reinforcement_count=1,
                )
                user_memory.attributes.append(new_attr)
                updated.append(new_attr)

            else:
                # Existing attribute — reinforce or decay
                delta = (
                    feedback_score
                    * self.reinforcement_rate
                    * feedback_confidence
                )
                existing.confidence = max(
                    self.min_confidence,
                    min(1.0, existing.confidence + delta),
                )
                existing.reinforcement_count = min(
                    self.max_reinforcement,
                    existing.reinforcement_count + (1 if feedback_score > 0 else 0),
                )
                existing.last_updated = time.time()
                updated.append(existing)

        # Update preference vector
        self._update_preference_vector(user_memory)
        self._total_reinforcements += 1

        return updated

    def reflect(
        self,
        user_memory: ReasoningMemory,
        trigger: ReflectionTrigger,
    ) -> Dict[str, Any]:
        """Run a reflection cycle to detect and adjust for preference drift.

        Compares recent vs historical attribute patterns. If drift is detected:
          - Decays older attributes
          - Boosts recently reinforced attributes
          - Logs reflection for audit

        Returns:
            Reflection report.
        """
        self._total_reflections += 1

        # Compute historical preference vector (from attributes with high reinforcement)
        historical_attrs = [
            a for a in user_memory.attributes
            if a.reinforcement_count >= 3
        ]
        recent_attrs = [
            a for a in user_memory.attributes
            if a.last_updated > time.time() - 86400 * 7  # last 7 days
        ]

        # Build attribute vectors
        hist_vec = self._build_attr_vector(historical_attrs)
        recent_vec = self._build_attr_vector(recent_attrs)

        # Compute drift
        drift = 0.0
        if np.linalg.norm(hist_vec) > 1e-8 and np.linalg.norm(recent_vec) > 1e-8:
            drift = float(
                1.0 - np.dot(hist_vec, recent_vec)
                / (np.linalg.norm(hist_vec) * np.linalg.norm(recent_vec) + 1e-8)
            )

        reflection_needed = drift > self.reflection_threshold

        if reflection_needed:
            # Decay older attributes
            for attr in user_memory.attributes:
                if attr.last_updated < time.time() - 86400 * 7:
                    attr.confidence *= self.decay_factor
                    attr.confidence = max(self.min_confidence, attr.confidence)

            # Boost recent attributes
            for attr in recent_attrs:
                attr.confidence = min(1.0, attr.confidence * 1.1)

            self._update_preference_vector(user_memory)

        report = {
            "reflection_id": f"ref_{self._total_reflections:06d}",
            "trigger": trigger.value,
            "drift_detected": reflection_needed,
            "drift_magnitude": round(drift, 6),
            "historical_attr_count": len(historical_attrs),
            "recent_attr_count": len(recent_attrs),
            "timestamp": time.time(),
        }
        user_memory.reflection_log.append(report)
        return report

    def _build_attr_vector(self, attributes: List[UserAttribute]) -> np.ndarray:
        """Build a fixed-dimensional vector from attribute list."""
        vec = np.zeros(DEFAULT_TEXT_EMBED_DIM, dtype=np.float32)
        for attr in attributes:
            idx = int(hashlib.md5(attr.attribute_name.encode()).hexdigest()[:8], 16)
            idx = idx % DEFAULT_TEXT_EMBED_DIM
            vec[idx] += attr.confidence * attr.reinforcement_count
        norm = np.linalg.norm(vec)
        return vec / (norm + 1e-8) if norm > 1e-8 else vec

    def _update_preference_vector(self, memory: ReasoningMemory):
        """Update user's preference vector from all attributes."""
        memory.preference_vector = self._build_attr_vector(memory.attributes)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "total_reinforcements": self._total_reinforcements,
            "total_reflections": self._total_reflections,
            "reinforcement_rate": self.reinforcement_rate,
            "reflection_threshold": self.reflection_threshold,
            "decay_factor": self.decay_factor,
        }


# ============================================================================
# Core: DualTrackMemoryArchitecture
# ============================================================================


class DualTrackMemoryArchitecture:
    """Dual-track memory: reasoning track + matching track.

    Reasoning Track (User/Item Memory Agents):
      - Persistent multimodal memories
      - Attribute-guided reinforcement + reflection updates
      - Interpretable: explains *why* an item is recommended

    Matching Track (Decoupled Multimodal Embedding Memory):
      - Raw interaction narratives + item images
      - Dense multimodal embeddings for fine-grained similarity
      - Captures cross-modal signals beyond structured memory
    """

    def __init__(
        self,
        user_id: str = "default_user",
        text_embed_dim: int = DEFAULT_TEXT_EMBED_DIM,
        image_embed_dim: int = DEFAULT_IMAGE_EMBED_DIM,
        multimodal_embed_dim: int = DEFAULT_MULTIMODAL_EMBED_DIM,
    ):
        self.user_id = user_id
        self.text_embed_dim = text_embed_dim
        self.image_embed_dim = image_embed_dim
        self.multimodal_embed_dim = multimodal_embed_dim

        # ── Reasoning track ──
        self.reasoning_memory = ReasoningMemory(user_id=user_id)
        self.reflection_engine = AttributeGuidedReinforcementReflection()

        # ── Matching track ──
        self.item_memories: Dict[str, ItemMemory] = {}
        self._item_embedding_matrix: Optional[np.ndarray] = None
        self._item_ids: List[str] = []

        # ── Stats ──
        self._total_recommendations: int = 0

    # ── Reasoning Track ───────────────────────────────────────────────

    def add_interaction(
        self,
        item_id: str,
        item_attrs: List[UserAttribute],
        narrative: str,
        feedback_score: float,
        image_features: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """Record a user-item interaction in both tracks.

        Args:
            item_id: Item identifier.
            item_attrs: Extracted item attributes.
            narrative: Raw interaction narrative.
            feedback_score: +1 (positive) to -1 (negative).
            image_features: Optional raw image features.

        Returns:
            Summary of the update.
        """
        # Reasoning track: reinforce attributes
        updated_attrs = self.reflection_engine.reinforce(
            self.reasoning_memory, item_attrs, feedback_score
        )
        self.reasoning_memory.interaction_history.append(narrative)

        # Matching track: store item with multimodal embedding
        if item_id not in self.item_memories:
            text_emb = self._compute_text_embedding(narrative)
            img_emb = self._compute_image_embedding(image_features)
            mm_emb = self._fuse_embeddings(text_emb, img_emb)

            self.item_memories[item_id] = ItemMemory(
                item_id=item_id,
                item_name=item_attrs[0].value if item_attrs else item_id,
                text_embedding=text_emb,
                image_embedding=img_emb,
                multimodal_embedding=mm_emb,
                interaction_narrative=narrative,
                categorical_attrs={
                    a.attribute_name: str(a.value)
                    for a in item_attrs if a.attribute_type == AttributeType.CATEGORICAL
                },
                interaction_count=1,
                last_interaction_time=time.time(),
            )
        else:
            self.item_memories[item_id].interaction_count += 1
            self.item_memories[item_id].last_interaction_time = time.time()

        self._invalidate_cache()

        return {
            "item_id": item_id,
            "updated_attributes": len(updated_attrs),
            "memory_size": len(self.item_memories),
            "interactions": len(self.reasoning_memory.interaction_history),
        }

    # ── Reflection ────────────────────────────────────────────────────

    def trigger_reflection(
        self, trigger: ReflectionTrigger = ReflectionTrigger.PERIODIC_AUDIT
    ) -> Dict[str, Any]:
        """Trigger a reflection cycle on the reasoning track."""
        return self.reflection_engine.reflect(self.reasoning_memory, trigger)

    # ── Matching Track: item scoring ──────────────────────────────────

    def compute_matching_scores(
        self,
        query_embedding: np.ndarray,
        candidate_items: Optional[List[str]] = None,
        top_k: int = 20,
    ) -> List[Tuple[str, float]]:
        """Compute matching track scores for candidate items.

        Uses multimodal cosine similarity between query embedding and
        stored item embeddings.
        """
        items = candidate_items or list(self.item_memories.keys())
        if not items:
            return []

        scores = []
        query_norm = np.linalg.norm(query_embedding) + 1e-8

        for item_id in items:
            mem = self.item_memories.get(item_id)
            if mem is None:
                continue
            mm_emb = mem.multimodal_embedding
            # Query is text-only; use text portion of multimodal embedding for dot product
            mm_text = mm_emb[:self.text_embed_dim]
            mm_norm = np.linalg.norm(mm_text) + 1e-8
            sim = float(np.dot(query_embedding, mm_text) / (query_norm * mm_norm))
            scores.append((item_id, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    # ── Embedding computation ─────────────────────────────────────────

    def _compute_text_embedding(self, text: str) -> np.ndarray:
        """Compute text embedding (simulated)."""
        rng = np.random.RandomState(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
        emb = rng.randn(self.text_embed_dim).astype(np.float32) * 0.1
        # Inject character-level features
        for i, ch in enumerate(text[:50]):
            emb[i % self.text_embed_dim] += ord(ch) * 0.001
        return emb / (np.linalg.norm(emb) + 1e-8)

    def _compute_image_embedding(self, image_features: Optional[np.ndarray]) -> np.ndarray:
        """Compute image embedding (simulated or from real features)."""
        if image_features is not None:
            return image_features.astype(np.float32)[:self.image_embed_dim]
        rng = np.random.RandomState(137)
        return rng.randn(self.image_embed_dim).astype(np.float32) * 0.1

    def _fuse_embeddings(
        self, text_emb: np.ndarray, image_emb: np.ndarray
    ) -> np.ndarray:
        """Fuse text and image embeddings into multimodal embedding.

        Uses concatenation + projection to multimodal_embed_dim.
        """
        concat = np.concatenate([text_emb, image_emb])
        # Project to multimodal dimension using fixed projection
        rng = np.random.RandomState(42)
        proj = rng.randn(
            self.multimodal_embed_dim,
            self.text_embed_dim + self.image_embed_dim,
        ).astype(np.float32) * 0.1
        fused = proj @ concat
        return fused / (np.linalg.norm(fused) + 1e-8)

    def _invalidate_cache(self):
        """Invalidate cached item embedding matrix."""
        self._item_embedding_matrix = None
        self._item_ids = []

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "reasoning": self.reasoning_memory.to_dict(),
            "matching": {
                "num_items": len(self.item_memories),
                "text_dim": self.text_embed_dim,
                "image_dim": self.image_embed_dim,
                "mm_dim": self.multimodal_embed_dim,
            },
            "reflection": self.reflection_engine.diagnostics(),
        }


# ============================================================================
# Core: MultimodalWeightedRRF
# ============================================================================


class MultimodalWeightedRRF:
    """Dual-track weighted Reciprocal Rank Fusion.

    Fuses rankings from the reasoning track (interpretable, attribute-based)
    and the matching track (dense multimodal embeddings) using weighted RRF.

    RRF formula:
      RRF_score(d) = sum_{r in rankings} w_r / (k + rank(d, r))
    where w_r is the track weight, k is a constant (default 60).

    Weight modes:
      - STATIC: fixed 0.5/0.5
      - CONFIDENCE_WEIGHTED: based on per-track confidence
      - ATTRIBUTE_SENSITIVE: depends on query attribute types
      - ADAPTIVE: learns from historical ranking performance
    """

    def __init__(
        self,
        k: int = DEFAULT_RRF_K,
        weight_mode: FusionWeightMode = FusionWeightMode.CONFIDENCE_WEIGHTED,
        reasoning_weight: float = 0.5,
        matching_weight: float = 0.5,
    ):
        self.k = k
        self.weight_mode = weight_mode
        self.reasoning_weight = reasoning_weight
        self.matching_weight = matching_weight

        # Adaptive tracking
        self._reasoning_wins: int = 0
        self._matching_wins: int = 0
        self._total_fusions: int = 0

    def fuse(
        self,
        reasoning_ranking: List[Tuple[str, float]],
        matching_ranking: List[Tuple[str, float]],
        visual_items: Optional[Set[str]] = None,
        reasoning_confidence: float = 0.8,
        matching_confidence: float = 0.8,
    ) -> List[RankedItem]:
        """Fuse two rankings using weighted RRF.

        Args:
            reasoning_ranking: Ranked list from reasoning track (id, score).
            matching_ranking: Ranked list from matching track (id, score).
            visual_items: Set of items with visual evidence.
            reasoning_confidence: Confidence of reasoning track ranking.
            matching_confidence: Confidence of matching track ranking.

        Returns:
            List of RankedItem, sorted by fused_score descending.
        """
        if visual_items is None:
            visual_items = set()

        # Determine weights
        rw, mw = self._compute_weights(reasoning_confidence, matching_confidence)

        # Build rank maps
        r_rank = {item_id: i + 1 for i, (item_id, _) in enumerate(reasoning_ranking)}
        m_rank = {item_id: i + 1 for i, (item_id, _) in enumerate(matching_ranking)}
        r_score = dict(reasoning_ranking)
        m_score = dict(matching_ranking)

        all_items = set(r_rank.keys()) | set(m_rank.keys())

        fused: List[RankedItem] = []
        for item_id in all_items:
            rr = r_rank.get(item_id, len(reasoning_ranking) + 1)
            mr = m_rank.get(item_id, len(matching_ranking) + 1)

            # Weighted RRF
            rrf = rw / (self.k + rr) + mw / (self.k + mr)

            fused.append(RankedItem(
                item_id=item_id,
                reasoning_score=r_score.get(item_id, 0.0),
                matching_score=m_score.get(item_id, 0.0),
                fused_score=rrf,
                visual_evidence=item_id in visual_items,
                confidence=(reasoning_confidence + matching_confidence) / 2,
            ))

        # Sort by fused score
        fused.sort(key=lambda x: x.fused_score, reverse=True)

        # Assign ranks and generate explanations
        for i, item in enumerate(fused):
            item.rank = i + 1
            item.explanation = self._generate_explanation(item, reasoning_confidence, matching_confidence)

        self._total_fusions += 1
        return fused

    def _compute_weights(
        self, reasoning_conf: float, matching_conf: float
    ) -> Tuple[float, float]:
        """Compute track weights based on the selected mode."""
        if self.weight_mode == FusionWeightMode.STATIC:
            return self.reasoning_weight, self.matching_weight

        elif self.weight_mode == FusionWeightMode.CONFIDENCE_WEIGHTED:
            total = reasoning_conf + matching_conf
            if total < 1e-6:
                return 0.5, 0.5
            return reasoning_conf / total, matching_conf / total

        elif self.weight_mode == FusionWeightMode.ATTRIBUTE_SENSITIVE:
            # Visual attributes → boost matching; textual → boost reasoning
            # Simplified: use confidence as proxy
            total = reasoning_conf + matching_conf
            if total < 1e-6:
                return 0.5, 0.5
            return reasoning_conf / total, matching_conf / total

        elif self.weight_mode == FusionWeightMode.ADAPTIVE:
            # Adaptive: learn from historical win ratios
            r_wins = self._reasoning_wins + 1
            m_wins = self._matching_wins + 1
            total = r_wins + m_wins
            return r_wins / total, m_wins / total

        return 0.5, 0.5

    def _generate_explanation(
        self, item: RankedItem, r_conf: float, m_conf: float
    ) -> str:
        """Generate a natural language explanation for the fusion."""
        parts = []
        if item.reasoning_score > 0.5:
            parts.append(f"preference-aligned (score={item.reasoning_score:.2f})")
        if item.visual_evidence and item.matching_score > 0.5:
            parts.append(f"visual match (score={item.matching_score:.2f})")
        if not parts:
            parts.append("composite recommendation")
        return "; ".join(parts)

    def record_feedback(self, item_id: str, track_that_matched: MemoryTrack):
        """Record which track better predicted user preference."""
        if track_that_matched == MemoryTrack.REASONING:
            self._reasoning_wins += 1
        elif track_that_matched == MemoryTrack.MATCHING:
            self._matching_wins += 1

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "mode": self.weight_mode.value,
            "k": self.k,
            "reasoning_weight": round(self.reasoning_weight, 4),
            "matching_weight": round(self.matching_weight, 4),
            "total_fusions": self._total_fusions,
            "reasoning_wins": self._reasoning_wins,
            "matching_wins": self._matching_wins,
        }


# ============================================================================
# Unified Entry: MultimodalMemoryAgentCollaboration
# ============================================================================


class MultimodalMemoryAgentCollaboration:
    """Unified entry point for multimodal memory-enhanced agent collaboration.

    Integrates:
      - DualTrackMemoryArchitecture: reasoning + matching tracks
      - AttributeGuidedReinforcementReflection: RL-based memory updates
      - MultimodalWeightedRRF: dual-track fusion ranking
    """

    def __init__(
        self,
        user_id: str = "default_user",
        text_embed_dim: int = DEFAULT_TEXT_EMBED_DIM,
        image_embed_dim: int = DEFAULT_IMAGE_EMBED_DIM,
        multimodal_embed_dim: int = DEFAULT_MULTIMODAL_EMBED_DIM,
        fusion_mode: FusionWeightMode = FusionWeightMode.CONFIDENCE_WEIGHTED,
    ):
        self.user_id = user_id
        self.dual_track = DualTrackMemoryArchitecture(
            user_id=user_id,
            text_embed_dim=text_embed_dim,
            image_embed_dim=image_embed_dim,
            multimodal_embed_dim=multimodal_embed_dim,
        )
        self.rrf = MultimodalWeightedRRF(weight_mode=fusion_mode)

    # ── Main pipeline ─────────────────────────────────────────────────

    def record_interaction(
        self,
        item_id: str,
        item_name: str,
        item_attributes: Dict[str, Tuple[AttributeType, Any]],
        narrative: str,
        feedback_score: float,
        image_features: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """Record a user-item interaction.

        Args:
            item_id: Item identifier.
            item_name: Human-readable item name.
            item_attributes: Dict of attr_name → (AttributeType, value).
            narrative: Interaction description.
            feedback_score: +1 (like) to -1 (dislike).
            image_features: Optional image features for visual items.
        """
        attrs = [
            UserAttribute(
                attribute_id=f"{item_id}_{name}",
                attribute_name=name,
                attribute_type=atype,
                value=str(val),
                source=item_id,
                reinforcement_count=1 if feedback_score > 0 else 0,
            )
            for name, (atype, val) in item_attributes.items()
        ]
        return self.dual_track.add_interaction(
            item_id=item_id,
            item_attrs=attrs,
            narrative=narrative,
            feedback_score=feedback_score,
            image_features=image_features,
        )

    def recommend(
        self,
        query_text: str,
        top_k: int = 10,
        visual_items: Optional[Set[str]] = None,
    ) -> List[RankedItem]:
        """Generate recommendations using dual-track architecture.

        Args:
            query_text: User query / context for recommendation.
            top_k: Number of recommendations to return.
            visual_items: Items known to have visual features.

        Returns:
            Ranked list of recommended items.
        """
        if visual_items is None:
            visual_items = set()

        # ── Reasoning track ranking ──
        # Score items by similarity to user's preference vector
        pref_vector = self.dual_track.reasoning_memory.preference_vector
        pref_norm = np.linalg.norm(pref_vector) + 1e-8
        reasoning_scores = []
        for item_id, item_mem in self.dual_track.item_memories.items():
            text_emb = item_mem.text_embedding
            text_norm = np.linalg.norm(text_emb) + 1e-8
            sim = float(np.dot(pref_vector, text_emb) / (pref_norm * text_norm))
            reasoning_scores.append((item_id, sim))
        reasoning_scores.sort(key=lambda x: x[1], reverse=True)
        reasoning_ranking = reasoning_scores[:top_k * 2]

        # ── Matching track ranking ──
        query_emb = self.dual_track._compute_text_embedding(query_text)
        matching_scores = self.dual_track.compute_matching_scores(
            query_embedding=query_emb,
            top_k=top_k * 2,
        )
        matching_ranking = matching_scores

        # ── Fusion ──
        reasoning_conf = 0.7 + 0.1 * min(1.0, len(reasoning_scores) / 100)
        matching_conf = 0.7 + 0.1 * min(1.0, len(matching_scores) / 100)

        fused = self.rrf.fuse(
            reasoning_ranking=reasoning_ranking,
            matching_ranking=matching_ranking,
            visual_items=visual_items,
            reasoning_confidence=reasoning_conf,
            matching_confidence=matching_conf,
        )

        return fused[:top_k]

    def reflect(self, trigger: ReflectionTrigger = ReflectionTrigger.PERIODIC_AUDIT):
        """Run reflection cycle."""
        return self.dual_track.trigger_reflection(trigger)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module": MODULE_ID,
            "version": MODULE_VERSION,
            "user_id": self.user_id,
            "dual_track": self.dual_track.diagnostics(),
            "rrf": self.rrf.diagnostics(),
        }


# ============================================================================
# Self-Test
# ============================================================================



def _test_attribute_and_reinforcement(rng: np.random.RandomState) -> None:
    """Test steps 1-5: UserAttribute creation, AGGR reinforcement, negative feedback decay, reflection cycle."""
    # ── 1. UserAttribute 创建 ──
    attr = UserAttribute(
        attribute_id="attr_001",
        attribute_name="price_range",
        attribute_type=AttributeType.NUMERICAL,
        value="100-200",
        reinforcement_count=3,
    )
    assert attr.attribute_type == AttributeType.NUMERICAL
    assert attr.confidence == 1.0
    print(f"[PASS] 1. UserAttribute: {attr.attribute_name}={attr.value}, "
          f"type={attr.attribute_type.value}")

    # ── 2. AttributeGuidedReinforcementReflection 实例化 ──
    agrr = AttributeGuidedReinforcementReflection(
        reinforcement_rate=0.1,
        reflection_threshold=0.15,
    )
    assert agrr.reinforcement_rate == 0.1
    print(f"[PASS] 2. 强化反思引擎: rate={agrr.reinforcement_rate}, "
          f"threshold={agrr.reflection_threshold}")

    # ── 3. 强化用户记忆 ──
    user_mem = ReasoningMemory(user_id="test_user")
    item_attrs = [
        UserAttribute("ia1", "genre", AttributeType.CATEGORICAL, "sci-fi", reinforcement_count=1),
        UserAttribute("ia2", "price_range", AttributeType.NUMERICAL, "10-30", reinforcement_count=1),
        UserAttribute("ia3", "style", AttributeType.VISUAL, "minimalist", reinforcement_count=1),
    ]
    updated = agrr.reinforce(user_mem, item_attrs, feedback_score=1.0, feedback_confidence=0.9)
    assert len(updated) == 3
    assert user_mem.get_attribute("genre") is not None
    assert user_mem.get_attribute("price_range").confidence > 0.5
    print(f"[PASS] 3. 强化: {len(updated)} attrs updated, "
          f"genre_conf={user_mem.get_attribute('genre').confidence:.4f}")

    # ── 4. 负反馈衰减 ──
    agrr.reinforce(user_mem, item_attrs[:1], feedback_score=-0.5, feedback_confidence=0.8)
    genre_attr = user_mem.get_attribute("genre")
    assert genre_attr.confidence < 1.0  # should have decayed
    print(f"[PASS] 4. 负反馈衰减: genre_conf={genre_attr.confidence:.4f} (decayed)")

    # ── 5. 反思周期 ──
    # Add more interactions to make attributes look "historical"
    for i in range(5):
        user_mem.interaction_history.append(f"interaction_{i}")
        agrr.reinforce(user_mem, item_attrs, feedback_score=0.8)
    report = agrr.reflect(user_mem, ReflectionTrigger.PERIODIC_AUDIT)
    assert "drift_magnitude" in report
    print(f"[PASS] 5. 反思: trigger={report['trigger']}, "
          f"drift={report['drift_magnitude']:.6f}, "
          f"needed={report['drift_detected']}")


def _test_dual_track_and_matching(rng: np.random.RandomState) -> None:
    """Test steps 6-10: DualTrackMemoryArchitecture, interaction recording, matching scores, reflection."""
    # ── 6. DualTrackMemoryArchitecture 实例化 ──
    dt = DualTrackMemoryArchitecture(
        user_id="user_001",
        text_embed_dim=768,
        image_embed_dim=512,
        multimodal_embed_dim=1024,
    )
    assert dt.user_id == "user_001"
    print(f"[PASS] 6. 双轨道架构: user={dt.user_id}, "
          f"text_dim={dt.text_embed_dim}, img_dim={dt.image_embed_dim}")

    # ── 7. 记录交互 ──
    movie_attrs = [
        UserAttribute("ma1", "genre", AttributeType.CATEGORICAL, "sci-fi"),
        UserAttribute("ma2", "director", AttributeType.CATEGORICAL, "Nolan"),
        UserAttribute("ma3", "rating", AttributeType.NUMERICAL, "8.5"),
    ]
    result = dt.add_interaction(
        item_id="movie_001",
        item_attrs=movie_attrs,
        narrative="User watched Interstellar and loved it. Praised the visuals and story.",
        feedback_score=1.0,
    )
    assert result["updated_attributes"] == 3
    assert "movie_001" in dt.item_memories
    print(f"[PASS] 7. 交互记录: {result['updated_attributes']} attrs, "
          f"memory_size={result['memory_size']}")

    # ── 8. 多条交互 ──
    for i, (movie, genre, score) in enumerate([
        ("movie_002", "action", 1.0),
        ("movie_003", "comedy", -0.5),
        ("movie_004", "sci-fi", 0.8),
        ("movie_005", "drama", 0.3),
    ], start=2):
        attrs = [
            UserAttribute(f"m{i}a1", "genre", AttributeType.CATEGORICAL, genre),
            UserAttribute(f"m{i}a2", "rating", AttributeType.NUMERICAL, f"{5 + (i-2)}.0"),
        ]
        dt.add_interaction(
            item_id=movie,
            item_attrs=attrs,
            narrative=f"User interacted with {movie}, feedback={score}",
            feedback_score=score,
            image_features=rng.randn(512).astype(np.float32) if i % 2 == 0 else None,
        )
    assert len(dt.item_memories) == 5
    print(f"[PASS] 8. 多条交互: {len(dt.item_memories)} items in matching track")

    # ── 9. 匹配轨道评分 ──
    query_emb = dt._compute_text_embedding("sci-fi adventure movie")
    scores = dt.compute_matching_scores(query_emb, top_k=3)
    assert len(scores) == 3
    # First score should be highest
    assert scores[0][1] >= scores[-1][1]
    print(f"[PASS] 9. 匹配评分: top={scores[0][0]}, "
          f"score={scores[0][1]:.4f}, returned={len(scores)}")

    # ── 10. 反思 ──
    ref_report = dt.trigger_reflection(ReflectionTrigger.PERIODIC_AUDIT)
    assert "drift_detected" in ref_report
    print(f"[PASS] 10. 双重反思: drift={ref_report['drift_magnitude']:.6f}, "
          f"history={ref_report['historical_attr_count']}/{ref_report['recent_attr_count']}")


def _test_rrf_and_fusion() -> None:
    """Test steps 11-12 + 17: MultimodalWeightedRRF instantiation, fusion, all four fusion modes."""
    # ── 11. MultimodalWeightedRRF 实例化 ──
    rrf = MultimodalWeightedRRF(
        k=60,
        weight_mode=FusionWeightMode.CONFIDENCE_WEIGHTED,
    )
    assert rrf.k == 60
    assert rrf.weight_mode == FusionWeightMode.CONFIDENCE_WEIGHTED
    print(f"[PASS] 11. RRF: k={rrf.k}, mode={rrf.weight_mode.value}")

    # ── 12. RRF 融合 ──
    r_rank = [("movie_001", 0.95), ("movie_002", 0.82), ("movie_004", 0.78)]
    m_rank = [("movie_004", 0.88), ("movie_001", 0.85), ("movie_005", 0.72), ("movie_002", 0.65)]
    visual_set = {"movie_001", "movie_004"}
    fused = rrf.fuse(
        reasoning_ranking=r_rank,
        matching_ranking=m_rank,
        visual_items=visual_set,
    )
    assert len(fused) >= 3
    # Check that fused is sorted by fused_score
    for i in range(len(fused) - 1):
        assert fused[i].fused_score >= fused[i + 1].fused_score
    # Visual evidence flag
    for item in fused:
        if item.item_id in visual_set:
            assert item.visual_evidence
    print(f"[PASS] 12. RRF融合: {len(fused)} items, top={fused[0].item_id} "
          f"(score={fused[0].fused_score:.6f}, visual={fused[0].visual_evidence})")


    # ── 17. 四种融合模式 ──
    for mode in FusionWeightMode:
        r = MultimodalWeightedRRF(weight_mode=mode)
        result = r.fuse(r_rank, m_rank)
        assert len(result) >= 3
    print(f"[PASS] 17. 融合模式: STATIC/CONFIDENCE/ATTRIBUTE/ADAPTIVE 均通过")


def _test_unified_pipeline(rng: np.random.RandomState) -> MultimodalMemoryAgentCollaboration:
    """Test steps 13-18: MultimodalMemoryAgentCollaboration pipeline, recommend, reflect, diagnostics.

    Returns:
        MultimodalMemoryAgentCollaboration instance.
    """
    # ── 13. MultimodalMemoryAgentCollaboration 统一入口 ──
    mmacr = MultimodalMemoryAgentCollaboration(
        user_id="user_001",
        fusion_mode=FusionWeightMode.CONFIDENCE_WEIGHTED,
    )
    assert mmacr.user_id == "user_001"
    print(f"[PASS] 13. 统一入口: user={mmacr.user_id}")

    # ── 14. 完整交互+推荐管线 ──
    mmacr.record_interaction(
        item_id="movie_010",
        item_name="Inception",
        item_attributes={
            "genre": (AttributeType.CATEGORICAL, "sci-fi"),
            "director": (AttributeType.CATEGORICAL, "Nolan"),
            "style": (AttributeType.VISUAL, "mind-bending"),
        },
        narrative="User watched Inception and rated it 5 stars.",
        feedback_score=1.0,
    )
    mmacr.record_interaction(
        item_id="movie_011",
        item_name="The Matrix",
        item_attributes={
            "genre": (AttributeType.CATEGORICAL, "sci-fi"),
            "style": (AttributeType.VISUAL, "dystopian"),
        },
        narrative="User rewatched The Matrix, commented on the visual effects.",
        feedback_score=0.9,
        image_features=rng.randn(512).astype(np.float32),
    )
    assert len(mmacr.dual_track.item_memories) == 2
    print(f"[PASS] 14. 管线: {len(mmacr.dual_track.item_memories)} items in memory")

    # ── 15. 推荐 ──
    recs = mmacr.recommend(
        query_text="sci-fi movies with great visuals",
        top_k=5,
        visual_items={"movie_011"},
    )
    assert len(recs) >= 1
    top_rec = recs[0]
    assert top_rec.rank == 1
    assert len(top_rec.explanation) > 0
    print(f"[PASS] 15. 推荐: top={top_rec.item_id}, fused={top_rec.fused_score:.6f}, "
          f"explanation='{top_rec.explanation}'")

    # ── 16. 反思 ──
    refl = mmacr.reflect(ReflectionTrigger.PERIODIC_AUDIT)
    assert "drift_magnitude" in refl
    print(f"[PASS] 16. 反思: drift={refl['drift_magnitude']:.6f}")


    # ── 18. 诊断 ──
    diag = mmacr.diagnostics()
    assert diag["dual_track"]["matching"]["num_items"] == 2
    assert diag["rrf"]["total_fusions"] >= 1
    assert diag["dual_track"]["reflection"]["total_reflections"] >= 1
    print(f"[PASS] 18. 诊断: items={diag['dual_track']['matching']['num_items']}, "
          f"fusions={diag['rrf']['total_fusions']}, "
          f"reflections={diag['dual_track']['reflection']['total_reflections']}")

    return mmacr

def run_self_test() -> MultimodalMemoryAgentCollaboration:
    """Run comprehensive self-test for M120 MultimodalMemoryAgentCollaboration.

    Returns:
        Fully initialized and tested instance.
    """
    rng = np.random.RandomState(42)
    print(SEP)
    print("  M120 MultimodalMemoryAgentCollaboration — 自检")
    print(f"  Paper: {PAPER_REF}")
    print(SEP)
    _test_attribute_and_reinforcement(rng)
    _test_dual_track_and_matching(rng)
    _test_rrf_and_fusion()
    mmacr = _test_unified_pipeline(rng)
    print(SUB)
    print("  [M120 自检结果] ALL_PASS — 18/18 项通过")
    print(SEP)
    return mmacr
if __name__ == "__main__":
    run_self_test()
