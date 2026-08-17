"""Shared module-level helpers for the Trinity client package.

Split from trinity/core/client.py (2026-08-17): pure functions that carry
no mutable module state. _TRINITY_STORE, _import_trinity_bridge,
_BRIDGE_CACHE and _get_cached_bridge stay in _construction.py because
Trinity.__init__/_init_sqlite_adapter mutate _TRINITY_STORE via a
global statement, which requires the canonical binding to live in the
same module as the writer.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced

# ---- Locate output directory ----
def _find_trinity_store() -> Optional[str]:
    """Find the Trinity output directory（权威大库统一解析）。

    统一规则（2026-08-15，修复双库口径）：
      1. 显式 TRINITY_STORE 环境变量（最高优先）；
      2. 否则固定权威路径 ~/.trinity/store（唯一生产存储）；
      3. 不再回退 cwd —— 曾导致 cwd 不在权威路径时创建
         data/trinity_store.db / <cwd>/trinity_store.db 等小库，
         与权威大库（11k+ 记忆）双库并存、口径不一致（压测暴露）。
    """
    env_store = os.environ.get("TRINITY_STORE")
    if env_store and os.path.isdir(env_store):
        return env_store
    home_store = str(Path.home() / ".trinity" / "store")
    os.makedirs(home_store, exist_ok=True)
    return home_store

# ---- vector search helper functions ----
def _get_embedding_engine():
    """延迟加载嵌入引擎（只初始化一次）。"""
    try:
        from trinity.embeddings.engine import create_engine
        return create_engine(backend="auto", use_cache=True)
    except Exception:
        return None

def _get_vector_index(dim: int = 1024):
    """延迟加载向量索引（只初始化一次）。
    
    默认使用 FAISS HNSW（对数级搜索），回退到 Annoy，最后到 Numpy。
    """
    try:
        from trinity.vector_index.index import create_index, HNSWConfig
        return create_index(
            backend="auto",
            dim=dim,
            metric="cosine",
            index_type="hnsw",
            hnsw_config=HNSWConfig(M=32, efConstruction=200, efSearch=64),
        )
    except Exception:
        return None

def _fuse_results(
    sqlite_results: List[Dict[str, Any]],
    vector_results: List[Dict[str, Any]],
    top_k: int,
    recency_weight: float = 0.3,
    vector_weight: float = 0.4,
    importance_weight: float = 0.3,
) -> List[Dict[str, Any]]:
    """融合排序：将 SQLite FTS 结果和向量搜索结果混合排序。

    融合公式:
        final_score = recency_norm × recency_weight
                    + vector_score × vector_weight
                    + importance × importance_weight

    Args:
        sqlite_results: SQLite 搜索结果列表。
        vector_results: 向量搜索结果列表。
        top_k: 最终返回数量。
        recency_weight: 时效性权重（默认 0.3）。
        vector_weight: 向量相似度权重（默认 0.4）。
        importance_weight: 重要性权重（默认 0.3）。

    Returns:
        融合排序后的结果列表。
    """
    seen = {}  # memory_id -> result

    # 建立向量分数映射
    vector_scores: Dict[str, float] = {}
    for vr in vector_results:
        mid = vr.get("memory_id", vr.get("id", ""))
        vector_scores[mid] = vr.get("score", 0.0)

    # 计算时间基准（最近时间戳）
    max_timestamp = 0.0
    timestamps = []
    for sr in sqlite_results:
        ts = sr.get("created_at", sr.get("timestamp", 0))
        try:
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts).timestamp()
        except Exception:
            ts = 0.0
        timestamps.append(ts)
    if timestamps:
        max_timestamp = max(timestamps)

    # 融合排序
    for sr in sqlite_results:
        mid = sr.get("memory_id", "")
        # 时效性归一化
        ts = sr.get("created_at", sr.get("timestamp", 0))
        try:
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts).timestamp()
        except Exception:
            ts = 0.0
        recency_norm = ts / max_timestamp if max_timestamp > 0 else 0.5

        vector_score = vector_scores.get(mid, 0.0)
        importance = sr.get("importance", 0.5)

        final_score = (
            recency_norm * recency_weight
            + vector_score * vector_weight
            + importance * importance_weight
        )

        seen[mid] = {**sr, "score": round(final_score, 4),
                     "recency_score": round(recency_norm, 4),
                     "vector_score": round(vector_score, 4)}

    # 如果向量搜索结果中有 SQLite 未覆盖的条目，也加入
    for vr in vector_results:
        mid = vr.get("memory_id", vr.get("id", ""))
        if mid not in seen:
            seen[mid] = {**vr, "score": vr.get("score", 0.0) * vector_weight}

    # 按最终分数降序排序
    fused = sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)
    return fused[:top_k]
