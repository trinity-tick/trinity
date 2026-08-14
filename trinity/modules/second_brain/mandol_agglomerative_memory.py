"""
MANDOL — Agglomerative Memory with Unified Retrieval
=====================================================
arXiv 2606.29778 · P46-7

统一凝聚记忆图: 在单一数据结构中融合 key-value + 向量 + 图三种检索原语。
两层分层记忆: 基础层(原始) → 抽象层(聚合后可追溯抽象)。查询自适应路由器
自动选择检索路径。定量去噪与冲突消解 (LLM-free)。

设计要点:
  - MandolAgglomerativeGraph: 统一凝聚记忆图
  - HierarchicalMemoryLayer: 两层分层
  - QueryAdaptiveRouter: 查询自适应路由
  - QuantitativeDenoiser: 定量去噪 + token 预算约束
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict
import hashlib

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RetrievalPath(Enum):
    KV_LOOKUP = auto()      # key-value 精确查找
    SEMANTIC_SEARCH = auto() # 向量语义搜索
    GRAPH_TRAVERSE = auto()  # 图遍历


class QueryType(Enum):
    FACTUAL = auto()         # 事实查询 → KV_LOOKUP
    CONCEPTUAL = auto()      # 概念查询 → SEMANTIC_SEARCH
    RELATIONAL = auto()      # 关系查询 → GRAPH_TRAVERSE
    HYBRID = auto()          # 混合查询 → 多路径融合


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class AbstractMemoryNode:
    """抽象记忆节点——聚合后的可追溯抽象记忆。"""
    node_id: str
    summary: str = ""               # 抽象摘要
    source_ids: List[str] = field(default_factory=list)  # 追溯源节点 ID
    embedding_vector: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)
    confidence: float = 1.0          # 聚合置信度
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SemanticGraphFusion:
    """语义图融合——边权重反映语义关系强度。"""
    source_id: str
    target_id: str
    relation: str = "related_to"
    weight: float = 0.5
    evidence: str = ""               # 融合依据
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# QuantitativeDenoiser
# ---------------------------------------------------------------------------

class QuantitativeDenoiser:
    """定量去噪与冲突消解 (LLM-free)。

    基于统计信号检测: 低频来源(噪声) + 矛盾信号(冲突) 在 token 预算内过滤。
    """

    def __init__(self, noise_threshold: float = 0.05, conflict_threshold: float = 0.3) -> None:
        self.noise_threshold = noise_threshold
        self.conflict_threshold = conflict_threshold
        self._source_freq: Dict[str, int] = defaultdict(int)
        self._total_signals: int = 0
        self._lock = threading.RLock()

    def observe(self, source: str) -> None:
        """记录来源信号。"""
        with self._lock:
            self._source_freq[source] += 1
            self._total_signals += 1

    def filter(
        self, items: List[Dict[str, Any]], token_budget: int = 2048,
    ) -> List[Dict[str, Any]]:
        """过滤噪声和冲突, 返回 token 预算内的干净条目。"""
        with self._lock:
            if self._total_signals == 0:
                return items

            results: List[Tuple[float, Dict[str, Any]]] = []

            for item in items:
                source = item.get("source", "unknown")
                freq = self._source_freq.get(source, 0)
                noise_ratio = freq / self._total_signals

                if noise_ratio < self.noise_threshold:
                    continue  # 低频噪声过滤

                confidence = item.get("confidence", 0.5)
                if confidence < self.conflict_threshold:
                    continue  # 低置信冲突过滤

                score = confidence * (1.0 - noise_ratio * 0.5)
                results.append((score, item))

            # 按分数排序, token 预算截断
            results.sort(key=lambda x: x[0], reverse=True)
            used = 0
            kept = []
            for _, item in results:
                item_tokens = len(str(item.get("content", ""))) // 4
                if used + item_tokens <= token_budget:
                    kept.append(item)
                    used += item_tokens

            return kept

    def statistics(self) -> Dict[str, Any]:
        return {
            "sources_tracked": len(self._source_freq),
            "total_signals": self._total_signals,
            "noise_threshold": self.noise_threshold,
        }


# ---------------------------------------------------------------------------
# QueryAdaptiveRouter
# ---------------------------------------------------------------------------

class QueryAdaptiveRouter:
    """查询自适应路由器——根据查询特征自动选择检索路径。"""

    def __init__(self) -> None:
        self._path_weights: Dict[QueryType, Dict[RetrievalPath, float]] = {
            QueryType.FACTUAL: {RetrievalPath.KV_LOOKUP: 0.8, RetrievalPath.SEMANTIC_SEARCH: 0.15, RetrievalPath.GRAPH_TRAVERSE: 0.05},
            QueryType.CONCEPTUAL: {RetrievalPath.KV_LOOKUP: 0.1, RetrievalPath.SEMANTIC_SEARCH: 0.8, RetrievalPath.GRAPH_TRAVERSE: 0.1},
            QueryType.RELATIONAL: {RetrievalPath.KV_LOOKUP: 0.1, RetrievalPath.SEMANTIC_SEARCH: 0.2, RetrievalPath.GRAPH_TRAVERSE: 0.7},
            QueryType.HYBRID: {RetrievalPath.KV_LOOKUP: 0.33, RetrievalPath.SEMANTIC_SEARCH: 0.34, RetrievalPath.GRAPH_TRAVERSE: 0.33},
        }
        self._lock = threading.RLock()

    def classify(self, query: str) -> QueryType:
        """根据查询文本推断查询类型。"""
        q_lower = query.lower()
        # 事实特征: 精确时间/数字/专名
        has_digits = any(c.isdigit() for c in q_lower)
        # 关系特征: 关系疑问词
        relational_kw = {"related", "dependency", "depends", "parent", "child", "link", "connect", "neighbor", "关系", "依赖", "连接"}
        has_relational = any(kw in q_lower for kw in relational_kw)

        if has_relational:
            return QueryType.RELATIONAL
        if has_digits and len(q_lower.split()) <= 6:
            return QueryType.FACTUAL
        if len(q_lower.split()) >= 6:
            return QueryType.CONCEPTUAL
        return QueryType.HYBRID

    def route(self, query: str) -> List[Tuple[RetrievalPath, float]]:
        """返回应使用的检索路径及权重。"""
        qtype = self.classify(query)
        with self._lock:
            weights = self._path_weights[qtype]
            sorted_paths = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            return [(p, w) for p, w in sorted_paths if w > 0]

    def statistics(self) -> Dict[str, Any]:
        return {"supported_query_types": [q.name for q in QueryType]}


# ---------------------------------------------------------------------------
# HierarchicalMemoryLayer
# ---------------------------------------------------------------------------

class HierarchicalMemoryLayer:
    """两层分层记忆: 基础层(原始记忆) → 抽象层(聚合后可追溯)。"""

    def __init__(self) -> None:
        # 基础层: 原始记忆 (key → value)
        self._base: Dict[str, Dict[str, Any]] = {}
        # 抽象层: 聚合节点
        self._abstract: Dict[str, AbstractMemoryNode] = {}
        self._fusion_edges: List[SemanticGraphFusion] = []
        self._lock = threading.RLock()

    def store_base(self, key: str, value: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        """存入基础层。"""
        with self._lock:
            self._base[key] = {
                "value": value, "timestamp": time.time(),
                "metadata": metadata or {},
            }

    def aggregate(
        self, keys: List[str], summary: str, abstract_id: Optional[str] = None,
    ) -> AbstractMemoryNode:
        """聚合一组基础记忆为抽象节点。"""
        with self._lock:
            aid = abstract_id or f"abs_{hashlib.md5(summary.encode()).hexdigest()[:12]}"
            node = AbstractMemoryNode(
                node_id=aid, summary=summary, source_ids=list(keys),
            )
            self._abstract[aid] = node
            return node

    def abstract_from_base(self, key: str) -> Optional[AbstractMemoryNode]:
        """从基础层 key 反查所属抽象节点。"""
        for node in self._abstract.values():
            if key in node.source_ids:
                return node
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "base_entries": len(self._base),
            "abstract_nodes": len(self._abstract),
            "fusion_edges": len(self._fusion_edges),
        }


# ---------------------------------------------------------------------------
# MandolAgglomerativeGraph
# ---------------------------------------------------------------------------

class MandolAgglomerativeGraph:
    """MANDOL 统一凝聚记忆图——融合 KV + 向量 + 图三种检索原语。"""

    def __init__(self, vector_dim: int = 128) -> None:
        self.vector_dim = vector_dim
        self.layers = HierarchicalMemoryLayer()
        self.router = QueryAdaptiveRouter()
        self.denoiser = QuantitativeDenoiser()
        # KV 检索缓存
        self._kv_store: Dict[str, Any] = {}
        # 向量索引 (简化: Dict[key, np.ndarray])
        self._vector_index: Dict[str, np.ndarray] = {}
        # 图邻接表
        self._graph_adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        self._lock = threading.RLock()

    def insert(
        self, key: str, value: Any, vector: Optional[np.ndarray] = None,
        edges: Optional[List[Tuple[str, float]]] = None, source: str = "unknown",
    ) -> None:
        """统一插入——同时更新 KV + 向量 + 图。"""
        with self._lock:
            # KV
            self._kv_store[key] = value
            # 向量
            if vector is not None and vector.shape[0] == self.vector_dim:
                self._vector_index[key] = vector.copy()
            # 图边
            if edges:
                for target, weight in edges:
                    self._graph_adj[key].append((target, weight))
            # 基础层
            self.layers.store_base(key, value, {"source": source})
            # 去噪器
            self.denoiser.observe(source)

    def retrieve(self, query: str, top_k: int = 10) -> Dict[str, List[Any]]:
        """统一检索——自动路由并融合多路径结果。

        Returns
        -------
        Dict[str, List[Any]]
            {"kv_results": [...], "semantic_results": [...], "graph_results": [...]}
        """
        paths = self.router.route(query)

        results: Dict[str, List[Any]] = {
            "kv_results": [], "semantic_results": [], "graph_results": [],
        }

        for path, _ in paths:
            if path == RetrievalPath.KV_LOOKUP:
                # 精确 key 匹配
                tokens = set(query.lower().split())
                for k, v in self._kv_store.items():
                    if any(t in k.lower() for t in tokens):
                        results["kv_results"].append({"key": k, "value": v, "match": "partial"})

            elif path == RetrievalPath.SEMANTIC_SEARCH:
                # 简化的向量余弦搜索
                query_vec = self._text_to_vec(query)
                scored = []
                for k, vec in self._vector_index.items():
                    sim = float(np.dot(query_vec, vec))
                    scored.append((sim, k))
                scored.sort(key=lambda x: x[0], reverse=True)
                for sim, k in scored[:top_k]:
                    results["semantic_results"].append({"key": k, "similarity": round(sim, 4)})

            elif path == RetrievalPath.GRAPH_TRAVERSE:
                tokens = set(query.lower().split())
                for node, neighbors in self._graph_adj.items():
                    if any(t in node.lower() for t in tokens):
                        for nbr, w in neighbors[:top_k]:
                            results["graph_results"].append({"source": node, "target": nbr, "weight": w})

        return results

    def _text_to_vec(self, text: str) -> np.ndarray:
        """文本 → 向量 (简化散列映射)。"""
        vec = np.zeros(self.vector_dim, dtype=np.float32)
        for i, ch in enumerate(text[:self.vector_dim]):
            idx = (ord(ch) * 7 + i) % self.vector_dim
            vec[idx] += 1.0 / (i + 1)
        norm = np.linalg.norm(vec)
        return (vec / norm).astype(np.float32) if norm > 0 else vec

    def statistics(self) -> Dict[str, Any]:
        return {
            "kv_entries": len(self._kv_store),
            "vector_index_size": len(self._vector_index),
            "graph_nodes": len(self._graph_adj),
            "layers": self.layers.statistics(),
            "denoiser": self.denoiser.statistics(),
            "router": self.router.statistics(),
        }
