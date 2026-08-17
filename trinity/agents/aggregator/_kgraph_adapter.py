"""KGraph adapter for the MemoryAggregator package (split from aggregator.py, 2026-08-17).
Plain class (not a mixin), kept intact from the pre-split implementation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

class _AggregatorKGraphAdapter:
    """GraphVectorHybridRetriever 兼容的轻量 kgraph（基于聚合池关系图）。"""

    def __init__(self, aggregator: "MemoryAggregator"):
        self._agg = aggregator

    def query_relations(self, memory_id: str, max_depth: int = 1) -> list:
        """返回 memory_id 的直接邻接关系（模拟 kgraph.query_relations）。"""
        graph = self._agg._relations_graph
        edges = []
        adj = graph.get(memory_id, {})
        for target, rel in adj.items():
            edges.append({"subject_id": memory_id, "object_id": target,
                          "predicate": str(rel)})
        # 反向边（target → memory_id）
        for src, adj_dict in graph.items():
            if memory_id in adj_dict:
                edges.append({"subject_id": src, "object_id": memory_id,
                              "predicate": str(adj_dict[memory_id])})
        return edges[: max_depth * 16]

    def get_entity(self, memory_id: str) -> Optional[dict]:
        """返回 memory_id 对应的记忆向量（模拟 get_entity）。"""
        dv = self._agg._pool.get(memory_id)
        if dv is None:
            return None
        return {"id": memory_id, "name": memory_id,
                "properties": {"content": getattr(dv, "content", "")[:200]}}

    def ppr_search(
        self,
        query_entities: list,
        top_k: int = 20,
        alpha: float = 0.85,
        max_iter: int = 50,
        tol: float = 1e-6,
        **kwargs,
    ) -> list:
        """Personalized PageRank（幂迭代）— 2026-08-17 由 1-2 跳 BFS 升级。

        对齐 HippoRAG 2 增强 PPR：种子实体注入个性化重启分布 p，
        v_{t+1} = alpha·Mᵀv_t + (1-alpha)·p，图扩散评分优于固定跳数加权。
        BFS 先收集 3 跳内子图限定规模（11k 节点图上幂迭代可控）。
        """
        from collections import Counter
        graph = self._agg._relations_graph

        # 1) 种子解析
        seeds = set()
        for seed in query_entities:
            sid = seed if isinstance(seed, str) else (seed or {}).get("id", "")
            if sid:
                seeds.add(sid)
        if not seeds:
            return []

        # 2) BFS 收集 3 跳内可达子图（限定幂迭代规模）
        nodes = set(seeds)
        frontier = set(seeds)
        for _ in range(3):
            nxt = set()
            for n in frontier:
                for nb in graph.get(n, {}):
                    if nb not in nodes:
                        nodes.add(nb)
                        nxt.add(nb)
            frontier = nxt
            if not frontier:
                break
        if not nodes:
            return []
        nodes = list(nodes)
        idx = {n: i for i, n in enumerate(nodes)}
        n = len(nodes)

        # 3) 个性化重启分布 + 初始向量（种子均分）
        p = [0.0] * n
        for s in seeds:
            p[idx[s]] = 1.0 / len(seeds)
        v = list(p)

        # 4) 行归一化转移矩阵（出度均匀分布）
        # 悬空节点（无出边）跳转到个性化分布 p，保证质量守恒（sum→1）。
        M = [[0.0] * n for _ in range(n)]
        for src in nodes:
            outs = graph.get(src, {})
            total = len(outs)
            if total == 0:
                for j in range(n):
                    M[idx[src]][j] = p[j]
                continue
            for nb in outs:
                j = idx.get(nb)
                if j is not None:
                    M[idx[src]][j] = 1.0 / total

        # 5) 幂迭代: v = alpha·Mᵀv + (1-alpha)·p
        for _ in range(max_iter):
            nv = [0.0] * n
            for i in range(n):
                vi = v[i]
                if vi <= 0.0:
                    continue
                row = M[i]
                for j in range(n):
                    mij = row[j]
                    if mij > 0.0:
                        nv[j] += alpha * vi * mij
            for j in range(n):
                nv[j] += (1.0 - alpha) * p[j]
            diff = sum(abs(nv[i] - v[i]) for i in range(n))
            v = nv
            if diff < tol:
                break

        # 6) 排序返回
        ranked = sorted(range(n), key=lambda i: v[i], reverse=True)
        return [{"id": nodes[i], "score": round(float(v[i]), 6)} for i in ranked[:top_k]]
