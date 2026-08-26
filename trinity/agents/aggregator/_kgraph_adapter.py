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
        2026-08-24（R8 P1-4）：算法提取至 trinity/kgraph/ppr_core.py 共享。
        """
        from trinity.kgraph.ppr_core import ppr_from_graph

        # 1) 种子解析
        seeds = set()
        for seed in query_entities:
            sid = seed if isinstance(seed, str) else (seed or {}).get("id", "")
            if sid:
                seeds.add(sid)
        if not seeds:
            return []

        # 2) 幂迭代 PPR（共享实现：BFS 3 跳子图 + 个性化重启）
        return ppr_from_graph(
            self._agg._relations_graph,
            list(seeds),
            top_k=top_k,
            alpha=alpha,
            max_iter=max_iter,
            tol=tol,
        )
