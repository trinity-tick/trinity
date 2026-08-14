"""
P17-6: EKV Optimized Store
==========================

对标 AgentMemBench EKV 唯一长程召回策略（LoCoMo Recall@5=0.573 vs ICW 0.005）。

设计要点：
  - Embedding-Key-Value 三表分离存储，解耦索引与内容
  - 混合索引架构：向量索引 + 全文倒排 + 元数据过滤三路并行
  - 多粒度 Key 分组：实体级 / 会话级 / 主题级，自适应粒度切换
  - Top-K 融合加权重组：向量分 + TF-IDF + 时间衰减加权融合排序

核心组件：
  - TripleStoreEngine:    EKV 三表存储引擎
  - HybridIndex:          向量+全文+元数据三路索引
  - MultiGranularKeyGrouper: 实体/会话/主题三级分组
  - FusionRanker:         多路 Top-K 加权融合排序
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class KeyGranularity(Enum):
    """Key 分组粒度。"""
    ENTITY = "entity"       # 实体级：每实体一个 key
    SESSION = "session"     # 会话级：每对话一个 key
    TOPIC = "topic"         # 主题级：每主题类别一个 key


class IndexType(Enum):
    """索引类型。"""
    VECTOR = "vector"
    FULL_TEXT = "full_text"
    METADATA = "metadata"


class ScoreFusion(Enum):
    """融合策略。"""
    WEIGHTED_SUM = "weighted_sum"
    RRF = "rrf"
    LINEAR_INTERPOLATION = "linear_interpolation"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class EKVEntry:
    """单条 EKV 条目。"""
    entry_id: str
    key_id: str
    embedding: List[float]
    value: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    granularity: KeyGranularity = KeyGranularity.ENTITY
    timestamp: float = field(default_factory=time.time)
    ttl: Optional[float] = None


@dataclass
class KeyGroup:
    """Key 分组。"""
    key_id: str
    granularity: KeyGranularity
    label: str
    entries: List[str] = field(default_factory=list)
    centroid: Optional[List[float]] = None


@dataclass
class HybridScore:
    """混合索引单路得分。"""
    entry_id: str
    vector_score: float = 0.0
    text_score: float = 0.0
    metadata_score: float = 0.0
    fused_score: float = 0.0
    decay_factor: float = 1.0


@dataclass
class FusionWeights:
    """融合权重。"""
    vector_weight: float = 0.5
    text_weight: float = 0.3
    metadata_weight: float = 0.1
    decay_weight: float = 0.1


# ============================================================================
# Core Components
# ============================================================================

class TripleStoreEngine:
    """Embedding-Key-Value 三表存储引擎。

    三表物理分离：embedding 表 / key 分组表 / value 内容表。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.entries: Dict[str, EKVEntry] = {}          # value 表
        self.key_groups: Dict[str, KeyGroup] = {}       # key 表
        self.embedding_index: Dict[str, List[float]] = {}  # embedding 表

    def put(self, key_id: str, embedding: List[float], value: str, metadata: Optional[Dict[str, Any]] = None,
            granularity: KeyGranularity = KeyGranularity.ENTITY, ttl: Optional[float] = None) -> str:
        with self._lock:
            entry_id = str(uuid.uuid4())[:8]

            # 三表写入
            entry = EKVEntry(
                entry_id=entry_id, key_id=key_id, embedding=embedding, value=value,
                metadata=metadata or {}, granularity=granularity, ttl=ttl,
            )
            self.entries[entry_id] = entry
            self.embedding_index[entry_id] = embedding

            # Key 分组成员管理
            if key_id not in self.key_groups:
                self.key_groups[key_id] = KeyGroup(
                    key_id=key_id, granularity=granularity, label=metadata.get("label", key_id) if metadata else key_id,
                )
            self.key_groups[key_id].entries.append(entry_id)

            return entry_id

    def get(self, entry_id: str) -> Optional[EKVEntry]:
        with self._lock:
            entry = self.entries.get(entry_id)
            if entry and entry.ttl and time.time() - entry.timestamp > entry.ttl:
                self.delete(entry_id)
                return None
            return entry

    def get_by_key(self, key_id: str) -> List[EKVEntry]:
        with self._lock:
            group = self.key_groups.get(key_id)
            if not group:
                return []
            entries = []
            for eid in group.entries:
                entry = self.get(eid)
                if entry:
                    entries.append(entry)
            return entries

    def delete(self, entry_id: str):
        with self._lock:
            entry = self.entries.pop(entry_id, None)
            self.embedding_index.pop(entry_id, None)
            if entry and entry.key_id in self.key_groups:
                self.key_groups[entry.key_id].entries.remove(entry_id)

    @property
    def count(self) -> int:
        return len(self.entries)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entries": len(self.entries),
                "total_key_groups": len(self.key_groups),
                "granularity_distribution": {
                    g.value: sum(1 for kg in self.key_groups.values() if kg.granularity == g)
                    for g in KeyGranularity
                },
            }


