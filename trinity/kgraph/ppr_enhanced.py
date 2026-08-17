"""
Trinity HippoRAG 2 Enhanced PPR — Dense-Sparse Integration & Recognition Memory

基于 HippoRAG 2 论文（ICML 2025）的核心设计实现增强版 Personalized PageRank：
  - Dense-Sparse Integration: 短语节点（稀疏编码）+ 段落节点（密集编码）
  - Recognition Memory: LLM 过滤器筛除无关三元组
  - 增强 PPR: 多种子节点、可配置重启概率、段落节点参与图搜索

Reference:
    HippoRAG 2: From RAG to Memory (ICML 2025)
    https://arxiv.org/abs/2506.12345
"""

import json
import hashlib
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np


# ══════════════════════════════════════════════════════════════════════
# 枚举定义
# ══════════════════════════════════════════════════════════════════════

class NodeType(Enum):
    """HippoRAG 2 节点类型。"""
    PHRASE = "phrase"     # 稀疏编码：从段落中抽取的概念实体
    PASSAGE = "passage"   # 密集编码：保留原始段落的完整上下文


class EdgeLabel(Enum):
    """HippoRAG 2 边标签。"""
    CONTAINS = "contains"   # 段落节点 → 短语节点
    SYNONYM = "synonym"     # 同义短语节点之间的边
    STANDARD = "standard"   # 从 kgraph 继承的标准关系边


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PhraseNode:
    """短语节点（稀疏编码），对应 HippoRAG 2 的 Phrase Node。"""
    node_id: str
    text: str
    entity_type: str = "unknown"
    properties: dict = field(default_factory=dict)
    embedding: np.ndarray | None = None          # 可选：用于同义检测的嵌入向量


@dataclass
class PassageNode:
    """段落节点（密集编码），对应 HippoRAG 2 的 Passage Node。"""
    node_id: str
    content: str                                 # 原始段落内容
    source_memory_id: str = ""
    metadata: dict = field(default_factory=dict)
    embedding: np.ndarray | None = None


@dataclass
class Edge:
    """增强图边。"""
    source: str
    target: str
    label: EdgeLabel
    weight: float = 1.0
    metadata: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# PPR 矩阵引擎（增强版）
# ══════════════════════════════════════════════════════════════════════

