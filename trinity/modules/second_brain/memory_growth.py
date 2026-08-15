"""
# status: orphan (2026-08-15 audit, not in runtime path)
P7-2: Token-Level Reasoning-Aligned Memory Growth (对标 MemSearch-o1 ACL2026)
==============================================================================

核心设计（基于 MemSearch-o1 ACL 2026）：
  - Seed Token 提取：从检索查询中提取记忆种子 Token
  - Token 级记忆片段生长：动态生长为细粒度记忆片段（memory fragments）
  - 贡献函数回溯精炼（Contribution Function）：通过贡献函数回溯并深度精炼
  - 全局连接记忆路径重组：最终重组为全局连接的记忆路径（memory path）

关键转变：
  流式追加 + 摘要模式  →  结构化 Token 级生长 + 路径推理

核心机制：
  1. 种子 Token → 记忆片段生长 (Grow)
  2. 贡献函数 → 回溯精炼 (Retrace & Refine)
  3. 跨片段连接 → 全局路径重组 (Reorganize)

Reference: Zhang et al., "MemSearch-o1: Empowering Large Language Models
           with Reasoning-Aligned Memory Growth in Agentic Search",
           ACL 2026, pp. 925-943.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ────────────────────────────────────────────────────────


class GrowthMode(Enum):
    """记忆生长模式。"""
    INCREMENTAL = "incremental"      # 增量生长：逐 Token 追加
    BURST = "burst"                  # 爆发生长：批量 Token 一次生长
    ADAPTIVE = "adaptive"            # 自适应：根据查询复杂度切换


class FragmentType(Enum):
    """记忆片段类型。"""
    FACT = "fact"                    # 事实片段
    CONCEPT = "concept"              # 概念片段
    RELATION = "relation"            # 关系片段
    PROCEDURE = "procedure"          # 过程片段
    QUERY_BRIDGE = "query_bridge"    # 查询桥接片段
    COUNTERFACTUAL = "counterfactual"  # 反事实片段


class ContributionMetric(Enum):
    """贡献度量方式。"""
    SEMANTIC_SIMILARITY = "semantic_similarity"       # 语义相似度
    TOKEN_OVERLAP = "token_overlap"                    # Token 重叠率
    REASONING_ALIGNMENT = "reasoning_alignment"         # 推理对齐度
    MIXED = "mixed"                                     # 混合度量


class PathType(Enum):
    """记忆路径类型。"""
    LINEAR = "linear"                # 线性路径
    BRANCHING = "branching"         # 分支路径
    CYCLIC = "cyclic"               # 环状路径
    MESH = "mesh"                   # 网状路径


# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass
class SeedToken:
    """从查询中提取的记忆种子 Token。

    Args:
        token_id: 唯一标识
        token_text: Token 文本
        position: 在查询中的位置
        importance: 重要性权重 [0,1]
        semantic_type: 语义类型（entity/action/attribute）
        embedding: 向量表示（可选）
    """
    token_id: str = field(default_factory=lambda: f"stok_{uuid.uuid4().hex[:10]}")
    token_text: str = ""
    position: int = 0
    importance: float = 0.5
    semantic_type: str = "entity"
    embedding: Optional[List[float]] = None


@dataclass
class MemoryFragment:
    """Token 级细粒度记忆片段。

    Args:
        fragment_id: 唯一标识
        seed_tokens: 来源种子 Token ID 列表
        content: 片段文本内容
        fragment_type: 片段类型
        confidence: 置信度 [0,1]
        contribution_score: 贡献分数（贡献函数输出）
        parent_fragment_ids: 父片段 ID 列表
        child_fragment_ids: 子片段 ID 列表
        tokens_consumed: 消耗的 Token 数
        created_at: 创建时间
        refined_at: 最近精炼时间
        refinement_count: 精炼次数
    """
    fragment_id: str = field(
        default_factory=lambda: f"mfrg_{uuid.uuid4().hex[:12]}"
    )
    seed_tokens: List[str] = field(default_factory=list)
    content: str = ""
    fragment_type: FragmentType = FragmentType.FACT
    confidence: float = 0.5
    contribution_score: float = 0.0
    parent_fragment_ids: List[str] = field(default_factory=list)
    child_fragment_ids: List[str] = field(default_factory=list)
    tokens_consumed: int = 0
    created_at: float = field(default_factory=time.time)
    refined_at: Optional[float] = None
    refinement_count: int = 0


@dataclass
class MemoryPath:
    """全局连接的记忆路径。

    Args:
        path_id: 唯一标识
        fragments: 有序片段 ID 列表
        path_type: 路径类型
        entry_query: 入口查询
        total_confidence: 整体置信度
        reasoning_chain: 推理链（各步骤描述）
        branch_points: 分支点索引列表
        created_at: 创建时间
        access_count: 访问次数
    """
    path_id: str = field(default_factory=lambda: f"mpath_{uuid.uuid4().hex[:12]}")
    fragments: List[str] = field(default_factory=list)
    path_type: PathType = PathType.LINEAR
    entry_query: str = ""
    total_confidence: float = 0.5
    reasoning_chain: List[str] = field(default_factory=list)
    branch_points: List[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    access_count: int = 0


@dataclass
class ContributionTrace:
    """贡献函数回溯记录。

    Args:
        trace_id: 唯一标识
        fragment_id: 关联片段ID
        scores: Token 级贡献分数列表
        aggregated_score: 聚合贡献分数
        retention_decision: 保留决策
        trace_timestamp: 记录时间
    """
    trace_id: str = field(default_factory=lambda: f"ctr_{uuid.uuid4().hex[:10]}")
    fragment_id: str = ""
    scores: List[float] = field(default_factory=list)
    aggregated_score: float = 0.0
    retention_decision: bool = True
    trace_timestamp: float = field(default_factory=time.time)


@dataclass
class GrowthStats:
    """记忆生长统计快照。"""
    total_fragments: int = 0
    active_fragments: int = 0
    pruned_fragments: int = 0
    total_paths: int = 0
    total_seed_tokens: int = 0
    total_contributions_evaluated: int = 0
    avg_fragment_confidence: float = 0.0
    avg_path_length: float = 0.0
    refinement_count: int = 0


# ── 贡献函数 ──────────────────────────────────────────────────────────


class ContributionFunction:
    """贡献函数：回溯评估每个记忆片段对查询的贡献。

    实现三种度量并支持混合：
    1. 语义相似度（embedding-based）
    2. Token 重叠率（Jaccard）
    3. 推理对齐度（reasoning alignment）

    输出贡献分数用于保留/精炼/丢弃决策。
    """

    def __init__(
        self,
        metric: ContributionMetric = ContributionMetric.MIXED,
        sim_weight: float = 0.5,
        overlap_weight: float = 0.3,
        reasoning_weight: float = 0.2,
        retention_threshold: float = 0.15,
    ):
        self.metric = metric
        self.sim_weight = sim_weight
        self.overlap_weight = overlap_weight
        self.reasoning_weight = reasoning_weight
        self.retention_threshold = retention_threshold

        self._evaluations: int = 0
        self._lock = threading.RLock()

    def evaluate(
        self,
        fragment: MemoryFragment,
        query_tokens: List[str],
        query_embedding: Optional[List[float]] = None,
        reasoning_context: Optional[str] = None,
    ) -> ContributionTrace:
        """评估一个记忆片段对查询的贡献。

        Args:
            fragment: 记忆片段
            query_tokens: 查询 Token 列表
            query_embedding: 查询向量（可选）
            reasoning_context: 推理上下文（可选）

        Returns:
            ContributionTrace
        """
        with self._lock:
            self._evaluations += 1

        scores: List[float] = []
        content_lower = fragment.content.lower()

        # 1. Token 重叠率
        if self.metric in (
            ContributionMetric.TOKEN_OVERLAP,
            ContributionMetric.MIXED,
        ):
            fragment_tokens = set(content_lower.split())
            query_set = {t.lower() for t in query_tokens}
            intersection = fragment_tokens & query_set
            union = fragment_tokens | query_set
            overlap_score = (
                len(intersection) / max(len(union), 1)
                if union else 0.0
            )
            scores.append(overlap_score)

        # 2. 语义相似度（简化：基于种子 Token 匹配）
        if self.metric in (
            ContributionMetric.SEMANTIC_SIMILARITY,
            ContributionMetric.MIXED,
        ):
            seed_texts = {st.lower() for st in fragment.seed_tokens}
            query_set_lower = {t.lower() for t in query_tokens}
            shared = seed_texts & query_set_lower
            sim_score = (
                len(shared) / max(len(seed_texts | query_set_lower), 1)
                if seed_texts or query_set_lower else 0.0
            )
            scores.append(sim_score)

        # 3. 推理对齐度
        if self.metric in (
            ContributionMetric.REASONING_ALIGNMENT,
            ContributionMetric.MIXED,
        ):
            reasoning_score = 0.0
            if reasoning_context:
                for token in query_tokens:
                    if token.lower() in reasoning_context.lower():
                        reasoning_score += 0.05
                reasoning_score = min(reasoning_score, 1.0)
            scores.append(reasoning_score)

        # 聚合
        if self.metric == ContributionMetric.MIXED:
            aggregated = (
                scores[0] * self.overlap_weight
                + scores[1] * self.sim_weight
                + (scores[2] if len(scores) > 2 else 0) * self.reasoning_weight
            )
        else:
            aggregated = scores[0] if scores else 0.0

        retention = aggregated >= self.retention_threshold

        return ContributionTrace(
            fragment_id=fragment.fragment_id,
            scores=scores,
            aggregated_score=round(aggregated, 6),
            retention_decision=retention,
        )

    def batch_evaluate(
        self,
        fragments: List[MemoryFragment],
        query_tokens: List[str],
        query_embedding: Optional[List[float]] = None,
        reasoning_context: Optional[str] = None,
    ) -> List[ContributionTrace]:
        """批量评估片段贡献。"""
        traces = []
        for frag in fragments:
            traces.append(
                self.evaluate(
                    frag, query_tokens, query_embedding, reasoning_context
                )
            )
        return traces

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_evaluations": self._evaluations,
                "metric": self.metric.value,
                "retention_threshold": self.retention_threshold,
                "weights": {
                    "similarity": self.sim_weight,
                    "overlap": self.overlap_weight,
                    "reasoning": self.reasoning_weight,
                },
            }


# ── 种子 Token 提取器 ─────────────────────────────────────────────────


class SeedTokenExtractor:
    """从查询中提取记忆种子 Token。

    对查询进行分词，按重要性排序，提取核心种子 Token。
    """

    def __init__(self, max_seeds: int = 20, min_token_length: int = 2):
        self.max_seeds = max_seeds
        self.min_token_length = min_token_length

    def extract(self, query: str, query_embedding: Optional[List[float]] = None) -> List[SeedToken]:
        """从查询提取种子 Token 列表。

        Args:
            query: 原始查询文本
            query_embedding: 查询向量（可选）

        Returns:
            SeedToken 列表（按重要性降序）
        """
        import re

        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query)
        if not tokens:
            return []

        seeds: List[SeedToken] = []

        for idx, token in enumerate(tokens):
            if len(token) < self.min_token_length:
                continue

            # 重要性估计：基于位置（靠前更重要）和长度
            position_factor = 1.0 - min(idx / max(len(tokens), 1), 0.9)
            length_factor = min(len(token) / 10.0, 1.0)
            importance = 0.4 * position_factor + 0.3 * length_factor + 0.3 * 0.5

            semantic_type = self._classify_token(token)

            seeds.append(
                SeedToken(
                    token_text=token,
                    position=idx,
                    importance=round(min(importance, 1.0), 4),
                    semantic_type=semantic_type,
                )
            )

        seeds.sort(key=lambda s: s.importance, reverse=True)
        return seeds[:self.max_seeds]

    @staticmethod
    def _classify_token(token: str) -> str:
        """启发式 Token 语义类型分类。"""
        lower = token.lower()
        action_keywords = {
            "get", "find", "search", "query", "retrieve", "fetch", "load",
            "create", "make", "build", "generate", "produce",
            "update", "modify", "change", "edit", "patch",
            "delete", "remove", "drop", "clear",
            "compute", "calculate", "evaluate", "analyze",
        }
        entity_suffixes = ("_id", "_type", "_name", "_key")

        if any(token.endswith(s) for s in entity_suffixes):
            return "attribute"
        if lower in action_keywords:
            return "action"
        return "entity"


# ── 令牌级记忆生长引擎 ────────────────────────────────────────────────


class MemoryGrowthEngine:
    """MemSearch-o1 风格的 Token 级推理对齐记忆生长引擎。

    三步流程：
    1. Grow:   从种子 Token 生长为记忆片段
    2. Retrace: 贡献函数回溯精炼
    3. Reorganize: 重组为全局连接记忆路径
    """

    def __init__(
        self,
        max_fragments: int = 1000,
        max_paths: int = 200,
        growth_mode: GrowthMode = GrowthMode.ADAPTIVE,
        contribution_metric: ContributionMetric = ContributionMetric.MIXED,
        retention_threshold: float = 0.15,
        auto_prune: bool = True,
    ):
        self.max_fragments = max_fragments
        self.max_paths = max_paths
        self.growth_mode = growth_mode
        self.auto_prune = auto_prune

        # 子组件
        self.seed_extractor = SeedTokenExtractor()
        self.contribution_fn = ContributionFunction(
            metric=contribution_metric,
            retention_threshold=retention_threshold,
        )

        # 存储
        self._fragments: Dict[str, MemoryFragment] = {}
        self._paths: Dict[str, MemoryPath] = {}
        self._contribution_traces: deque = deque(maxlen=1000)

        self._lock = threading.RLock()

        logger.info(
            "MemoryGrowthEngine initialized (max_fragments=%d, "
            "max_paths=%d, mode=%s)",
            max_fragments,
            max_paths,
            growth_mode.value,
        )

    # ── 阶段 1: Growing (种子 → 片段) ──────────────────────────────

    def grow_from_query(
        self,
        query: str,
        retrieved_content: Optional[List[Dict[str, Any]]] = None,
        query_embedding: Optional[List[float]] = None,
    ) -> List[MemoryFragment]:
        """从查询生长记忆片段。

        Args:
            query: 原始查询
            retrieved_content: 检索到的内容（可选）
            query_embedding: 查询向量

        Returns:
            新生长的记忆片段列表
        """
        # Step 1: 提取种子 Token
        seeds = self.seed_extractor.extract(query, query_embedding)
        if not seeds:
            return []

        # Step 2: 从种子 Token 生长片段
        new_fragments: List[MemoryFragment] = []
        with self._lock:
            for seed in seeds:
                # 基于种子 Token 和检索内容构建片段
                content_parts = [f"Seed '{seed.token_text}' (type={seed.semantic_type})"]

                if retrieved_content:
                    for doc in retrieved_content[:3]:
                        doc_text = doc.get("content", doc.get("snippet", ""))
                        if seed.token_text.lower() in doc_text.lower():
                            content_parts.append(
                                f"...{doc_text[:200]}..."
                            )
                            break

                content = " | ".join(content_parts)
                fragment = MemoryFragment(
                    seed_tokens=[seed.token_text],
                    content=content,
                    fragment_type=FragmentType.FACT,
                    confidence=seed.importance,
                    tokens_consumed=len(content.split()),
                )

                self._fragments[fragment.fragment_id] = fragment
                new_fragments.append(fragment)

        logger.debug(
            "Grew %d fragments from %d seed tokens (query: '%s...')",
            len(new_fragments),
            len(seeds),
            query[:60],
        )
        return new_fragments

    # ── 阶段 2: Retracing (贡献函数精炼) ───────────────────────────

    def retrace_and_refine(
        self,
        fragments: List[MemoryFragment],
        query: str,
        reasoning_context: Optional[str] = None,
    ) -> Tuple[List[MemoryFragment], List[MemoryFragment]]:
        """回溯精炼：贡献函数评估，决定保留/丢弃。

        Args:
            fragments: 待评估片段
            query: 原始查询
            reasoning_context: 推理上下文

        Returns:
            (保留片段列表, 丢弃片段列表)
        """
        query_tokens = query.split()
        traces = self.contribution_fn.batch_evaluate(
            fragments, query_tokens, reasoning_context=reasoning_context
        )

        retained: List[MemoryFragment] = []
        discarded: List[MemoryFragment] = []

        with self._lock:
            for trace in traces:
                frag = self._fragments.get(trace.fragment_id)
                if frag is None:
                    continue

                frag.contribution_score = trace.aggregated_score
                frag.refined_at = time.time()
                frag.refinement_count += 1

                self._contribution_traces.append(trace)

                if trace.retention_decision:
                    retained.append(frag)
                else:
                    discarded.append(frag)
                    if self.auto_prune:
                        del self._fragments[trace.fragment_id]

        logger.info(
            "Retrace: %d retained, %d discarded (threshold=%.2f)",
            len(retained),
            len(discarded),
            self.contribution_fn.retention_threshold,
        )
        return retained, discarded

    # ── 阶段 3: Reorganize (全局路径重组) ──────────────────────────

    def reorganize_paths(
        self,
        fragments: List[MemoryFragment],
        query: str,
    ) -> List[MemoryPath]:
        """将精炼后的片段重组为全局连接记忆路径。

        Args:
            fragments: 精炼后的片段列表
            query: 入口查询

        Returns:
            重组后的记忆路径列表
        """
        if not fragments:
            return []

        # 按种子 Token 分组建立连接
        seed_groups: Dict[str, List[MemoryFragment]] = defaultdict(list)
        for frag in fragments:
            for seed_tok in frag.seed_tokens:
                seed_groups[seed_tok].append(frag)

        paths: List[MemoryPath] = []

        with self._lock:
            # 路径类型判断
            if len(fragments) <= 2:
                path_type = PathType.LINEAR
            elif any(
                len(seed_groups[st]) > 1
                for st in seed_groups
            ):
                path_type = PathType.BRANCHING
            else:
                path_type = PathType.MESH

            # 构建路径
            path = MemoryPath(
                fragments=[f.fragment_id for f in fragments],
                path_type=path_type,
                entry_query=query,
                total_confidence=round(
                    np.mean([f.confidence for f in fragments]), 4
                ),
                reasoning_chain=self._build_reasoning_chain(fragments),
                branch_points=list(range(len(fragments))),
            )
            self._paths[path.path_id] = path
            paths.append(path)

            # 更新片段间的父子关系
            for i in range(len(fragments) - 1):
                fragments[i].child_fragment_ids.append(
                    fragments[i + 1].fragment_id
                )
                fragments[i + 1].parent_fragment_ids.append(
                    fragments[i].fragment_id
                )

            # 路径数限制
            while len(self._paths) > self.max_paths:
                oldest = min(
                    self._paths.keys(),
                    key=lambda k: self._paths[k].created_at,
                    default=None,
                )
                if oldest:
                    del self._paths[oldest]

        return paths

    # ── 完整流水线 ─────────────────────────────────────────────────

    def process_query(
        self,
        query: str,
        retrieved_content: Optional[List[Dict[str, Any]]] = None,
        reasoning_context: Optional[str] = None,
    ) -> Tuple[List[MemoryFragment], List[MemoryPath]]:
        """三步完整流水线：Grow → Retrace → Reorganize。

        Args:
            query: 原始查询
            retrieved_content: 检索到的内容
            reasoning_context: 推理上下文

        Returns:
            (保留的记忆片段列表, 生成的记忆路径列表)
        """
        # Stage 1: Grow
        fragments = self.grow_from_query(query, retrieved_content)

        # Stage 2: Retrace
        retained, _ = self.retrace_and_refine(
            fragments, query, reasoning_context
        )

        # Stage 3: Reorganize
        paths = self.reorganize_paths(retained, query)

        return retained, paths

    # ── 查询已有路径 ───────────────────────────────────────────────

    def find_paths(
        self,
        query_tokens: Optional[List[str]] = None,
        min_confidence: float = 0.0,
        top_k: int = 10,
    ) -> List[MemoryPath]:
        """查找与查询 Token 匹配的记忆路径。

        Args:
            query_tokens: 查询 Token 列表
            min_confidence: 最低置信度
            top_k: 返回数

        Returns:
            记忆路径列表
        """
        with self._lock:
            candidates = list(self._paths.values())
            if query_tokens:
                query_set = {t.lower() for t in query_tokens}
                scored: List[Tuple[MemoryPath, float]] = []
                for path in candidates:
                    path_text = " ".join(
                        self._fragments[fid].content.lower()
                        for fid in path.fragments
                        if fid in self._fragments
                    )
                    tokens_in_path = set(path_text.split())
                    score = (
                        len(query_set & tokens_in_path)
                        / max(len(query_set), 1)
                    )
                    scored.append((path, score))
                scored.sort(key=lambda x: -x[1])
                candidates = [p for p, _ in scored[:top_k]]

            return [
                p
                for p in candidates[:top_k]
                if p.total_confidence >= min_confidence
            ]

    def get_fragment(self, fragment_id: str) -> Optional[MemoryFragment]:
        return self._fragments.get(fragment_id)

    def get_path(self, path_id: str) -> Optional[MemoryPath]:
        return self._paths.get(path_id)

    # ── 推理链构建 ─────────────────────────────────────────────────

    @staticmethod
    def _build_reasoning_chain(
        fragments: List[MemoryFragment],
    ) -> List[str]:
        """基于片段构建推理链描述。"""
        chain = []
        for i, frag in enumerate(fragments):
            step = (
                f"Step {i + 1}: [{frag.fragment_type.value}] "
                f"{frag.content[:100]} "
                f"(conf={frag.confidence:.2f}, contrib={frag.contribution_score:.4f})"
            )
            chain.append(step)
        return chain

    # ── 修剪 ──────────────────────────────────────────────────────

    def prune_low_contribution(self, threshold: Optional[float] = None) -> int:
        """按贡献分数修剪低价值片段。"""
        th = threshold or self.contribution_fn.retention_threshold
        removed = 0
        with self._lock:
            low_ids = [
                fid
                for fid, frag in self._fragments.items()
                if frag.contribution_score < th and frag.refinement_count > 0
            ]
            for fid in low_ids:
                del self._fragments[fid]
                removed += 1
        logger.info("Pruned %d low-contribution fragments", removed)
        return removed

    # ── 统计与诊断 ───────────────────────────────────────────────

    def snapshot(self) -> GrowthStats:
        """获取统计快照。"""
        with self._lock:
            fragments = list(self._fragments.values())
            active = [f for f in fragments if f.contribution_score >= self.contribution_fn.retention_threshold]
            paths = list(self._paths.values())

            return GrowthStats(
                total_fragments=len(fragments),
                active_fragments=len(active),
                pruned_fragments=len(fragments) - len(active),
                total_paths=len(paths),
                total_seed_tokens=sum(len(f.seed_tokens) for f in fragments),
                total_contributions_evaluated=len(self._contribution_traces),
                avg_fragment_confidence=round(
                    np.mean([f.confidence for f in fragments])
                    if fragments else 0.0,
                    4,
                ),
                avg_path_length=round(
                    np.mean([len(p.fragments) for p in paths])
                    if paths else 0.0,
                    4,
                ),
                refinement_count=sum(
                    f.refinement_count for f in fragments
                ),
            )

    def statistics(self) -> Dict[str, Any]:
        """返回完整运行时统计。"""
        snap = self.snapshot()
        return {
            "fragments_total": snap.total_fragments,
            "fragments_active": snap.active_fragments,
            "fragments_pruned": snap.pruned_fragments,
            "paths_total": snap.total_paths,
            "seed_tokens_total": snap.total_seed_tokens,
            "contributions_evaluated": snap.total_contributions_evaluated,
            "avg_fragment_confidence": snap.avg_fragment_confidence,
            "avg_path_length": snap.avg_path_length,
            "refinements_total": snap.refinement_count,
            "growth_mode": self.growth_mode.value,
            "max_fragments": self.max_fragments,
            "max_paths": self.max_paths,
            "auto_prune": self.auto_prune,
            "contribution_function": self.contribution_fn.statistics(),
        }

    def reset(self) -> None:
        """重置所有状态。"""
        with self._lock:
            self._fragments.clear()
            self._paths.clear()
            self._contribution_traces.clear()
        logger.info("MemoryGrowthEngine reset")