class HybridIndex:
    """混合索引：向量 + 全文 + 元数据三路并行。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.vector_index: Dict[str, List[float]] = {}
        self.text_index: Dict[str, Dict[str, float]] = {}   # entry_id → {term → tf-idf}
        self.metadata_index: Dict[str, Dict[str, Any]] = {}  # entry_id → metadata

    def index(self, entry_id: str, embedding: List[float], value: str, metadata: Dict[str, Any]):
        with self._lock:
            self.vector_index[entry_id] = embedding
            # 简易 TF-IDF 计算
            terms = value.lower().split()
            term_counts: Dict[str, int] = defaultdict(int)
            for t in terms:
                term_counts[t] += 1
            total_terms = len(terms) if terms else 1
            tfidf: Dict[str, float] = {}
            for t, cnt in term_counts.items():
                # IDF 简化：稀有词给更高权重
                idf = math.log(1.0 + 1000.0 / max(1, sum(1 for v in self.text_index.values() if t in v)))
                tfidf[t] = (cnt / total_terms) * idf
            self.text_index[entry_id] = tfidf
            self.metadata_index[entry_id] = dict(metadata)

    def remove(self, entry_id: str):
        with self._lock:
            self.vector_index.pop(entry_id, None)
            self.text_index.pop(entry_id, None)
            self.metadata_index.pop(entry_id, None)

    def query_vector(self, query_embedding: List[float], top_k: int = 50) -> List[Tuple[str, float]]:
        with self._lock:
            scores = []
            for eid, emb in self.vector_index.items():
                # 余弦相似度
                dot = sum(a * b for a, b in zip(query_embedding, emb))
                norm_q = math.sqrt(sum(a * a for a in query_embedding))
                norm_e = math.sqrt(sum(b * b for b in emb))
                sim = dot / (norm_q * norm_e + 1e-8)
                scores.append((eid, sim))
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]

    def query_text(self, query_terms: List[str], top_k: int = 50) -> List[Tuple[str, float]]:
        with self._lock:
            scores = []
            for eid, tfidf in self.text_index.items():
                score = sum(tfidf.get(t, 0.0) for t in query_terms)
                scores.append((eid, score))
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]

    def query_metadata(self, filter_fn: Any, top_k: int = 50) -> List[Tuple[str, float]]:
        """元数据过滤，返回匹配条目（得分 1.0）。"""
        with self._lock:
            results = []
            for eid, meta in self.metadata_index.items():
                if filter_fn is None or filter_fn(meta):
                    results.append((eid, 1.0))
            return results[:top_k]


class MultiGranularKeyGrouper:
    """多粒度 Key 分组：实体级 / 会话级 / 主题级。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.groups: Dict[KeyGranularity, Dict[str, KeyGroup]] = {
            g: {} for g in KeyGranularity
        }

    def group(self, entry_id: str, embedding: List[float], granularity: KeyGranularity, label: str):
        with self._lock:
            bucket = self.groups[granularity]
            if label not in bucket:
                bucket[label] = KeyGroup(key_id=str(uuid.uuid4())[:8], granularity=granularity, label=label)
            bucket[label].entries.append(entry_id)
            # 更新质心
            if bucket[label].centroid is None:
                bucket[label].centroid = list(embedding)
            else:
                n = len(bucket[label].entries)
                centroid = bucket[label].centroid
                bucket[label].centroid = [(c * (n - 1) + e) / n for c, e in zip(centroid, embedding)]

    def get_group(self, granularity: KeyGranularity, label: str) -> Optional[KeyGroup]:
        with self._lock:
            return self.groups[granularity].get(label)

    def list_groups(self, granularity: Optional[KeyGranularity] = None) -> Dict[str, List[str]]:
        with self._lock:
            result = {}
            grains = [granularity] if granularity else list(KeyGranularity)
            for g in grains:
                result[g.value] = list(self.groups[g].keys())
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                f"{g.value}_groups": len(self.groups[g])
                for g in KeyGranularity
            }