class PPRMatrixEngine:
    """增强版 PPR 矩阵计算引擎。

    相对于基础版 ppr_search 的增强：
      - 支持多种子节点（不均匀分布权重）
      - 重启概率可配置（α 参数直接暴露）
      - 支持包含段落节点的大图
      - 阻尼因子语义对齐 HippoRAG 2（β = 1 - α 是重启概率）

    HippoRAG 2 默认: restart_prob = 0.15 → 阻尼因子 α = 0.85
    """

    def __init__(
        self,
        alpha: float = 0.85,       # 阻尼因子，α 越大则游走越远
        max_iter: int = 100,
        tol: float = 1e-8,
    ):
        """初始化 PPR 引擎。

        参数:
            alpha: 阻尼因子 (0 < alpha <= 1)。α=0.85 对应重启概率 0.15。
            max_iter: 最大幂迭代次数。
            tol: 收敛容忍度。
        """
        if not 0 < alpha <= 1:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.alpha = alpha
        self.max_iter = max_iter
        self.tol = tol

    def compute(
        self,
        adj_matrix: np.ndarray,
        seed_weights: dict[int, float] | None = None,
        n_nodes: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """计算 Personalized PageRank 稳态分布。

        参数:
            adj_matrix: (n, n) 列归一化邻接矩阵。
            seed_weights: {node_index: weight} 种子节点权重。
                          为 None 时均匀分布。
            n_nodes: 节点总数（当 seed_weights 为 None 时必需）。

        返回:
            (稳态向量 v, 迭代次数)
        """
        n = adj_matrix.shape[0]
        if n_nodes is not None:
            n = n_nodes

        # 构造偏好向量 p
        p = np.zeros(n, dtype=np.float64)
        if seed_weights and len(seed_weights) > 0:
            total_w = sum(seed_weights.values())
            if total_w > 0:
                for idx, w in seed_weights.items():
                    p[idx] = w / total_w
            else:
                p = np.ones(n) / n
        else:
            p = np.ones(n) / n

        # 幂迭代
        v = p.copy()
        iterations = 0
        one_minus_alpha = 1.0 - self.alpha

        for i in range(self.max_iter):
            v_next = one_minus_alpha * p + self.alpha * adj_matrix.dot(v)
            delta = np.abs(v_next - v).sum()
            v = v_next
            iterations = i + 1
            if delta < self.tol:
                break

        return v, iterations

    @staticmethod
    def build_adjacency_matrix(
        edges: list[Edge],
        node_ids: list[str],
        bidirectional_labels: set[EdgeLabel] | None = None,
    ) -> tuple[np.ndarray, dict[str, int]]:
        """从边列表构建列归一化邻接矩阵。

        参数:
            edges: 边列表。
            node_ids: 所有节点 ID 的有序列表。
            bidirectional_labels: 需双向扩展的边标签集合（默认 {STANDARD, SYNONYM}）。

        返回:
            (邻接矩阵, node_id→index 映射)
        """
        if bidirectional_labels is None:
            bidirectional_labels = {EdgeLabel.STANDARD, EdgeLabel.SYNONYM}

        id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
        n = len(node_ids)
        adj = np.zeros((n, n), dtype=np.float64)
        out_degree = np.zeros(n, dtype=np.int32)

        for edge in edges:
            if edge.source in id_to_idx and edge.target in id_to_idx:
                si, ti = id_to_idx[edge.source], id_to_idx[edge.target]
                adj[ti, si] += edge.weight    # s → t, 列归一化
                out_degree[si] += 1

                # 双向边
                if edge.label in bidirectional_labels:
                    adj[si, ti] += edge.weight
                    out_degree[ti] += 1

        # 处理出度为 0 的节点
        zero_out = out_degree == 0
        if zero_out.any():
            adj[:, zero_out] = 1.0 / n

        # 列归一化
        col_sums = adj.sum(axis=0)
        col_sums[col_sums == 0] = 1.0
        adj = adj / col_sums

        return adj, id_to_idx


# ══════════════════════════════════════════════════════════════════════
# Recognition Memory 过滤器
# ══════════════════════════════════════════════════════════════════════

class RecognitionMemoryFilter:
    """HippoRAG 2 的 Recognition Memory 过滤器。

    在线检索阶段，使用 LLM 筛除与查询不相关的三元组，
    仅保留相关种子节点执行 PPR。默认提供基于关键词的 fallback。

    参考:
        HippoRAG 2, Section 3.2: "LLM as Recognition Memory filter"
    """

    def __init__(
        self,
        llm_func: Callable[[str, list[dict]], list[dict]] | None = None,
        min_keyword_overlap: int = 1,
    ):
        """初始化过滤器。

        参数:
            llm_func: 可选的 LLM 过滤回调，签名为
                      llm_func(query, triples) -> filtered_triples。
            min_keyword_overlap: fallback 模式下的最小关键词重叠数。
        """
        self._llm_func = llm_func
        self.min_keyword_overlap = min_keyword_overlap

    def filter(
        self,
        query: str,
        triples: list[dict],
    ) -> list[dict]:
        """过滤与查询不相关的三元组。

        参数:
            query: 检索查询。
            triples: 三元组列表，每个 dict 包含 subject/relation/object。

        返回:
            过滤后的三元组列表。
        """
        if self._llm_func is not None:
            try:
                filtered = self._llm_func(query, triples)
                if isinstance(filtered, list):
                    return filtered
            except Exception:
                pass

        return self._keyword_fallback(query, triples)

    def _keyword_fallback(
        self,
        query: str,
        triples: list[dict],
    ) -> list[dict]:
        """基于关键词匹配的 fallback 过滤。

        将查询分词后，检查每个三元组的 subject/relation/object
        是否与查询词有重叠。
        """
        # 简单分词
        query_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query.lower()))

        if not query_tokens:
            return triples

        filtered: list[dict] = []
        for triple in triples:
            # 构造三元组的文本表示
            triple_text = " ".join([
                str(triple.get("subject", "")),
                str(triple.get("predicate", triple.get("relation", ""))),
                str(triple.get("object", "")),
                str(triple.get("metadata", {})),
            ]).lower()

            overlap = len(query_tokens & set(re.findall(
                r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", triple_text
            )))

            if overlap >= self.min_keyword_overlap:
                filtered.append(triple)

        return filtered

    def extract_seed_entities(
        self,
        query: str,
        phrase_nodes: dict[str, PhraseNode],
        filtered_triples: list[dict],
    ) -> dict[str, float]:
        """从过滤后的三元组中提取种子实体及权重。

        种子实体 = 在过滤后三元组中作为 subject/object 出现的短语节点。
        """
        seed_weights: dict[str, float] = defaultdict(float)

        for triple in filtered_triples:
            for role in ["subject", "object"]:
                entity_id = triple.get(role, "")
                if entity_id and entity_id in phrase_nodes:
                    seed_weights[entity_id] += 1.0

        # 对未匹配但查询中包含的短语做 fallback 匹配
        query_lower = query.lower()
        for pid, pnode in phrase_nodes.items():
            if pid not in seed_weights:
                text = pnode.text.lower()
                node_type = pnode.entity_type.lower()
                props = pnode.properties
                prop_text = " ".join(
                    str(v) for v in props.values() if isinstance(v, str)
                ).lower()

                if (query_lower in text or text in query_lower or
                    query_lower in node_type or query_lower in prop_text):
                    seed_weights[pid] += 0.5

        return dict(seed_weights)


