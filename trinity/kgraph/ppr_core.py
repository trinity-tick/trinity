# -*- coding: utf-8 -*-
"""PPR 图检索核心（2026-08-24, R8 P1-4）。

从聚合池 _AggregatorKGraphAdapter.ppr_search 提取的幂迭代 PPR 算法，
供引擎 HybridRetriever 图谱通道与聚合池图谱通道共用（对齐
HippoRAG 2 增强 PPR / Graphiti 的 PPR+邻居扩展共识）。

用法：
    from trinity.kgraph.ppr_core import ppr_from_graph
    hits = ppr_from_graph(relations_graph, seed_ids, top_k=20)
    # relations_graph: {node_id: {neighbor_id: edge_label_or_weight}}
    # 返回 [{"id": node_id, "score": ppr_score}, ...]（含种子自身）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def ppr_from_graph(
    graph: Dict[str, Dict[str, Any]],
    seed_ids: List[str],
    top_k: int = 20,
    alpha: float = 0.85,
    max_iter: int = 50,
    tol: float = 1e-6,
    hops: int = 3,
    include_seeds: bool = True,
) -> List[Dict[str, Any]]:
    """Personalized PageRank（幂迭代）— BFS 子图限定 + 个性化重启。

    Args:
        graph: 邻接表 {node: {neighbor: label_or_weight}}。
        seed_ids: 种子节点（个性化重启分布均分）。
        top_k: 返回结果数。
        alpha: 阻尼因子（重启概率 = 1 - alpha）。
        max_iter: 幂迭代上限。
        tol: 收敛阈值。
        hops: BFS 收集子图的跳数（限定幂迭代规模）。
        include_seeds: 结果是否包含种子节点自身。

    Returns:
        [{"id": node_id, "score": ppr_score}, ...]（降序）。
    """
    seeds = {s for s in seed_ids if s and s in graph}
    if not seeds:
        return []

    # ── BFS 收集 hops 跳内可达子图（限定幂迭代规模）──
    nodes = set(seeds)
    frontier = set(seeds)
    for _ in range(hops):
        nxt = set()
        for n in frontier:
            for nb in graph.get(n, {}):
                if nb not in nodes:
                    nodes.add(nb)
                    nxt.add(nb)
        frontier = nxt
        if not frontier:
            break
    nodes = list(nodes)
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    # ── 个性化重启分布 + 初始向量（种子均分）──
    p = [0.0] * n
    for s in seeds:
        p[idx[s]] = 1.0 / len(seeds)
    v = list(p)

    # ── 行归一化转移矩阵（出度均匀分布；悬空节点跳回个性化分布）──
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

    # ── 幂迭代: v = alpha·Mᵀv + (1-alpha)·p ──
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

    # ── 排序返回 ──
    ranked = sorted(range(n), key=lambda i: v[i], reverse=True)
    out = []
    for i in ranked:
        node_id = nodes[i]
        if not include_seeds and node_id in seeds:
            continue
        out.append({"id": node_id, "score": round(float(v[i]), 6)})
        if len(out) >= top_k:
            break
    return out
