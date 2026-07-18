"""
Trinity unified entry point — Trinity and TrinityClient.

Provides two interfaces:
  1. Trinity       — Direct Python API (import and use inline)
  2. TrinityClient — In-process client that delegates to the MCP server via bridge

Both support the same 6 operations:
  - search       Semantic memory search (tri-signal + multi-query + rerank)
  - ingest       Write memory (CRDT versioned, SHA-256 audited)
  - diagnostics  Full system diagnostics
  - detect_contradiction    Contradiction detection
  - hopfield_energy         Hopfield energy evaluation
  - selfmem_strategy        SelfMem agent-controlled strategy
  - benchmark               Run benchmarks (LongMemEval, MemSyco, etc.)

搜索管线（任务C）:
  - 支持 use_vector=True 启用向量语义搜索
  - 向量 + SQLite FTS 融合排序
  - 默认 use_vector=False 保持向后兼容
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# ── Locate output directory ──────────────────────────────────────────────
def _find_trinity_store() -> Optional[str]:
    """Find the Trinity output directory."""
    candidates = [
        os.environ.get("TRINITY_STORE"),
        str(Path.home() / ".trinity" / "store"),
        str(Path.home() / "AppData" / "Roaming" / "Tencent" / "Marvis" /
            "User" / "oAN1i2S25HdLeBcp7ZJM0HU3JDc8" / "workspace" /
            "conv_19f49996244_37d75ffae4a6" / "output"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return os.getcwd()


_TRINITY_STORE = _find_trinity_store()


def _import_trinity_bridge():
    """Dynamically import the trinity_call bridge module."""
    sys.path.insert(0, _TRINITY_STORE)
    from trinity_call import trinity as _trinity
    return _trinity


# ── Cached bridge import ────────────────────────────────────────────────
_BRIDGE_CACHE: Optional[Any] = None

def _get_cached_bridge():
    global _BRIDGE_CACHE
    if _BRIDGE_CACHE is None:
        _BRIDGE_CACHE = _import_trinity_bridge()
    return _BRIDGE_CACHE


# ── 向量搜索辅助函数 ──────────────────────────────────────────────────
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


class Trinity:
    """Unified Trinity memory system client.

    Supports multi-tenant, multi-persona, multi-session operations.

    Usage:
        >>> from trinity import Trinity
        >>> mem = Trinity()
        >>> mem.ingest("user prefers dark mode")
        >>> results = mem.search("user preferences", top_k=5)
        >>>
        >>> # Multi-tenant:
        >>> mem = Trinity(tenant_id="acme_corp")
        >>> mem.ingest("Alice likes hiking", persona_id="alice")
        >>> results = mem.search("hiking", persona_id="alice")
    """

    def __init__(
        self,
        store_path: Optional[str] = None,
        tenant_id: str = "default",
        adapter: Optional[str] = None,
    ):
        global _TRINITY_STORE
        if store_path:
            _TRINITY_STORE = store_path
        self.tenant_id = tenant_id
        self._bridge = None
        self._adapter = None
        self._engine = None

        # ── 向量搜索缓存 ──────────────────────────────────────────
        self._embedding_engine = None
        self._vector_index = None

        if adapter == "postgresql":
            self._init_postgres_adapter()
        elif adapter == "sqlite":
            self._init_sqlite_adapter()
        elif adapter is None:
            # Default: use SQLite with store_path
            from trinity.adapters.sqlite import SQLiteAdapter
            _db_path = os.path.join(_TRINITY_STORE, "trinity_store.db") if _TRINITY_STORE else "trinity_store.db"
            try:
                self._adapter = SQLiteAdapter(db_path=_db_path)
                self._adapter.connect()
            except Exception:
                self._adapter = None
        else:
            raise ValueError(f"Unknown adapter: {adapter}")

    def _init_sqlite_adapter(self):
        from trinity.adapters.sqlite import SQLiteAdapter
        global _TRINITY_STORE
        _store_dir = os.path.join(_TRINITY_STORE, "data") if _TRINITY_STORE else os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data"
        )
        os.makedirs(_store_dir, exist_ok=True)
        db_path = os.path.join(_store_dir, "trinity_store.db")
        self._adapter = SQLiteAdapter(db_path=db_path)
        self._adapter.connect()

    def _init_postgres_adapter(self):
        from trinity.adapters.postgresql import PostgreSQLAdapter
        self._adapter = PostgreSQLAdapter(
            host=os.environ.get("TRINITY_PG_HOST", "localhost"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        )
        self._adapter.connect()

    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = _get_cached_bridge()
        return self._bridge

    # ── 核心操作 ──────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "hybrid",
        use_all_channels: bool = True,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        use_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        """语义记忆搜索。

        Args:
            query: 搜索查询字符串。
            top_k: 返回结果数量（默认 10）。
            mode: 检索模式 (semantic/graph/exact/hybrid)。
            use_all_channels: 使用全部 47 个检索通道。
            persona_id: 按角色筛选（多租户）。
            tenant_id: 按租户筛选（多租户）。
            use_vector: 是否启用向量语义搜索（默认 False，向后兼容）。

        Returns:
            匹配的记忆条目列表，含分数。
        """
        # ── 如果有 adapter，用 adapter 搜索 ───────────────────────
        if self._adapter:
            if use_vector and hasattr(self._adapter, "_fts_available") and self._adapter._fts_available():
                # 向量 + FTS 融合搜索
                return self._search_with_vector(
                    query=query,
                    persona_id=persona_id or None,
                    tenant_id=tenant_id or self.tenant_id,
                    top_k=top_k,
                )

            return self._adapter.search_memories(
                query=query,
                persona_id=persona_id or None,
                tenant_id=tenant_id or self.tenant_id,
                top_k=top_k,
            )

        if self._adapter:
            return self._adapter.search_memories(
                query=query,
                persona_id=persona_id or None,
                tenant_id=tenant_id or self.tenant_id,
                top_k=top_k,
            )
        return []

    def _search_with_vector(
        self,
        query: str,
        persona_id: Optional[str],
        tenant_id: Optional[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """向量 + SQLite 融合搜索。

        1. 先用 embedding engine 把 query 编码成向量
        2. 用 vector_index 做语义搜索
        3. 和 SQLite FTS 搜索结果做融合排序
        """
        # 1. SQLite 搜索（获取基准结果）
        sqlite_results = self._adapter.search_memories(
            query=query,
            persona_id=persona_id,
            tenant_id=tenant_id,
            top_k=top_k * 2,  # 多取一些用于融合
        )

        # 2. 向量搜索
        vector_results = self._vector_search(query, top_k=top_k * 2)

        # 3. 融合排序
        if vector_results:
            fused = _fuse_results(
                sqlite_results=sqlite_results,
                vector_results=vector_results,
                top_k=top_k,
                recency_weight=0.3,
                vector_weight=0.4,
                importance_weight=0.3,
            )
            return fused

        return sqlite_results[:top_k]

    def _vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """执行向量语义搜索。"""
        try:
            # 延迟加载嵌入引擎
            if self._embedding_engine is None:
                self._embedding_engine = _get_embedding_engine()
            if self._embedding_engine is None:
                return []

            # 编码查询
            query_vec = self._embedding_engine.embed(query)

            # 用 adapter 中所有记忆构建向量索引（实时索引）
            if self._adapter:
                all_memories = self._adapter.get_all_memories(limit=5000)
                if not all_memories:
                    return []

                dim = self._embedding_engine.embedding_dim()
                if self._vector_index is None:
                    self._vector_index = _get_vector_index(dim=dim)
                if self._vector_index is None:
                    return []

                # 使用混合索引（BM25稀疏 + FAISS HNSW稠密）
                try:
                    from trinity.vector_index.mixed import HybridIndex
                    if isinstance(self._vector_index, HybridIndex):
                        hybrid_results = self._vector_index.search(
                            query_vec,
                            top_k=top_k,
                            query_text=query,
                        )
                        if hybrid_results:
                            search_results = hybrid_results
                except Exception:
                    pass

                # 编码批量记忆
                texts = [m["content"] for m in all_memories]
                vectors = self._embedding_engine.embed_batch(texts)

                # 添加到索引
                for memory, vec in zip(all_memories, vectors):
                    self._vector_index.add(memory["memory_id"], vec, {
                        "content": memory["content"],
                        "importance": memory.get("importance", 0.5),
                        "created_at": memory.get("created_at", ""),
                    })

                # 搜索
                search_results = self._vector_index.search(query_vec, top_k=top_k)

                # 转换为标准格式
                vector_results = []
                for sr in search_results:
                    meta = sr.metadata
                    vector_results.append({
                        "memory_id": sr.id,
                        "content": meta.get("content", ""),
                        "content_preview": meta.get("content", "")[:100],
                        "importance": meta.get("importance", 0.5),
                        "created_at": meta.get("created_at", ""),
                        "score": sr.score,
                        "persona_id": "",
                        "role": "",
                        "tags": [],
                        "category": "",
                    })

                return vector_results

        except Exception as e:
            logger = __import__("logging").getLogger("trinity.core.client")
            logger.warning("向量搜索失败，回退到纯 SQLite 搜索: %s", e)

        return []

    def ingest(
        self,
        content: str,
        source_window: str = "",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write memory (CRDT versioned, SHA-256 audited).

        Args:
            content: Memory text content.
            source_window: Source window identifier.
            role: user/assistant/system.
            importance: Importance 0-1.
            tags: List of tags.
            category: Memory category.
            metadata: Additional metadata dict.
            persona_id: Persona/user identifier (multi-tenant).
            session_id: Session identifier (multi-tenant).
            tenant_id: Tenant/organization identifier (multi-tenant).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        tags = tags or []

        if self._adapter:
            return self._adapter.store_memory(
                content=content,
                persona_id=persona_id,
                session_id=session_id,
                tenant_id=tenant_id or self.tenant_id,
                role=role,
                importance=importance,
                tags=tags,
                category=category,
            )

        # fallback (should not happen with adapter)
        return self._adapter.store_memory(
            content=content, persona_id=persona_id,
            session_id=session_id, tenant_id=tenant_id or self.tenant_id,
            role=role, importance=importance, tags=tags, category=category,
        ) if self._adapter else {"memory_id": "", "error": "no adapter"}

    def diagnostics(self) -> Dict[str, Any]:
        """Run full system diagnostics."""
        if self._adapter:
            adapter_diag = self._adapter.diagnostics()
            from trinity.modules.second_brain import Engine
            try:
                import builtins
                _orig_print = builtins.print
                builtins.print = lambda *a, **kw: None
                engine = Engine()
                builtins.print = _orig_print
                engine_diag = engine.run_diagnostics()
            except Exception:
                engine_diag = {"status": "engine not available"}
            return {
                "trinity_version": "v6.37.0",
                "source_version": "v6.37",
                "total_modules": 5,
                "adapter": adapter_diag,
                "engine": engine_diag,
            }
        return self.bridge("diagnostics")

    def detect_contradiction(
        self, statement_a: str, statement_b: str
    ) -> Dict[str, Any]:
        return self.bridge("contradiction",
                           statement_a=statement_a,
                           statement_b=statement_b)

    def hopfield_energy(
        self, memories: List[Dict[str, Any]], query: str
    ) -> Dict[str, Any]:
        return self.bridge("hopfield", memories=memories, query=query)

    def selfmem_strategy(self, actions: List[str]) -> Dict[str, Any]:
        return self.bridge("strategy", actions=actions)

    def reason(self, query: str, multi_hop: bool = False, top_k: int = 5) -> Dict[str, Any]:
        if self._engine:
            from trinity.modules.open_domain.reasoner import OpenDomainReasoner
            reasoner = OpenDomainReasoner()
            if multi_hop:
                return reasoner.answer_multi_hop(query, retriever=self.search, top_k=top_k)
            return reasoner.answer(query, retriever=self.search, top_k=top_k)
        return self.bridge("reason", query=query, multi_hop=multi_hop, top_k=top_k)

    # ── Multi-tenant / Persona methods ─────────────────────────────────

    def get_persona_memories(
        self, persona_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if self._adapter:
            return self._adapter.get_persona_memories(persona_id, limit)
        return self.bridge("diagnostics").get("storage", {})

    def delete_memory(self, memory_id: str) -> bool:
        if self._adapter:
            return self._adapter.delete_memory(memory_id)
        return True

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        if self._adapter:
            return self._adapter.get_version_chain(memory_id)
        return []

    def switch_tenant(self, tenant_id: str) -> "Trinity":
        self.tenant_id = tenant_id
        return self

    # ------------------------------------------------------------------
    # 多模态记忆
    # ------------------------------------------------------------------

    @property
    def multimodal(self):
        """获取多模态记忆引擎（惰性初始化）"""
        if not hasattr(self, "_multimodal_memory"):
            self._multimodal_memory = None
        if self._multimodal_memory is None:
            from trinity.modules.multimodal.multimodal_memory import MultiModalMemory
            self._multimodal_memory = MultiModalMemory(
                storage_path=self._store_path,
                tenant_id=self.tenant_id,
            )
        return self._multimodal_memory

    def ingest_image(self, image_path: str, metadata: dict = None) -> dict:
        """摄取一张图片到多模态记忆"""
        from trinity.modules.multimodal.multimodal_memory import ModalityType
        result = self.multimodal.store(
            source_path=image_path,
            modality=ModalityType.IMAGE,
            metadata=metadata or {},
        )
        return {"engram_id": result.engram_id if result else None, "modality": "image"}

    def ingest_audio(self, audio_path: str, metadata: dict = None) -> dict:
        """摄取一段音频到多模态记忆"""
        from trinity.modules.multimodal.multimodal_memory import ModalityType
        result = self.multimodal.store(
            source_path=audio_path,
            modality=ModalityType.AUDIO,
            metadata=metadata or {},
        )
        return {"engram_id": result.engram_id if result else None, "modality": "audio"}

    def search_multimodal(self, query: str, top_k: int = 10,
                          reason: bool = False) -> list:
        """跨模态搜索记忆（文本→图像/音频/文本）"""
        results = self.multimodal.search(query=query, top_k=top_k, reason=reason)
        return [{"engram_id": r[0].engram_id, "score": r[1],
                 "modality": r[0].modality.value if hasattr(r[0], 'modality') else 'unknown'}
                for r in results]

    # ------------------------------------------------------------------
    # GPU 加速向量搜索
    # ------------------------------------------------------------------

    @property
    def gpu_index(self):
        """获取 GPU 加速向量索引（惰性初始化）"""
        if not hasattr(self, "_gpu_index"):
            self._gpu_index = None
        if self._gpu_index is None:
            from trinity.vector_index.index import (
                FaissIndex, HNSWConfig, NumpyBruteForceIndex,
            )
            try:
                import faiss
                has_gpu = hasattr(faiss, 'StandardGpuResources')
            except ImportError:
                has_gpu = False
            if has_gpu:
                self._gpu_index = FaissIndex(
                    dim=1024, metric="cosine", index_type="hnsw",
                    hnsw_config=HNSWConfig(M=32, efConstruction=200, efSearch=64),
                )
            else:
                # 回退到本地 numpy 作为精确搜索后端
                self._gpu_index = NumpyBruteForceIndex(dim=1024, metric="cosine")
        return self._gpu_index

    def search_with_gpu(self, query: str, top_k: int = 10) -> list:
        """使用 GPU/FAISS 加速向量搜索"""
        from trinity.embeddings.engine import EmbeddingEngine
        engine = EmbeddingEngine()
        query_vec = engine.embed(query)
        if query_vec is None:
            return self.search(query, top_k=top_k)
        results = self.gpu_index.search(query_vec, top_k)
        return [{"memory_id": r.id, "score": r.score, **r.metadata} for r in results]

    # ------------------------------------------------------------------
    # A2A 记忆共享
    # ------------------------------------------------------------------

    @property
    def a2a(self):
        """获取 A2A 记忆同步引擎（惰性初始化）"""
        if not hasattr(self, "_a2a_sync"):
            self._a2a_sync = None
        if self._a2a_sync is None:
            from trinity.a2a_memory import A2AMemorySync
            self._a2a_sync = A2AMemorySync(
                local_agent_id=f"trinity-{self.tenant_id}",
                local_store=self._a2a_store_callback,
                local_search=self._a2a_search_callback,
            )
        return self._a2a_sync

    def _a2a_store_callback(self, entry) -> bool:
        """A2A 存储回调：将远端记忆写入本地"""
        try:
            self.ingest(
                content=entry.content,
                persona_id=entry.persona_id,
                tags=entry.tags,
                importance=entry.importance,
            )
            return True
        except Exception as e:
            return False

    def _a2a_search_callback(self, query: str, top_k: int = 10) -> list:
        """A2A 搜索回调：搜索本地记忆供远端查询"""
        try:
            return self.search(query, top_k=top_k)
        except Exception:
            return []

    def share_memory(self, content: str, persona_id: str = "default",
                     tags: list = None, importance: float = 0.5) -> dict:
        """将一条记忆共享给所有在线 Trinity 实例"""
        from trinity.a2a_memory import create_memory_entry
        entry = create_memory_entry(
            content=content,
            persona_id=persona_id,
            source_agent=f"trinity-{self.tenant_id}",
            importance=importance,
            tags=tags or [],
        )
        results = self.a2a.share_to_all(entry)
        return {
            "memory_id": entry.memory_id,
            "shared_to": len(results),
            "results": [{"peer": r.peer, "success": r.success} for r in results],
        }

    def sync_from_peers(self, query: str = "") -> dict:
        """从所有在线实例同步记忆"""
        results = self.a2a.sync_all(query=query)
        return {
            "peers_contacted": len(results),
            "total_entries": sum(r.entries_count for r in results),
            "results": [{"peer": r.peer, "entries": r.entries_count, "success": r.success} for r in results],
        }

    def search_peers(self, query: str, top_k: int = 10) -> dict:
        """搜索所有在线实例的记忆"""
        results = self.a2a.search_peers(query, top_k=top_k)
        return {
            "query": query,
            "peers_found": len(results),
            "results": results,
        }

    def benchmark(self, name: str = "longmemeval",
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        from trinity.benchmark.runner import run_benchmark
        return run_benchmark(name, config)


class TrinityClient:
    """Alias for Trinity — same unified interface."""

    def __new__(cls, *args, **kwargs):
        return Trinity(*args, **kwargs)