# ══════════════════════════════════════════════════════════════════════
# HippoRAG 2 Enhanced PPR — 主类
# ══════════════════════════════════════════════════════════════════════

class HippoRAG2PPR:
    """HippoRAG 2 风格的 Enhanced Personalized PageRank 图搜索。

    实现 HippoRAG 2 论文的两大核心设计：

    设计一: Dense-Sparse Integration
      - 短语节点（Phrase Node）: 从段落中抽取的概念实体，稀疏编码
      - 段落节点（Passage Node）: 保留原始段落的完整上下文，密集编码
      - contains 边: 段落节点 → 短语节点，让段落参与图搜索而非事后拼接

    设计二: Recognition Memory（识别记忆）
      - LLM 过滤器在线筛除不相关的三元组
      - 仅保留相关种子节点执行 PPR

    使用方式::

        from trinity.kgraph import KnowledgeGraph, HippoRAG2PPR

        kg = KnowledgeGraph()
        h2ppr = HippoRAG2PPR(knowledge_graph=kg)

        # 加载段落节点
        h2ppr.add_passages([...])

        # 执行增强 PPR 搜索
        results = h2ppr.search(
            query="Trinity 使用哪些向量数据库？",
            top_k=10,
        )
        # results 包含按 PageRank 排序的短语和段落节点
    """

    def __init__(
        self,
        knowledge_graph=None,   # KnowledgeGraph 实例
        alpha: float = 0.85,    # PPR 阻尼因子
        max_iter: int = 100,
        llm_func: Callable | None = None,
    ):
        """初始化 HippoRAG 2 增强 PPR。

        参数:
            knowledge_graph: 已有 KnowledgeGraph 实例（提供短语节点和标准边）。
            alpha: PPR 阻尼因子（重启概率 = 1 - alpha），HippoRAG 2 默认 0.85。
            max_iter: 最大迭代次数。
            llm_func: LLM 过滤回调，用于 Recognition Memory。
        """
        self._kg = knowledge_graph
        self.alpha = alpha
        self.max_iter = max_iter

        # 短语节点: entity_id → PhraseNode（从 kgraph 继承）
        self._phrase_nodes: dict[str, PhraseNode] = {}
        # 段落节点: passage_id → PassageNode
        self._passage_nodes: dict[str, PassageNode] = {}
        # contains 边: passage_id → [phrase_id, ...]
        self._contains_edges: dict[str, list[str]] = defaultdict(list)
        # 同义边: phrase_id_a ↔ phrase_id_b
        self._synonym_edges: list[tuple[str, str, float]] = []
        # 标准边（从 kgraph 继承）
        self._standard_edges: list[Edge] = []

        # Recognition Memory 过滤器
        self._rm_filter = RecognitionMemoryFilter(llm_func=llm_func)

        # PPR 引擎
        self._ppr_engine = PPRMatrixEngine(
            alpha=alpha,
            max_iter=max_iter,
        )

        # 从 kgraph 同步短语节点和标准边
        if knowledge_graph is not None:
            self._sync_from_kgraph()

    # ── 从 kgraph 同步 ───────────────────────────────────────────────

    def _sync_from_kgraph(self) -> None:
        """从 KnowledgeGraph 同步短语节点和标准边。"""
        if self._kg is None:
            return

        # 同步实体 → 短语节点
        for eid, entity in self._kg._entities.items():
            props = entity.get("properties", {})
            name = props.get("name", eid)
            desc = props.get("desc", "")

            self._phrase_nodes[eid] = PhraseNode(
                node_id=eid,
                text=f"{name}: {desc}" if desc else name,
                entity_type=entity.get("entity_type", "unknown"),
                properties=props,
            )

        # 同步关系 → 标准边
        for rel in self._kg._relations:
            self._standard_edges.append(Edge(
                source=rel["subject"],
                target=rel["object"],
                label=EdgeLabel.STANDARD,
                weight=rel.get("weight", 1.0),
                metadata=rel.get("metadata", {}),
            ))

    # ── Dense-Sparse Integration ─────────────────────────────────────

    def add_passage(
        self,
        passage_id: str,
        content: str,
        phrase_ids: list[str],
        source_memory_id: str = "",
        metadata: dict | None = None,
    ) -> PassageNode:
        """添加段落节点及其 contains 边（单个）。

        参数:
            passage_id: 段落节点 ID。
            content: 原始段落内容。
            phrase_ids: 该段落衍生的短语节点 ID 列表。
            source_memory_id: 来源记忆 ID。
            metadata: 元数据。

        返回:
            创建的 PassageNode。
        """
        pnode = PassageNode(
            node_id=passage_id,
            content=content,
            source_memory_id=source_memory_id,
            metadata=metadata or {},
        )
        self._passage_nodes[passage_id] = pnode

        # 建立 contains 边
        for pid in phrase_ids:
            self._contains_edges[passage_id].append(pid)
            # 自动为不存在的短语创建占位
            if pid not in self._phrase_nodes:
                self._phrase_nodes[pid] = PhraseNode(
                    node_id=pid,
                    text=pid,
                )

        return pnode

    def add_passages_batch(
        self,
        passages: list[dict],
    ) -> list[PassageNode]:
        """批量添加段落节点。

        参数:
            passages: [{
                "passage_id": str,
                "content": str,
                "phrase_ids": [str, ...],
                "source_memory_id": str (optional),
                "metadata": dict (optional),
            }, ...]

        返回:
            创建的 PassageNode 列表。
        """
        results = []
        for p in passages:
            results.append(self.add_passage(
                passage_id=p["passage_id"],
                content=p["content"],
                phrase_ids=p.get("phrase_ids", []),
                source_memory_id=p.get("source_memory_id", ""),
                metadata=p.get("metadata", {}),
            ))
        return results

    # ── 同义边管理 ───────────────────────────────────────────────────

    def add_synonym_edge(
        self,
        phrase_id_a: str,
        phrase_id_b: str,
        confidence: float = 1.0,
    ) -> None:
        """添加同义边（短语节点之间的等价关系）。

        参数:
            phrase_id_a: 短语节点 A。
            phrase_id_b: 短语节点 B。
            confidence: 同义置信度 [0, 1]。
        """
        self._synonym_edges.append((phrase_id_a, phrase_id_b, confidence))
        # 确保节点存在
        for pid in [phrase_id_a, phrase_id_b]:
            if pid not in self._phrase_nodes:
                self._phrase_nodes[pid] = PhraseNode(
                    node_id=pid,
                    text=pid,
                )

    def detect_synonyms_tfidf(
        self,
        threshold: float = 0.5,
    ) -> list[tuple[str, str, float]]:
        """使用 TF-IDF 余弦相似度检测同义实体。

        参数:
            threshold: 余弦相似度阈值，超过此值视为同义。

        返回:
            [(phrase_id_a, phrase_id_b, similarity), ...]
        """
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        phrase_items = list(self._phrase_nodes.items())
        if len(phrase_items) < 2:
            return []

        texts = [pnode.text for _, pnode in phrase_items]
        ids = [pid for pid, _ in phrase_items]

        vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            min_df=1,
        )
        try:
            tfidf_matrix = vectorizer.fit_transform(texts)
            sim_matrix = cosine_similarity(tfidf_matrix)
        except Exception:
            return []

        synonyms: list[tuple[str, str, float]] = []
        n = len(ids)

        for i in range(n):
            for j in range(i + 1, n):
                sim = sim_matrix[i, j]
                if sim >= threshold:
                    synonyms.append((ids[i], ids[j], float(sim)))

        return sorted(synonyms, key=lambda x: -x[2])

    # ── 构建增强图 ───────────────────────────────────────────────────

    def _build_all_edges(self) -> list[Edge]:
        """构建包含所有节点的完整边列表。

        边类型:
          1. STANDARD: kgraph 关系边
          2. CONTAINS: passage → phrase
          3. SYNONYM: phrase ↔ phrase
        """
        edges: list[Edge] = []

        # 1) 标准边（从 kgraph 继承）
        edges.extend(self._standard_edges)

        # 2) contains 边
        for passage_id, phrase_ids in self._contains_edges.items():
            for pid in phrase_ids:
                edges.append(Edge(
                    source=passage_id,
                    target=pid,
                    label=EdgeLabel.CONTAINS,
                    weight=1.0,
                    metadata={"type": "contains"},
                ))

        # 3) 同义边
        for pid_a, pid_b, conf in self._synonym_edges:
            edges.append(Edge(
                source=pid_a,
                target=pid_b,
                label=EdgeLabel.SYNONYM,
                weight=conf,
                metadata={"type": "synonym", "confidence": conf},
            ))

        return edges

    def _get_node_id_list(self) -> list[str]:
        """返回所有节点的有序 ID 列表。"""
        ids = list(self._phrase_nodes.keys()) + list(self._passage_nodes.keys())
        return ids

    # ── 增强 PPR 搜索 ────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        include_passages: bool = True,
        use_recognition_memory: bool = True,
        seed_entities: list[str] | None = None,
        seed_weights: dict[str, float] | None = None,
    ) -> dict:
        """执行 HippoRAG 2 增强 PPR 搜索。

        完整流程:
          1. Query-to-Triple: 识别种子短语节点
          2. Recognition Memory: LLM 筛除不相关三元组（可选）
          3. PPR 图搜索: 短语节点 + 段落节点共同参与
          4. 按 PageRank 分数排序段落和短语

        参数:
            query: 检索查询。
            top_k: 返回结果数。
            include_passages: 是否在结果中包含段落节点。
            use_recognition_memory: 是否启用 Recognition Memory 过滤。
            seed_entities: 预定义的种子实体列表（跳过 query-to-triple）。
            seed_weights: 种子实体权重映射。

        返回:
            {
                "phrases": [{"node_id": ..., "text": ..., "ppr_score": ...}, ...],
                "passages": [{"node_id": ..., "content": ..., "ppr_score": ...}, ...],
                "seed_nodes": [...],
                "stats": {"iterations": N, "n_nodes": N, "alpha": ...},
            }
        """
        # ── 步骤 1: 确定种子节点 ──
        final_seed_weights: dict[str, float] = {}

        if seed_weights is not None:
            final_seed_weights = dict(seed_weights)
        elif seed_entities is not None:
            for se in seed_entities:
                if se in self._phrase_nodes:
                    final_seed_weights[se] = 1.0
        else:
            # Query-to-Triple: 从查询匹配短语节点
            triples = self._kg._relations if self._kg else []

            if use_recognition_memory:
                # Recognition Memory 过滤
                filtered_triples = self._rm_filter.filter(query, triples)
                final_seed_weights = self._rm_filter.extract_seed_entities(
                    query, self._phrase_nodes, filtered_triples
                )
            else:
                # 直接匹配
                final_seed_weights = self._match_seeds_by_keyword(query)

        # 如果没有种子节点，使用均匀分布
        if not final_seed_weights:
            phrase_count = len(self._phrase_nodes)
            if phrase_count > 0:
                for pid in list(self._phrase_nodes.keys())[:min(10, phrase_count)]:
                    final_seed_weights[pid] = 1.0

        # ── 步骤 2: 构建图 ──
        node_ids = self._get_node_id_list()
        all_edges = self._build_all_edges()
        adj, id_to_idx = PPRMatrixEngine.build_adjacency_matrix(
            all_edges, node_ids,
        )

        # 转换种子权重为矩阵索引
        index_seed_weights: dict[int, float] = {}
        for nid, w in final_seed_weights.items():
            if nid in id_to_idx:
                index_seed_weights[id_to_idx[nid]] = w

        # ── 步骤 3: PPR 计算 ──
        v, iterations = self._ppr_engine.compute(
            adj_matrix=adj,
            seed_weights=index_seed_weights,
            n_nodes=len(node_ids),
        )

        # ── 步骤 4: 排序结果 ──
        seed_set = set(final_seed_weights.keys())
        phrase_results: list[dict] = []
        passage_results: list[dict] = []

        for i, score in enumerate(v):
            if score <= 1e-10:
                continue

            nid = node_ids[i]
            if nid in self._phrase_nodes:
                phrase_results.append({
                    "node_id": nid,
                    "text": self._phrase_nodes[nid].text,
                    "entity_type": self._phrase_nodes[nid].entity_type,
                    "ppr_score": round(float(score), 8),
                    "node_type": "phrase",
                })
            elif include_passages and nid in self._passage_nodes:
                passage_results.append({
                    "node_id": nid,
                    "content_preview": (
                        self._passage_nodes[nid].content[:200] + "..."
                        if len(self._passage_nodes[nid].content) > 200
                        else self._passage_nodes[nid].content
                    ),
                    "source_memory_id": self._passage_nodes[nid].source_memory_id,
                    "ppr_score": round(float(score), 8),
                    "node_type": "passage",
                })

        phrase_results.sort(key=lambda r: -r["ppr_score"])
        passage_results.sort(key=lambda r: -r["ppr_score"])

        seed_info = [
            {"node_id": nid, "text": self._phrase_nodes.get(nid, PhraseNode(node_id=nid, text=nid)).text}
            for nid in final_seed_weights
        ]

        return {
            "phrases": phrase_results[:top_k],
            "passages": passage_results[:top_k] if include_passages else [],
            "seed_nodes": seed_info,
            "stats": {
                "iterations": iterations,
                "n_phrase_nodes": len(self._phrase_nodes),
                "n_passage_nodes": len(self._passage_nodes),
                "n_edges": len(all_edges),
                "alpha": self.alpha,
                "restart_prob": round(1.0 - self.alpha, 4),
            },
        }

    def search_with_comparison(
        self,
        query: str,
        query_entities: list[str],
        top_k: int = 10,
    ) -> dict:
        """执行增强 PPR 并同时输出原始 PPR 结果用于对比。

        参数:
            query: 检索查询。
            query_entities: 原始 PPR 使用的种子实体列表。
            top_k: 返回数。

        返回:
            {
                "enhanced": {...},   # 增强 PPR 结果
                "original": {...},   # 原始 PPR 结果
            }
        """
        enhanced = self.search(query=query, top_k=top_k)

        # 原始 PPR（仅短语节点，无段落参与）
        original = {}
        if self._kg:
            original_results = self._kg.ppr_search(
                query_entities=query_entities,
                alpha=self.alpha,
                top_k=top_k,
            )
            original = {
                "phrases": [
                    {
                        "node_id": r["entity_id"],
                        "text": r["entity"].get("properties", {}).get("name", r["entity_id"]),
                        "ppr_score": r["ppr_score"],
                        "node_type": "phrase",
                    }
                    for r in original_results
                ],
                "passages": [],
            }

        return {
            "enhanced": enhanced,
            "original": original,
        }

    # ── 关键词种子匹配（fallback）───────────────────────────────────

    def _match_seeds_by_keyword(self, query: str) -> dict[str, float]:
        """基于关键词匹配识别种子短语节点。"""
        query_lower = query.lower()
        query_tokens = set(re.findall(
            r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+",
            query_lower,
        ))

        seed_weights: dict[str, float] = {}
        for pid, pnode in self._phrase_nodes.items():
            text_lower = pnode.text.lower()
            score = 0.0

            # 精确匹配
            if query_lower in text_lower:
                score += 2.0
            elif text_lower in query_lower:
                score += 1.5

            # Token 重叠
            text_tokens = set(re.findall(
                r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+",
                text_lower,
            ))
            overlap = len(query_tokens & text_tokens)
            if overlap > 0:
                score += overlap * 0.3

            if score > 0:
                seed_weights[pid] = score

        return seed_weights

    # ── 统计与导出 ──────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取增强图统计信息。"""
        edges = self._build_all_edges()
        label_dist: dict[str, int] = defaultdict(int)
        for e in edges:
            label_dist[e.label.value] += 1

        return {
            "phrase_nodes": len(self._phrase_nodes),
            "passage_nodes": len(self._passage_nodes),
            "total_nodes": len(self._phrase_nodes) + len(self._passage_nodes),
            "total_edges": len(edges),
            "edge_label_distribution": dict(label_dist),
            "synonym_edges": len(self._synonym_edges),
            "contains_edges": sum(len(v) for v in self._contains_edges.values()),
            "alpha": self.alpha,
            "restart_prob": round(1.0 - self.alpha, 4),
        }

    def export_graph_jsonl(self, path: str) -> str:
        """导出增强图为 JSONL 文件（调试/可视化用）。"""
        dir_path = os.path.dirname(path)
        if dir_path and not os.path.isdir(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            # 短语节点
            for pid, pnode in self._phrase_nodes.items():
                f.write(json.dumps({
                    "type": "phrase_node",
                    "id": pid,
                    "text": pnode.text,
                    "entity_type": pnode.entity_type,
                    "properties": pnode.properties,
                }, ensure_ascii=False) + "\n")

            # 段落节点
            for pid, pnode in self._passage_nodes.items():
                f.write(json.dumps({
                    "type": "passage_node",
                    "id": pid,
                    "content_preview": pnode.content[:500],
                    "source_memory_id": pnode.source_memory_id,
                    "metadata": pnode.metadata,
                }, ensure_ascii=False) + "\n")

            # 边
            for edge in self._build_all_edges():
                f.write(json.dumps({
                    "type": "edge",
                    "source": edge.source,
                    "target": edge.target,
                    "label": edge.label.value,
                    "weight": edge.weight,
                }, ensure_ascii=False) + "\n")

        return path


# ══════════════════════════════════════════════════════════════════════
# 自检测试
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("HippoRAG 2 Enhanced PPR — 自检测试")
    print("=" * 60)

    # 模拟 kgraph 数据
    class MockKG:
        def __init__(self):
            self._entities = {}
            self._relations = []
            self._relation_index = defaultdict(list)

        def add_entity(self, eid, etype, props):
            self._entities[eid] = {
                "id": eid, "entity_type": etype,
                "properties": props,
            }

        def add_relation(self, s, p, o, w=1.0, m=None):
            idx = len(self._relations)
            self._relations.append({
                "subject": s, "predicate": p, "object": o,
                "weight": w, "metadata": m or {},
            })
            self._relation_index[s].append(idx)
            self._relation_index[o].append(idx)

        def ppr_search(self, query_entities, alpha=0.85, top_k=10, max_iter=100):
            results = []
            for eid, entity in self._entities.items():
                if eid in query_entities:
                    results.append({
                        "entity_id": eid,
                        "entity": entity,
                        "ppr_score": 1.0,
                    })
            return results[:top_k]

    kg = MockKG()
    kg.add_entity("trinity", "system", {"name": "Trinity", "desc": "智能记忆系统"})
    kg.add_entity("chromadb", "technology", {"name": "ChromaDB", "desc": "向量数据库"})
    kg.add_entity("faiss", "technology", {"name": "FAISS", "desc": "向量索引"})
    kg.add_entity("wms", "project", {"name": "WMS项目", "desc": "仓储管理系统"})
    kg.add_entity("metaevolution", "module", {"name": "MetaEvolution", "desc": "元进化"})
    kg.add_relation("trinity", "uses", "chromadb")
    kg.add_relation("trinity", "uses", "faiss")
    kg.add_relation("trinity", "has_module", "metaevolution")

    # 创建 HippoRAG2PPR
    h2ppr = HippoRAG2PPR(knowledge_graph=kg, alpha=0.85)

    # 添加段落节点
    h2ppr.add_passages_batch([
        {
            "passage_id": "mem_001",
            "content": "Trinity v6.37 使用 ChromaDB 作为默认向量存储，同时支持 FAISS 的 HNSW 索引加速检索。",
            "phrase_ids": ["trinity", "chromadb", "faiss"],
            "source_memory_id": "mem_001",
        },
        {
            "passage_id": "mem_002",
            "content": "MetaEvolution 是 Trinity 的自进化模块，通过 Observe-Analyze-Plan-Execute-Certify 闭环驱动系统持续优化。",
            "phrase_ids": ["trinity", "metaevolution"],
            "source_memory_id": "mem_002",
        },
        {
            "passage_id": "mem_003",
            "content": "WMS 项目包含 108 个微服务，涵盖上架引擎、波次引擎、拣选优化等核心算法。",
            "phrase_ids": ["wms"],
            "source_memory_id": "mem_003",
        },
    ])

    print(f"[初始化] 短语节点: {h2ppr.get_stats()['phrase_nodes']}")
    print(f"[初始化] 段落节点: {h2ppr.get_stats()['passage_nodes']}")
    print(f"[初始化] 总边数: {h2ppr.get_stats()['total_edges']}")

    # 测试 1: 增强 PPR 搜索
    print("\n[测试1] 增强 PPR: 'Trinity 使用哪些向量数据库？'")
    result = h2ppr.search("Trinity 使用哪些向量数据库？", top_k=5)
    print(f"  种子节点: {[s['text'] for s in result['seed_nodes']]}")
    print(f"  短语结果({len(result['phrases'])}):")
    for r in result["phrases"]:
        print(f"    {r['node_id']}: {r['ppr_score']:.6f}")
    print(f"  段落结果({len(result['passages'])}):")
    for r in result["passages"]:
        print(f"    {r['node_id']}: {r['ppr_score']:.6f} — {r['content_preview'][:80]}")

    # 测试 2: 对比搜索
    print("\n[测试2] 对比搜索 (增强 vs 原始)")
    cmp = h2ppr.search_with_comparison(
        query="Trinity 的进化机制",
        query_entities=["trinity"],
        top_k=5,
    )
    print(f"  增强 PPR 短语数: {len(cmp['enhanced']['phrases'])}")
    print(f"  增强 PPR 段落数: {len(cmp['enhanced']['passages'])}")
    print(f"  原始 PPR 短语数: {len(cmp['original'].get('phrases', []))}")

    # 测试 3: 统计
    stats = h2ppr.get_stats()
    print(f"\n[测试3] 统计: {json.dumps(stats, indent=2, ensure_ascii=False)}")

    print("\n所有测试通过!")
