"""
# status: orphan (2026-08-15 audit, not in runtime path)
P6-2: Neuro-Symbolic Hybrid Retrieval Engine (对标 NS-Mem)
===========================================================

实现向量相似度检索与确定性符号查询的联合打分，支持结构化推理查询
（如"所有满足条件X且关系Y的实体"），查询计划自动分解为向量子查询+
符号子查询，结果融合排序。

NS-Mem 混合检索引擎核心设计：
  - 向量检索 (Neural): 基于 embedding 的相似度搜索，适合归纳推理
  - 符号查询 (Symbolic): 确定性查询函数，适合演绎/分析推理
  - 联合打分 (Joint Scoring): 加权融合两种检索结果
  - 查询计划分解: 结构化查询自动拆分为向量+符号子查询

Reference: Jiang et al., "Advancing Multimodal Agent Reasoning with
           Long-Term Neuro-Symbolic Memory", arXiv:2603.15280, Mar 2026.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class QueryType(Enum):
    """查询类型分类。"""
    PURE_NEURAL = "pure_neural"      # 纯向量检索：语义相似度
    PURE_SYMBOLIC = "pure_symbolic"  # 纯符号查询：确定性过滤
    HYBRID = "hybrid"                # 混合查询：向量 + 符号
    STRUCTURED = "structured"        # 结构化推理查询


class SubQueryType(Enum):
    """子查询类型。"""
    VECTOR_SIMILARITY = "vector_similarity"
    SYMBOL_FILTER = "symbol_filter"
    GRAPH_TRAVERSAL = "graph_traversal"
    LOGICAL_INFERENCE = "logical_inference"


class FusionStrategy(Enum):
    """结果融合策略。"""
    WEIGHTED_SUM = "weighted_sum"          # 加权求和
    RECIPROCAL_RANK = "reciprocal_rank"    # RRF 倒数秩融合
    CASCADE = "cascade"                    # 级联：符号过滤→向量排序
    BORDA_COUNT = "borda_count"            # Borda 计数法


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class RetrievalResult:
    """单条检索结果。

    Args:
        result_id: 结果唯一标识
        content: 结果内容摘要
        neural_score: 向量相似度分数 [0,1]
        symbolic_score: 符号匹配分数 [0,1]
        combined_score: 联合分数 [0,1]
        source: 数据来源标识
        metadata: 额外元数据
    """
    result_id: str = field(default_factory=lambda: f"ret_{uuid.uuid4().hex[:12]}")
    content: str = ""
    neural_score: float = 0.0
    symbolic_score: float = 0.0
    combined_score: float = 0.0
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SubQueryPlan:
    """子查询计划。

    Args:
        sub_query_id: 子查询ID
        sub_type: 子查询类型
        query_text: 查询文本
        filters: 符号过滤条件 {field: value}
        weight: 融合权重
    """
    sub_query_id: str = field(default_factory=lambda: f"sq_{uuid.uuid4().hex[:12]}")
    sub_type: SubQueryType = SubQueryType.VECTOR_SIMILARITY
    query_text: str = ""
    filters: Dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0


@dataclass
class QueryPlan:
    """完整的查询计划。

    Args:
        plan_id: 计划ID
        original_query: 原始查询文本
        query_type: 查询类型
        sub_queries: 子查询列表
        fusion_strategy: 融合策略
        neural_weight: 向量检索权重
        symbolic_weight: 符号检索权重
    """
    plan_id: str = field(default_factory=lambda: f"qp_{uuid.uuid4().hex[:12]}")
    original_query: str = ""
    query_type: QueryType = QueryType.HYBRID
    sub_queries: List[SubQueryPlan] = field(default_factory=list)
    fusion_strategy: FusionStrategy = FusionStrategy.WEIGHTED_SUM
    neural_weight: float = 0.5
    symbolic_weight: float = 0.5


@dataclass
class RetrievalReport:
    """检索执行报告。

    Args:
        report_id: 报告ID
        plan: 执行的查询计划
        results: 融合后的结果列表
        elapsed_ms: 耗时（毫秒）
        neural_hits: 向量检索命中数
        symbolic_hits: 符号检索命中数
        merged_hits: 融合后结果数
    """
    report_id: str = field(default_factory=lambda: f"rpt_{uuid.uuid4().hex[:12]}")
    plan: Optional[QueryPlan] = None
    results: List[RetrievalResult] = field(default_factory=list)
    elapsed_ms: float = 0.0
    neural_hits: int = 0
    symbolic_hits: int = 0
    merged_hits: int = 0


# ── 混合检索引擎 ─────────────────────────────────────────────────────

class NeuroSymbolicRetriever:
    """NS-Mem 风格的神经-符号混合检索引擎。

    核心功能：
    1. 向量相似度检索（neural）—— 基于余弦相似度的语义匹配
    2. 确定性符号查询（symbolic）—— 基于字段值的精确过滤
    3. 联合打分 —— 加权或 RRF 融合
    4. 查询计划自动分解 —— 结构化查询 → 子查询

    设计要点：
    - 向量存储：内存中的 embedding 索引（简化版）
    - 符号索引：基于字典的倒排索引
    - 线程安全：RLock 保护共享状态
    - 统计追踪：完整的运行时指标
    """

    def __init__(
        self,
        default_fusion: FusionStrategy = FusionStrategy.WEIGHTED_SUM,
        neural_weight: float = 0.5,
        symbolic_weight: float = 0.5,
        embedding_dim: int = 128,
    ):
        self.default_fusion = default_fusion
        self.neural_weight = neural_weight
        self.symbolic_weight = symbolic_weight
        self.embedding_dim = embedding_dim

        # 向量索引：{doc_id: np.ndarray(embedding)}
        self._vector_index: Dict[str, np.ndarray] = {}
        # 文档存储：{doc_id: {field: value}}
        self._doc_store: Dict[str, Dict[str, Any]] = {}
        # 符号倒排索引：{field: {value: [doc_ids]}}
        self._symbol_index: Dict[str, Dict[Any, List[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        # 内容缓存
        self._content_cache: Dict[str, str] = {}

        self._lock = threading.RLock()

        self._stats: Dict[str, int] = {
            "total_docs_indexed": 0,
            "total_queries": 0,
            "total_plans_decomposed": 0,
            "neural_retrievals": 0,
            "symbolic_retrievals": 0,
            "hybrid_retrievals": 0,
        }

    # ── 文档索引 ─────────────────────────────────────────────────

    def index_document(
        self,
        doc_id: str,
        content: str,
        fields: Optional[Dict[str, Any]] = None,
        embedding: Optional[np.ndarray] = None,
    ) -> None:
        """索引文档：存储内容、字段和向量。

        Args:
            doc_id: 文档唯一标识
            content: 文档文本内容
            fields: 可过滤的元数据字段
            embedding: 预计算向量（None 时自动生成）
        """
        with self._lock:
            # 生成 embedding
            if embedding is None:
                embedding = self._hash_embedding(content)

            self._vector_index[doc_id] = embedding
            self._doc_store[doc_id] = fields or {}
            self._content_cache[doc_id] = content

            # 更新符号倒排索引
            if fields:
                for field, value in fields.items():
                    if isinstance(value, (list, tuple)):
                        for v in value:
                            self._symbol_index[field][v].append(doc_id)
                    else:
                        self._symbol_index[field][value].append(doc_id)

            self._stats["total_docs_indexed"] += 1

    def batch_index(
        self,
        documents: List[Dict[str, Any]],
    ) -> int:
        """批量索引文档。

        Args:
            documents: [{doc_id, content, fields, embedding}, ...]

        Returns:
            成功索引数量
        """
        count = 0
        for doc in documents:
            try:
                self.index_document(
                    doc_id=doc.get("doc_id", str(uuid.uuid4())),
                    content=doc.get("content", ""),
                    fields=doc.get("fields"),
                    embedding=doc.get("embedding"),
                )
                count += 1
            except Exception as exc:
                logger.warning("NeuroSymbolicRetriever: failed to index %s: %s", doc.get("doc_id"), exc)
        return count

    # ── 查询计划分解 ─────────────────────────────────────────────

    def decompose_query(self, query: str) -> QueryPlan:
        """将结构化查询自动分解为向量子查询+符号子查询。

        识别查询中的结构化模式：
        - "所有满足条件X且关系Y的实体" → 符号过滤 + 语义匹配
        - "类似X但不同Y的" → 向量相似 + 符号排除

        Args:
            query: 原始查询字符串

        Returns:
            分解后的查询计划
        """
        self._stats["total_plans_decomposed"] += 1
        query_lower = query.lower()
        plan = QueryPlan(original_query=query)
        plan.neural_weight = self.neural_weight
        plan.symbolic_weight = self.symbolic_weight

        # 检测结构化模式
        has_filter_markers = any(
            marker in query_lower
            for marker in ["满足", "具有", "属于", "大于", "小于", "等于", "包含",
                           "filter", "where", "satisfying", "having", "category",
                           "type", "属性", "条件"]
        )
        has_similarity_markers = any(
            marker in query_lower
            for marker in ["类似", "相似", "相关", "like", "similar", "relevant",
                           "about", "regarding", "related to"]
        )

        if has_filter_markers and has_similarity_markers:
            plan.query_type = QueryType.HYBRID
            # 拆分为向量子查询和符号子查询
            plan.sub_queries = [
                SubQueryPlan(
                    sub_type=SubQueryType.VECTOR_SIMILARITY,
                    query_text=query,
                    weight=0.5,
                ),
                SubQueryPlan(
                    sub_type=SubQueryType.SYMBOL_FILTER,
                    query_text=query,
                    weight=0.5,
                ),
            ]
        elif has_filter_markers:
            plan.query_type = QueryType.STRUCTURED
            plan.sub_queries = [
                SubQueryPlan(
                    sub_type=SubQueryType.SYMBOL_FILTER,
                    query_text=query,
                    weight=1.0,
                ),
            ]
        elif has_similarity_markers:
            plan.query_type = QueryType.PURE_NEURAL
            plan.sub_queries = [
                SubQueryPlan(
                    sub_type=SubQueryType.VECTOR_SIMILARITY,
                    query_text=query,
                    weight=1.0,
                ),
            ]
        else:
            # 默认混合
            plan.query_type = QueryType.HYBRID
            plan.sub_queries = [
                SubQueryPlan(
                    sub_type=SubQueryType.VECTOR_SIMILARITY,
                    query_text=query,
                    weight=0.6,
                ),
                SubQueryPlan(
                    sub_type=SubQueryType.SYMBOL_FILTER,
                    query_text=query,
                    weight=0.4,
                ),
            ]

        return plan

    # ── 检索执行 ─────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        plan: Optional[QueryPlan] = None,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> RetrievalReport:
        """执行混合检索。

        Args:
            query: 查询字符串
            plan: 预分解的查询计划（None 时自动分解）
            top_k: 返回结果数
            filters: 额外符号过滤条件

        Returns:
            RetrievalReport 检索报告
        """
        self._stats["total_queries"] += 1
        start = time.perf_counter()

        if plan is None:
            plan = self.decompose_query(query)

        all_results: List[RetrievalResult] = []
        neural_hits = 0
        symbolic_hits = 0

        with self._lock:
            for sq in plan.sub_queries:
                if sq.sub_type == SubQueryType.VECTOR_SIMILARITY:
                    results = self._neural_retrieve(sq.query_text, top_k * 2)
                    neural_hits = len(results)
                    all_results.extend(results)
                    self._stats["neural_retrievals"] += 1
                elif sq.sub_type == SubQueryType.SYMBOL_FILTER:
                    all_filters = {**sq.filters, **(filters or {})}
                    results = self._symbolic_retrieve(all_filters, top_k * 2)
                    symbolic_hits = len(results)
                    all_results.extend(results)
                    self._stats["symbolic_retrievals"] += 1

        # 去重 + 融合打分
        merged = self._fuse_results(
            all_results, plan.fusion_strategy or self.default_fusion,
            neural_weight=plan.neural_weight,
            symbolic_weight=plan.symbolic_weight,
        )
        merged = merged[:top_k]

        elapsed = (time.perf_counter() - start) * 1000.0
        self._stats["hybrid_retrievals"] += 1

        return RetrievalReport(
            plan=plan,
            results=merged,
            elapsed_ms=elapsed,
            neural_hits=neural_hits,
            symbolic_hits=symbolic_hits,
            merged_hits=len(merged),
        )

    def retrieve_simple(
        self, query: str, top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        """简化检索接口：直接返回结果列表。"""
        report = self.retrieve(query, top_k=top_k, filters=filters)
        return report.results

    # ── 向量检索 ─────────────────────────────────────────────────

    def _neural_retrieve(
        self, query: str, top_k: int = 10,
    ) -> List[RetrievalResult]:
        """基于余弦相似度的向量检索。"""
        query_emb = self._hash_embedding(query)
        results: List[RetrievalResult] = []

        for doc_id, doc_emb in self._vector_index.items():
            sim = self._cosine_similarity(query_emb, doc_emb)
            if sim > 0:
                content = self._content_cache.get(doc_id, "")
                results.append(RetrievalResult(
                    content=content,
                    neural_score=float(sim),
                    source=doc_id,
                    metadata=self._doc_store.get(doc_id, {}),
                ))

        results.sort(key=lambda r: r.neural_score, reverse=True)
        return results[:top_k]

    # ── 符号检索 ─────────────────────────────────────────────────

    def _symbolic_retrieve(
        self, filters: Dict[str, Any], top_k: int = 10,
    ) -> List[RetrievalResult]:
        """基于确定性符号条件的精确过滤。

        支持：
        - 等值匹配：field == value
        - 列表匹配：field ∈ [values]
        - 范围匹配（字符串前缀）：field.startswith(prefix)
        """
        if not filters:
            return []

        # 对每个过滤条件取交集
        candidate_sets: List[Set[str]] = []
        for field, value in filters.items():
            if isinstance(value, (list, tuple)):
                field_candidates: Set[str] = set()
                for v in value:
                    field_candidates.update(self._symbol_index.get(field, {}).get(v, []))
                candidate_sets.append(field_candidates)
            elif isinstance(value, str) and value.endswith("*"):
                # 前缀匹配
                prefix = value[:-1].lower()
                field_candidates: Set[str] = set()
                field_index = self._symbol_index.get(field, {})
                for k, doc_ids in field_index.items():
                    if str(k).lower().startswith(prefix):
                        field_candidates.update(doc_ids)
                candidate_sets.append(field_candidates)
            else:
                doc_ids = self._symbol_index.get(field, {}).get(value, [])
                candidate_sets.append(set(doc_ids))

        if not candidate_sets:
            return []

        matched_ids = candidate_sets[0]
        for cs in candidate_sets[1:]:
            matched_ids = matched_ids & cs

        results: List[RetrievalResult] = []
        for doc_id in list(matched_ids)[:top_k]:
            content = self._content_cache.get(doc_id, "")
            score = 1.0 / max(1, len(candidate_sets))
            results.append(RetrievalResult(
                content=content,
                symbolic_score=float(score),
                source=doc_id,
                metadata=self._doc_store.get(doc_id, {}),
            ))

        return results

    # ── 结果融合 ─────────────────────────────────────────────────

    def _fuse_results(
        self,
        results: List[RetrievalResult],
        strategy: FusionStrategy,
        neural_weight: float = 0.5,
        symbolic_weight: float = 0.5,
    ) -> List[RetrievalResult]:
        """多策略结果融合与去重。"""
        # 按 source 去重并合并分数
        merged: Dict[str, RetrievalResult] = {}
        for r in results:
            if r.source in merged:
                existing = merged[r.source]
                existing.neural_score = max(existing.neural_score, r.neural_score)
                existing.symbolic_score = max(existing.symbolic_score, r.symbolic_score)
            else:
                merged[r.source] = RetrievalResult(
                    content=r.content,
                    neural_score=r.neural_score,
                    symbolic_score=r.symbolic_score,
                    source=r.source,
                    metadata=r.metadata,
                )

        for r in merged.values():
            if strategy == FusionStrategy.WEIGHTED_SUM:
                r.combined_score = (
                    neural_weight * r.neural_score
                    + symbolic_weight * r.symbolic_score
                )
            elif strategy == FusionStrategy.CASCADE:
                # 级联：符号过滤优先，向量排序在后
                r.combined_score = r.symbolic_score if r.symbolic_score > 0 else r.neural_score * 0.5
            else:
                r.combined_score = (r.neural_score + r.symbolic_score) / 2.0

        sorted_results = sorted(
            merged.values(), key=lambda r: r.combined_score, reverse=True
        )
        return sorted_results

    # ── 辅助方法 ─────────────────────────────────────────────────

    def _hash_embedding(self, text: str) -> np.ndarray:
        """确定性哈希 embedding（简化版，生产环境可用真实模型替换）。"""
        h = hashlib.sha256(text.encode("utf-8")).digest()
        raw = np.array([b / 255.0 for b in h], dtype=float)
        # 扩展到目标维度
        emb = np.zeros(self.embedding_dim)
        repeat = (self.embedding_dim + 31) // 32
        for i in range(repeat):
            start = 0
            end = min(32, self.embedding_dim - i * 32)
            emb[i * 32 : i * 32 + end] = raw[:end]
        # L2 归一化
        norm = np.linalg.norm(emb)
        if norm > 1e-10:
            emb = emb / norm
        return emb

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """计算余弦相似度。"""
        dot = float(np.dot(a, b))
        norm_a = float(np.linalg.norm(a))
        norm_b = float(np.linalg.norm(b))
        if norm_a < 1e-10 or norm_b < 1e-10:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    # ── 统计与诊断 ───────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            vector_size = len(self._vector_index)
            symbol_fields = list(self._symbol_index.keys())
            total_symbol_entries = sum(
                len(values) for field_index in self._symbol_index.values()
                for values in field_index.values()
            )
            return {
                "total_docs_indexed": self._stats["total_docs_indexed"],
                "vector_index_size": vector_size,
                "symbol_index_fields": len(symbol_fields),
                "symbol_index_total_entries": total_symbol_entries,
                "total_queries": self._stats["total_queries"],
                "total_plans_decomposed": self._stats["total_plans_decomposed"],
                "neural_retrievals": self._stats["neural_retrievals"],
                "symbolic_retrievals": self._stats["symbolic_retrievals"],
                "hybrid_retrievals": self._stats["hybrid_retrievals"],
                "embedding_dim": self.embedding_dim,
                "default_fusion": self.default_fusion.value,
            }

    def reset(self) -> None:
        """重置所有索引和状态。"""
        with self._lock:
            self._vector_index.clear()
            self._doc_store.clear()
            self._symbol_index.clear()
            self._content_cache.clear()
            for k in self._stats:
                self._stats[k] = 0