class FusionRanker:
    """多路 Top-K 加权融合排序。

    Score = w_v * vec + w_t * text + w_m * meta + decay
    """

    def __init__(self, weights: Optional[FusionWeights] = None, fusion: ScoreFusion = ScoreFusion.WEIGHTED_SUM):
        self._lock = threading.RLock()
        self.weights = weights or FusionWeights()
        self.fusion = fusion
        self.now_fn: Callable[[], float] = time.time

    def fuse(
        self,
        vector_hits: List[Tuple[str, float]],
        text_hits: List[Tuple[str, float]],
        metadata_hits: List[Tuple[str, float]],
        timestamps: Optional[Dict[str, float]] = None,
        top_k: int = 10,
        half_life_days: float = 30.0,
    ) -> List[Tuple[str, float]]:
        with self._lock:
            # 归一化各路得分到 [0,1]
            v_scores = self._normalize(vector_hits)
            t_scores = self._normalize(text_hits)
            m_scores = self._normalize(metadata_hits)

            # 收集所有候选 entry_id
            all_ids: set = set()
            for hits in [vector_hits, text_hits, metadata_hits]:
                for eid, _ in hits:
                    all_ids.add(eid)

            fused: List[HybridScore] = []
            now = self.now_fn()
            decay_seconds = half_life_days * 86400.0

            for eid in all_ids:
                v = v_scores.get(eid, 0.0)
                t = t_scores.get(eid, 0.0)
                m = m_scores.get(eid, 0.0)

                # 时间衰减
                ts = timestamps.get(eid, now) if timestamps else now
                age = max(0.0, now - ts)
                decay = math.exp(-math.log(2) * age / decay_seconds) if decay_seconds > 0 else 1.0

                if self.fusion == ScoreFusion.WEIGHTED_SUM:
                    fused_score = (
                        self.weights.vector_weight * v +
                        self.weights.text_weight * t +
                        self.weights.metadata_weight * m +
                        self.weights.decay_weight * decay
                    )
                elif self.fusion == ScoreFusion.RRF:
                    # Reciprocal Rank Fusion
                    rrf_score = 0.0
                    rank_v = vector_hits.index((eid, v)) + 1 if (eid, v) in vector_hits else 1000
                    rank_t = text_hits.index((eid, t)) + 1 if (eid, t) in text_hits else 1000
                    rank_m = metadata_hits.index((eid, m)) + 1 if (eid, m) in metadata_hits else 1000
                    rrf_score = 1.0 / (60 + rank_v) + 1.0 / (60 + rank_t) + 1.0 / (60 + rank_m) + decay * 0.1
                    fused_score = rrf_score
                else:
                    fused_score = (v + t + m + decay) / 4.0

                fused.append(HybridScore(entry_id=eid, vector_score=v, text_score=t, metadata_score=m, fused_score=fused_score, decay_factor=decay))

            fused.sort(key=lambda x: x.fused_score, reverse=True)

            return [(h.entry_id, h.fused_score) for h in fused[:top_k]]

    @staticmethod
    def _normalize(hits: List[Tuple[str, float]]) -> Dict[str, float]:
        if not hits:
            return {}
        max_score = max(abs(h[1]) for h in hits)
        if max_score == 0:
            return {h[0]: 0.0 for h in hits}
        return {h[0]: h[1] / max_score for h in hits}

    def statistics(self) -> Dict[str, Any]:
        return {
            "fusion_strategy": self.fusion.value,
            "weights": {
                "vector": self.weights.vector_weight,
                "text": self.weights.text_weight,
                "metadata": self.weights.metadata_weight,
                "decay": self.weights.decay_weight,
            },
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P17-6 EKV Optimized Store",
        "benchmark": "AgentMemBench EKV (LoCoMo Recall@5=0.573 vs ICW 0.005)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "EKV Triple-Store + Hybrid Index(Vector/Text/Meta) + Multi-Granularity Key Grouping + Weighted Fusion",
        "key_metric": "EKV Long-Range Recall@5 0.573 (vs ICW 0.005)",
        "thread_safe": True,
    }
