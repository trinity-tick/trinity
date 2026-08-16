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

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced


# ── Locate output directory ──────────────────────────────────────────────
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
        use_ann: bool = False,
        evolution_enabled: bool = True,
    ):
        global _TRINITY_STORE
        if store_path:
            _TRINITY_STORE = store_path
        self.tenant_id = tenant_id
        self._bridge = None
        self._adapter = None
        self._engine = None

        # ── 自进化记忆系统 ──────────────────────────────────────────
        self.evolution_enabled = evolution_enabled
        self._scheduler = None

        # ── 写入加工管线串行锁（2026-08-15 二轮压测修复）────────
        # postprocess 后台线程化后，8 并发加工线程同时 sklearn fit +
        # 抢 _write_lock 会拖垮写入线程（GIL 风暴 + 锁饥饿，实测响应
        # p95 3.7s）。加工是后台异步工作，本就无需并发：全局串行化，
        # 同一时刻至多一个加工线程（embedding 引擎也只 fit 一次）。
        import threading as _thr
        self._postprocess_lock = _thr.Lock()

        # ── 分层检索配置 ──────────────────────────────────────────
        self.half_life_days: float = 7.0       # 时间衰减半衰期（天）
        self.agent_weight_default: float = 1.0  # 未配置 Agent 的默认权重
        self.push_half_life_days: float = 30.0  # 推送记忆时间衰减半衰期（天）

        # ── 向量搜索缓存 ──────────────────────────────────────────
        self._embedding_engine = None
        self._vector_index = None
        self._ann_index = None
        # 性能（2026-08-15）：ANN 索引持久化缓存（版本键：维度+条数+最新updated_at）。
        # 此前每次 use_ann 搜索都全量编码+重建索引——缓存后首次构建、后续直查。
        self._ann_cache = None
        # ①落盘持久化（2026-08-15）：索引 save/load 到 ~/.trinity/data/ann_index.bin，
        # 跨进程/重启免 30s 重建；写入增量维护（脏计数阈值触发 save）。
        self._ann_index_path = os.path.join(
            os.path.expanduser("~/.trinity"), "data", "ann_index.bin"
        )
        self._ann_dirty = 0

        # ── ANN 配置 ──────────────────────────────────────────────
        self.use_ann: bool = use_ann  # 启用 hnswlib/FAISS HNSW ANN 索引

        # ── 混合检索（向量 + BM25 + 图谱）─────────────────────────
        self._hybrid_retriever = None
        self._bm25_index = None
        self._bm25_ready = False  # 后台预构建完成标记（2026-08-15）
        self._bm25_lock = _thr.Lock()  # 构建原子化（2026-08-15 二轮）

        # ── 跨模态检索（文字 ↔ 图片记忆）─────────────────────────
        self._cross_modal_retriever = None

        # ── 个性化引擎（PAHF 双反馈, R3 P0-2, 2026-08-15）────────
        # 惰性实例化；TRINITY_PERSONALIZE=on 时 search 注入偏好上下文。
        self._personalization = None

        # ── SAGE 自进化图记忆（R5 P0, 2026-08-15, MindMemOS 对齐）──
        # 惰性实例化；写入时可同步图记忆、查询图证据路径、触发自进化。
        self._sage = None

        # ── DCPM 双过程认知记忆（R5 P0, 2026-08-15, Dual-Process 对齐）
        # 惰性实例化；System1 信念修订链 + System2 夜间 schema 归纳。
        self._dcpm = None

        # ── 记忆压缩引擎 ──────────────────────────────────────────
        self._compressor = None

        if adapter == "postgresql":
            self._init_postgres_adapter()
        elif adapter == "sqlite":
            self._init_sqlite_adapter()
        elif adapter is None:
            # Default: use SQLite with store_path, but honor TRINITY_DB_PATH env var.
            # store_path 语义：目录 → 内部生成 trinity_store.db；已是 .db 文件 → 直接使用。
            from trinity.adapters.sqlite import SQLiteAdapter
            _db_path = os.environ.get("TRINITY_DB_PATH")
            if not _db_path:
                if _TRINITY_STORE and os.path.isfile(_TRINITY_STORE):
                    _db_path = _TRINITY_STORE
                elif _TRINITY_STORE:
                    _db_path = os.path.join(_TRINITY_STORE, "trinity_store.db")
                else:
                    # 2026-08-15：不再用相对路径（曾落到 cwd 小库），固定权威路径
                    _db_path = os.path.join(
                        str(Path.home() / ".trinity" / "store"), "trinity_store.db"
                    )
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
        # 统一到权威大库（~/.trinity/store/trinity_store.db），不再拼 data/ 子目录
        # （2026-08-15：曾生成 ~/.trinity/store/data/trinity_store.db 等小库，双库并存）
        _store_dir = _TRINITY_STORE or os.path.join(
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

    @traced("memory.search")
    def search(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "hybrid",
        use_all_channels: bool = True,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        use_vector: bool = False,
        agent_weight: Optional[float] = None,
        ranked: bool = False,
        modality: Optional[str] = None,
        dedup_by_session: bool = False,
    ) -> Dict[str, Any]:
        """语义记忆搜索。

        Args:
            query: 搜索查询字符串。
            top_k: 返回结果数量（默认 10）。
            mode: 检索模式 (semantic/graph/exact/hybrid)。
            use_all_channels: 使用全部 47 个检索通道。
            persona_id: 按角色筛选（多租户）。
            tenant_id: 按租户筛选（多租户）。
            agent_id: 按Agent筛选（命名空间隔离）。
            app_id: 按应用筛选（多范围ACL）。
            session_id: 按会话筛选。
            category: 按记忆类别筛选（episodic/semantic/procedural）。
            use_vector: 是否启用向量语义搜索（默认 False，向后兼容）。
            agent_weight: 调用方指定的 Agent 权重值，覆盖存储层配置。
            ranked: 是否启用三层分层排序（语义 / 时间衰减 / Agent 权重）。

        Returns:
            Dict with 'results' (匹配条目列表) and 'pushed_memories' (主动推送列表)。
        """
        raw_results: List[Dict[str, Any]] = []

        if self._adapter:
            _mode = (mode or "hybrid").lower()
            # ── 真实 mode 路由（GEN-2，修复"mode 参数装饰性"）──────────
            #   keyword/exact → FTS5 关键词（保持默认行为）
            #   semantic       → 向量检索（可用时）；不可用回退 FTS5
            #   hybrid         → 47 通道融合（仅当 hybrid retriever 已初始化，
            #                    否则回退 FTS5 以保持默认路径不变）
            #   graph          → 图谱检索（adapter 支持时）；否则回退 FTS5
            _vector_available = (
                hasattr(self._adapter, "_fts_available")
                and self._adapter._fts_available()
            )
            _use_hybrid = (
                _mode == "hybrid"
                and self._hybrid_retriever is not None
            )
            _use_graph = (
                _mode in ("graph", "hybrid")
                and hasattr(self._adapter, "search_graph")
            )
            if _mode == "semantic" and (use_vector or _vector_available):
                try:
                    raw_results = self._search_with_vector(
                        query=query,
                        persona_id=persona_id or None,
                        tenant_id=tenant_id or self.tenant_id,
                        agent_id=agent_id or None,
                        top_k=top_k,
                    )
                except Exception:
                    # 向量路径不可用 → 回退 FTS5
                    raw_results = self._adapter.search_memories(
                        query=query,
                        persona_id=persona_id or None,
                        tenant_id=tenant_id or self.tenant_id,
                        agent_id=agent_id or None,
                        app_id=app_id,
                        session_id=session_id,
                        category=category,
                        top_k=top_k,
                    )
            elif _use_hybrid:
                try:
                    raw_results = self.search_hybrid(
                        query=query, top_k=top_k, strategy="fusion",
                        agent_id=agent_id, persona_id=persona_id,
                        tenant_id=tenant_id,
                    ).get("results", [])
                except Exception:
                    raw_results = []
            elif _use_graph:
                try:
                    raw_results = self._adapter.search_graph(
                        query=query, top_k=top_k,
                        persona_id=persona_id or None,
                        tenant_id=tenant_id or self.tenant_id,
                    )
                except Exception:
                    raw_results = []
            else:
                raw_results = self._adapter.search_memories(
                    query=query,
                    persona_id=persona_id or None,
                    tenant_id=tenant_id or self.tenant_id,
                    agent_id=agent_id or None,
                    app_id=app_id,
                    session_id=session_id,
                    category=category,
                    top_k=top_k,
                )

        # modality 过滤
        if modality and raw_results:
            raw_results = [m for m in raw_results if m.get("modality") == modality]

        # 多会话检索优化（2026-08-15）：按 session 聚合去重——同一会话只保留
        # 相关性最高的一条，使跨会话答案进入前 top_k（MS/长程召回提升；
        # LongMemEval 500q MS top_k=10 后 R@5 0.525→0.95，会话均衡是其主因之一）。
        if dedup_by_session and raw_results:
            seen_sessions = set()
            deduped = []
            for m in raw_results:
                sid = m.get("session_id") or "default"
                if sid in seen_sessions:
                    continue
                seen_sessions.add(sid)
                deduped.append(m)
                if len(deduped) >= top_k:
                    break
            raw_results = deduped

        if ranked and raw_results:
            raw_results = self._apply_layered_ranking(
                raw_results=raw_results,
                top_k=top_k,
                agent_weight=agent_weight,
            )

        # 收集本次搜索结果中的记忆 ID，进行主动推送
        memory_ids = [m.get("memory_id", "") for m in raw_results if m.get("memory_id")]
        pushed = self.proactive_push(memory_ids)

        # 自动审计日志
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=None, action="search", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"query": query, "top_k": top_k, "mode": mode,
                             "hits": len(raw_results), "memory_ids": memory_ids[:10]},
                )
            except Exception:
                pass

        return {
            "results": raw_results,
            "pushed_memories": pushed,
        }

    # ── 分层检索（Multi-Stage Ranking）──────────────────────────────

    def _apply_layered_ranking(
        self,
        raw_results: List[Dict[str, Any]],
        top_k: int,
        agent_weight: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """三层排序管线：语义分数 → 时间衰减 → Agent 权重。

        Layer 1 — 语义相似度：复用 raw_results 中的 score 字段。
        Layer 2 — 时间衰减：decay = 2^(-days_since_creation / half_life_days)。
        Layer 3 — Agent 权重：查询存储层的 agent_weights 配置。

        Args:
            raw_results: 基础搜索结果列表。
            top_k: 返回结果数量。
            agent_weight: 可选，调用方指定的权重，优先于存储层配置。

        Returns:
            排序后的结果，每条含 final_score 与 layer_scores 明细。
        """
        import math
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        half_life_days = float(self.half_life_days)

        # 读取 Agent 权重配置
        weights: Dict[str, float] = {}
        if self._adapter and hasattr(self._adapter, "get_agent_weights"):
            weights = self._adapter.get_agent_weights()

        ranked = []
        for item in raw_results:
            # ── Layer 1: 语义分数 ────────────────────────────────
            semantic_score = float(item.get("score", 0.5))

            # ── Layer 2: 时间衰减 ────────────────────────────────
            time_decay_score = 1.0
            created_at = item.get("created_at", "")
            if created_at:
                try:
                    if isinstance(created_at, str):
                        # 处理多种时间格式
                        ts = created_at
                        for fmt in [
                            "%Y-%m-%dT%H:%M:%S.%f%z",
                            "%Y-%m-%dT%H:%M:%S%z",
                            "%Y-%m-%dT%H:%M:%S.%f",
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S.%f",
                            "%Y-%m-%d %H:%M:%S",
                        ]:
                            try:
                                dt = datetime.strptime(ts, fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            dt = None
                    else:
                        dt = created_at

                    if dt:
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        days_since = (now - dt).total_seconds() / 86400.0
                        if half_life_days > 0:
                            time_decay_score = math.pow(
                                2, -days_since / half_life_days
                            )
                        else:
                            time_decay_score = 1.0
                except Exception:
                    time_decay_score = 1.0

            # ── Layer 3: Agent 权重 ──────────────────────────────
            item_agent_id = item.get("agent_id", "default")
            if agent_weight is not None:
                agent_weight_score = float(agent_weight)
            elif item_agent_id in weights:
                agent_weight_score = float(weights[item_agent_id])
            else:
                agent_weight_score = float(self.agent_weight_default)

            # ── Layer 4: 模态加权 ────────────────────────────────
            modality_weights = {
                "code": 1.2,
                "trace": 1.1,
                "text": 1.0,
                "image_description": 0.9,
            }
            modality_weight = modality_weights.get(
                item.get("modality", "text"), 1.0
            )

            # ── 综合得分 ─────────────────────────────────────────
            final_score = (
                semantic_score * time_decay_score * agent_weight_score * modality_weight
            )

            ranked.append({
                **item,
                "final_score": round(final_score, 6),
                "layer_scores": {
                    "semantic_score": round(semantic_score, 6),
                    "time_decay_score": round(time_decay_score, 6),
                    "agent_weight_score": round(agent_weight_score, 4),
                    "modality_weight": round(modality_weight, 4),
                },
            })

        ranked.sort(key=lambda x: x["final_score"], reverse=True)
        return ranked[:top_k]

    # ── 多模态便捷方法 ─────────────────────────────────────────────

    def ingest_code(
        self,
        content: str,
        language: str = "python",
        file_path: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入代码记忆，自动提取语言/函数名/imports 等元数据。

        Args:
            content: 代码文本。
            language: 编程语言（python/javascript/go/rust 等）。
            file_path: 源代码文件路径（可选）。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        from trinity.core.code_analyzer import analyze_code

        analysis = analyze_code(content, language)
        metadata = {
            "language": language,
            "functions": analysis.get("functions", []),
            "imports": analysis.get("imports", []),
            "classes": analysis.get("classes", []),
            "loc": analysis.get("loc", len(content.splitlines())),
        }

        return self.ingest(
            content=content,
            modality="code",
            metadata=metadata,
            source_uri=file_path,
            **kwargs,
        )

    def ingest_image_description(
        self,
        description: str,
        image_source: Optional[str] = None,
        image_dimensions: Optional[Dict[str, int]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入图片描述记忆。

        Args:
            description: 图片的文字描述。
            image_source: 图片来源 URL 或本地路径。
            image_dimensions: {"width": 1920, "height": 1080} 格式的尺寸信息。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        metadata = {"source": image_source} if image_source else {}
        if image_dimensions:
            metadata["dimensions"] = image_dimensions

        return self.ingest(
            content=description,
            modality="image_description",
            metadata=metadata,
            source_uri=image_source,
            **kwargs,
        )

    def ingest_trace(
        self,
        steps: List[str],
        task_name: str = "",
        elapsed_seconds: Optional[float] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入执行轨迹记忆。

        Args:
            steps: 步骤描述列表，如 ['Step 1: 读取文件', 'Step 2: 解析 JSON']。
            task_name: 任务名称。
            elapsed_seconds: 总耗时（秒）。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        content = f"[Trace] {task_name}\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
        metadata = {
            "step_count": len(steps),
            "task_name": task_name,
        }
        if elapsed_seconds is not None:
            metadata["elapsed_seconds"] = elapsed_seconds

        return self.ingest(
            content=content,
            modality="trace",
            metadata=metadata,
            **kwargs,
        )

    def set_agent_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """设置 Agent 检索权重。

        Args:
            agent_id: Agent 标识（如 'file-agent'、'browser'）。
            weight: 权重值（建议 0.1-2.0）。

        Returns:
            操作结果。
        """
        if self._adapter and hasattr(self._adapter, "set_agent_weight"):
            return self._adapter.set_agent_weight(agent_id, weight)
        return {"error": "Adapter does not support agent weights"}

    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 权重配置。

        Returns:
            Dict[agent_id, weight]
        """
        if self._adapter and hasattr(self._adapter, "get_agent_weights"):
            return self._adapter.get_agent_weights()
        return {}

    def delete_agent_weight(self, agent_id: str) -> bool:
        """删除 Agent 权重配置。

        Args:
            agent_id: Agent 标识。

        Returns:
            是否删除成功。
        """
        if self._adapter and hasattr(self._adapter, "delete_agent_weight"):
            return self._adapter.delete_agent_weight(agent_id)
        return False

    # ── 主动记忆推送（Proactive Memory Push）──────────────────────

    def proactive_push(
        self,
        memory_ids: List[str],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """基于当前上下文记忆，主动推送关联记忆。

        策略：
        1. 收集本次涉及的所有记忆 ID。
        2. 查询这些记忆的 memory_links，按 strength 降序取 top-k。
        3. 排除已在请求记忆列表中的条目，去重。
        4. 应用时间衰减（半衰期 30 天）。

        Args:
            memory_ids: 当前上下文中的记忆 ID 列表。
            top_k: 最大推送条数（默认 5）。

        Returns:
            推送记忆列表，每条含 push_reason (link_type + strength) 和 push_score。
        """
        import math
        from datetime import datetime, timezone

        if not self._adapter or not hasattr(self._adapter, "get_linked_memories"):
            return []

        seen: set = set(memory_ids)
        pushed: Dict[str, Dict[str, Any]] = {}
        now = datetime.now(timezone.utc)

        for mid in memory_ids:
            if not mid:
                continue
            try:
                links = self._adapter.get_linked_memories(mid, min_strength=0.3)
            except Exception:
                continue
            for link in links:
                target_id = link.get("target_id", "")
                if not target_id or target_id in seen:
                    continue
                # 2026-08-15（二轮压测修复）：strength 可能为 None
                # （旧数据/部分链接未填）→ float(None) 崩溃；兜底 0.5。
                strength = float(link.get("strength") or 0.5)
                link_type = link.get("link_type", "semantic")

                # 时间衰减
                push_score = float(strength)
                created_at = link.get("created_at", "")
                if created_at:
                    try:
                        if isinstance(created_at, str):
                            for fmt in [
                                "%Y-%m-%dT%H:%M:%S.%f%z",
                                "%Y-%m-%dT%H:%M:%S%z",
                                "%Y-%m-%dT%H:%M:%S.%f",
                                "%Y-%m-%dT%H:%M:%S",
                                "%Y-%m-%d %H:%M:%S.%f",
                                "%Y-%m-%d %H:%M:%S",
                            ]:
                                try:
                                    dt = datetime.strptime(created_at, fmt)
                                    break
                                except ValueError:
                                    continue
                            else:
                                dt = None
                        else:
                            dt = created_at
                        if dt:
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            days_since = (now - dt).total_seconds() / 86400.0
                            if self.push_half_life_days > 0:
                                push_score *= math.pow(
                                    2, -days_since / self.push_half_life_days
                                )
                    except Exception:
                        pass

                # 优先保留强度更高的
                if target_id not in pushed or push_score > pushed[target_id].get("push_score", 0):
                    pushed[target_id] = {
                        "memory_id": target_id,
                        "target_content": link.get("target_content", ""),
                        "push_reason": {
                            "link_type": link_type,
                            "strength": strength,
                            "source_id": mid,
                        },
                        "push_score": round(push_score, 6),
                    }

        # 按 push_score 降序，取 top_k
        sorted_pushed = sorted(
            pushed.values(), key=lambda x: x["push_score"], reverse=True
        )
        return sorted_pushed[:top_k]

    # ── 记忆链接管理 ──────────────────────────────────────────────

    def create_link(
        self, source_id: str, target_id: str,
        link_type: str = "semantic", strength: float = 0.5,
    ) -> Dict[str, Any]:
        """创建记忆关联链接。

        Args:
            source_id: 源记忆 ID。
            target_id: 目标记忆 ID。
            link_type: 链接类型（co_occurrence/semantic/causal/same_task）。
            strength: 关联强度 0-1。

        Returns:
            创建结果。
        """
        if self._adapter and hasattr(self._adapter, "create_memory_link"):
            return self._adapter.create_memory_link(
                source_id, target_id, link_type, strength,
            )
        return {"error": "Adapter does not support memory links"}

    def get_links(self, memory_id: str) -> Dict[str, Any]:
        """获取某记忆的完整关联网络（含双向链接）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            Dict with outgoing/incoming lists.
        """
        if self._adapter and hasattr(self._adapter, "get_all_links"):
            return self._adapter.get_all_links(memory_id)
        return {"outgoing": [], "incoming": []}

    def delete_link(self, link_id: str) -> bool:
        """删除指定链接。

        Args:
            link_id: 链接 ID。

        Returns:
            是否删除成功。
        """
        if self._adapter and hasattr(self._adapter, "delete_memory_link"):
            return self._adapter.delete_memory_link(link_id)
        return False

    def adjust_link_strength(
        self, link_id: str, action: str = "strengthen", delta: float = 0.1,
    ) -> Dict[str, Any]:
        """调整链接强度。

        Args:
            link_id: 链接 ID。
            action: 'strengthen' 或 'weaken'。
            delta: 调整幅度（默认 0.1）。

        Returns:
            操作结果。
        """
        if not self._adapter:
            return {"error": "No adapter"}
        if action == "strengthen" and hasattr(self._adapter, "strengthen_link"):
            return self._adapter.strengthen_link(link_id, delta)
        elif action == "weaken" and hasattr(self._adapter, "weaken_link"):
            return self._adapter.weaken_link(link_id, delta)
        return {"error": f"Invalid action: {action}"}

    # ── 记忆图谱 ──────────────────────────────────────────────────

    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体。

        Args:
            name: 实体名称。
            etype: 类型 (person/project/file/agent/task/concept/tag)。
            properties: 附加属性 JSON。

        Returns:
            Dict with id/name/type/properties/created_at.
        """
        if self._adapter and hasattr(self._adapter, "upsert_entity"):
            return self._adapter.upsert_entity(name, etype, properties)
        return self.bridge("upsert_entity", name=name, etype=etype, properties=properties)

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联关系）。"""
        if self._adapter and hasattr(self._adapter, "get_entity"):
            return self._adapter.get_entity(entity_id)
        return self.bridge("get_entity", entity_id=entity_id)

    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体。"""
        if self._adapter and hasattr(self._adapter, "search_entities"):
            return self._adapter.search_entities(name, etype, limit)
        return self.bridge("search_entities", name=name, etype=etype, limit=limit)

    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建关系（幂等去重）。"""
        if self._adapter and hasattr(self._adapter, "create_relation"):
            return self._adapter.create_relation(subject_id, predicate, object_id, properties)
        return self.bridge("create_relation",
                           subject_id=subject_id, predicate=predicate,
                           object_id=object_id, properties=properties)

    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系。"""
        if self._adapter and hasattr(self._adapter, "query_relations"):
            return self._adapter.query_relations(subject_id, predicate, object_id, limit)
        return self.bridge("query_relations",
                           subject_id=subject_id, predicate=predicate,
                           object_id=object_id, limit=limit)

    def traverse(self, start_id: str,
                 max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历子图。

        Returns:
            Dict with nodes/edges.
        """
        if self._adapter and hasattr(self._adapter, "traverse"):
            return self._adapter.traverse(start_id, max_hops)
        return self.bridge("traverse", start_id=start_id, max_hops=max_hops)

    def explore_topic(self, topic_name: str) -> Dict[str, Any]:
        """以主题词为入口，自动搜索实体 + 遍历关系 + 聚合知识卡片。

        流程：
        1. 搜索实体（匹配 topic_name）
        2. 对每个实体 expand 1 跳关系
        3. 聚合关联记忆
        4. 返回结构化知识卡片

        Returns:
            Dict with entities/relations/related_memories/summary.
        """
        entities = self.search_entities(name=topic_name, limit=5)

        all_entities: Dict[str, Dict] = {}
        all_relations: List[Dict] = []
        for ent in entities:
            eid = ent["id"]
            all_entities[eid] = ent
            sub_graph = self.traverse(eid, max_hops=1)
            for n in sub_graph.get("nodes", []):
                if n.get("id"):
                    all_entities.setdefault(n["id"], n)
            for e in sub_graph.get("edges", []):
                if e not in all_relations:
                    all_relations.append(e)

        # 聚合关联记忆
        related_memories: List[Dict] = []
        if self._adapter and hasattr(self._adapter, "search_memories"):
            for eid in all_entities:
                ent_data = all_entities.get(eid, {})
                ent_name = ent_data.get("name", "")
                if ent_name:
                    mems = self._adapter.search_memories(
                        query=ent_name, top_k=3,
                        persona_id=None, tenant_id=None, agent_id=None,
                    )
                    for m in mems:
                        mid = m.get("memory_id", "")
                        if mid and not any(
                            x.get("memory_id") == mid for x in related_memories
                        ):
                            related_memories.append(m)

        return {
            "topic": topic_name,
            "entities": list(all_entities.values()),
            "relations": all_relations,
            "related_memories": related_memories[:10],
            "summary": (
                f"知识图谱: {topic_name} — "
                f"{len(all_entities)} 个实体, "
                f"{len(all_relations)} 条关系, "
                f"{len(related_memories[:10])} 条关联记忆"
            ),
        }

    def _search_with_vector(
        self,
        query: str,
        persona_id: Optional[str],
        tenant_id: Optional[str],
        agent_id: Optional[str],
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
            agent_id=agent_id,
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

            # 编码查询（进程内向量缓存：同 query 免重复编码，2026-08-15）
            if not hasattr(self, "_query_vec_cache"):
                self._query_vec_cache = {}
            qhash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
            query_vec = self._query_vec_cache.get(qhash)
            if query_vec is None:
                query_vec = self._embedding_engine.embed(query)
                if len(self._query_vec_cache) > 200:
                    self._query_vec_cache.clear()
                self._query_vec_cache[qhash] = query_vec

            # 用 adapter 中所有记忆构建向量索引（实时索引）
            if self._adapter:
                dim = self._embedding_engine.embedding_dim()

                # ── ANN 路径（持久缓存 + 磁盘加载 + 后台预热，2026-08-15）──
                if self.use_ann:
                    if self._ann_cache is not None:
                        return self._vector_search_ann(query_vec, top_k, dim)
                    # 优先磁盘索引（跨进程/重启免重建）；否则后台构建+本次降级 FTS
                    if self._try_load_ann_from_disk(dim):
                        return self._vector_search_ann(query_vec, top_k, dim)
                    self._ensure_ann_background()
                    return self._adapter.search_memories(
                        query=query, top_k=top_k,
                    ) if self._adapter else []

                # 非 ANN 路径才在此拉全量
                # 2026-08-15：上限 5000 只覆盖 11.7k 大库的 42%——提到全量，
                # 避免向量召回盲区（11.7k 规模全量编码/建索引仍在可接受范围）。
                all_memories = self._adapter.get_all_memories(limit=20000)
                if not all_memories:
                    return []

                # ── 传统 vector_index 路径 ─────────────────────────
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

    def _get_ann_index(self, dim: int):
        """获取或创建 ANN 索引实例（延迟初始化）。"""
        if self._ann_index is None:
            from trinity.retrieval.ann_index import ANNIndex
            self._ann_index = ANNIndex(
                dim=dim,
                space="cosine",
                max_elements=100000,
                M=16,
                ef_construction=200,
            )
        return self._ann_index

    def _ensure_ann_background(self) -> None:
        """后台线程预热 ANN 索引（2026-08-15）：首次构建约 30s（全量编码），
        放后台避免首查阻塞；构建完成前 use_ann 查询降级走 FTS。"""
        if getattr(self, "_ann_building", False):
            return
        self._ann_building = True
        import threading
        t = threading.Thread(target=self._build_ann_in_background, daemon=True)
        t.start()

    def _build_ann_in_background(self) -> None:
        import time as _time
        try:
            if self._embedding_engine is None:
                self._embedding_engine = _get_embedding_engine()
            if self._embedding_engine is None:
                return
            all_memories = self._adapter.get_all_memories(limit=20000) if self._adapter else []
            if not all_memories:
                return
            dim = self._embedding_engine.embedding_dim()
            ann = self._get_ann_index(dim)
            texts = [m["content"] for m in all_memories]
            vectors = self._embedding_engine.embed_batch(texts)
            mem_ids = [m["memory_id"] for m in all_memories]
            ann.add_vectors(mem_ids, vectors)
            mem_map = {m["memory_id"]: m for m in all_memories}
            max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            self._ann_cache = ((dim, len(all_memories), max_upd), mem_map, _time.time())
            # 落盘持久化（跨进程/重启免重建）
            try:
                os.makedirs(os.path.dirname(self._ann_index_path) or ".", exist_ok=True)
                ann.save(self._ann_index_path)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("ANN index save failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 构建失败则下次查询再试
            logger = sys.modules.get("logging", None)
            if logger:
                logging.getLogger(__name__).warning("ANN background build failed: %s", exc)
        finally:
            self._ann_building = False

    def _try_load_ann_from_disk(self, dim: int) -> bool:
        """启动/首次查询时从磁盘加载 ANN 索引（免全量编码重建）。

        成功 → 填充 _ann_cache（mem_map 拉全量一次 ~160ms，远快于 30s 编码）。
        """
        import time as _time
        try:
            meta_path = self._ann_index_path + ".meta.json"
            if not os.path.exists(meta_path):
                return False
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("dim") != dim or not meta.get("size"):
                return False
            self._ann_index = None
            ann = self._get_ann_index(dim)
            ann.load(self._ann_index_path)
            all_memories = self._adapter.get_all_memories(limit=20000) if self._adapter else []
            if not all_memories:
                return False
            mem_map = {m["memory_id"]: m for m in all_memories}
            max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            self._ann_cache = ((dim, len(all_memories), max_upd), mem_map, _time.time())
            return True
        except Exception:  # noqa: BLE001
            self._ann_cache = None
            return False

    def _ann_incremental_add(self, memory_id: str, content: str) -> None:
        """ANN 增量维护：新/更新记忆写入后同步进索引（若已构建且 use_ann）。

        后台线程调用；embed 单条约 380ms 不影响写路径；脏计数阈值触发 save。
        """
        try:
            if self._ann_cache is None or not self.use_ann or not content or not memory_id:
                return
            if self._embedding_engine is None:
                self._embedding_engine = _get_embedding_engine()
            if self._embedding_engine is None:
                return
            dim = self._embedding_engine.embedding_dim()
            ann = self._get_ann_index(dim)
            if memory_id in self._ann_cache[1]:
                try:
                    ann.remove_vector(memory_id)
                except Exception:  # noqa: BLE001
                    pass
            vec = self._embedding_engine.embed(content)
            ann.add_vectors([memory_id], [vec])
            self._ann_cache[1][memory_id] = {
                "memory_id": memory_id, "content": content,
                "content_preview": content[:100], "importance": 0.5,
                "created_at": "", "score": 0.0,
            }
            self._ann_dirty += 1
            if self._ann_dirty >= 20:
                self._ann_dirty = 0
                try:
                    ann.save(self._ann_index_path)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _ann_incremental_remove(self, memory_id: str) -> None:
        """ANN 增量维护：删除记忆时从索引移除。"""
        try:
            if self._ann_cache is None or not memory_id:
                return
            if memory_id not in self._ann_cache[1]:
                return
            dim = self._ann_cache[0][0]
            ann = self._get_ann_index(dim)
            try:
                ann.remove_vector(memory_id)
            except Exception:  # noqa: BLE001
                pass
            self._ann_cache[1].pop(memory_id, None)
            self._ann_dirty += 1
        except Exception:  # noqa: BLE001
            pass

    def _vector_search_ann(
        self,
        query_vec: Any,  # np.ndarray
        top_k: int,
        dim: int,
    ) -> List[Dict[str, Any]]:
        """使用 ANNIndex 执行向量语义搜索（索引持久化缓存，2026-08-15）。

        版本键 = (dim, 记忆条数, 最新 updated_at)，TTL=60s：
          - 缓存命中且未过期 → 直接 ANN 搜索（不再拉全量/编码/重建）；
          - 未命中/过期 → 拉全量 + 编码 + 重建索引 + 写缓存。
        此前每次 use_ann 搜索都全量编码+重建（毫秒级查询 → 秒级）。
        """
        import time as _time

        ann = self._get_ann_index(dim)
        ttl = 60.0

        if self._ann_cache is not None and (_time.time() - self._ann_cache[2]) < ttl:
            mem_map = self._ann_cache[1]
        else:
            all_memories = self._adapter.get_all_memories(limit=20000) if self._adapter else []
            if not all_memories:
                return []
            # 重建干净索引（旧索引含过期向量，置 None 强制新实例）
            self._ann_index = None
            ann = self._get_ann_index(dim)
            texts = [m["content"] for m in all_memories]
            vectors = self._embedding_engine.embed_batch(texts)
            mem_ids = [m["memory_id"] for m in all_memories]
            ann.add_vectors(mem_ids, vectors)
            mem_map = {m["memory_id"]: m for m in all_memories}
            max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            self._ann_cache = ((dim, len(all_memories), max_upd), mem_map, _time.time())

        # 搜索
        results = ann.search(query_vec, k=top_k, ef=50)

        # 构建结果映射（mem_map 来自缓存/构建分支）
        vector_results = []
        for mem_id, score in results:
            mem = mem_map.get(mem_id, {})
            vector_results.append({
                "memory_id": mem_id,
                "content": mem.get("content", ""),
                "content_preview": mem.get("content", "")[:100],
                "importance": mem.get("importance", 0.5),
                "created_at": mem.get("created_at", ""),
                "score": score,
                "persona_id": "",
                "role": "",
                "tags": [],
                "category": "",
            })

        return vector_results

    @traced("memory.ingest")
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
        agent_id: str = "default",
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        source_uri: Optional[str] = None,
        postprocess: bool = True,
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
            agent_id: Agent identifier (namespace isolation).
            ttl_seconds: Time-to-live in seconds (None = never expire).
            modality: Memory modality (text/image_description/code/json/trace/audio_transcript).
            source_uri: Original file path or URL (optional).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp, pushed_memories.
        """
        tags = tags or []

        result: Dict[str, Any] = {}
        if self._adapter:
            result = self._adapter.store_memory(
                content=content,
                persona_id=persona_id,
                session_id=session_id,
                tenant_id=tenant_id or self.tenant_id,
                agent_id=agent_id,
                ttl_seconds=ttl_seconds,
                role=role,
                importance=importance,
                tags=tags,
                category=category,
                modality=modality,
                metadata=metadata,
                source_uri=source_uri,
            )
        else:
            result = (
                self._adapter.store_memory(
                    content=content, persona_id=persona_id,
                    session_id=session_id, tenant_id=tenant_id or self.tenant_id,
                    agent_id=agent_id, ttl_seconds=ttl_seconds,
                    role=role, importance=importance, tags=tags, category=category,
                    modality=modality, metadata=metadata, source_uri=source_uri,
                ) if self._adapter else {"memory_id": "", "error": "no adapter"}
            )

        # 自动审计日志（同步：核心写入 + 审计链即时落账，保证可信链完整）
        memory_id = result.get("memory_id", "")
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=memory_id, action="create", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"importance": importance, "tags": tags,
                             "category": category, "modality": modality},
                )
            except Exception:
                pass

        # 加工管线（语义关联 + 实体提取 + 主动推送）
        # 2026-08-15（二轮压测修复）：postprocess 默认后台线程执行——
        # 写入即时返回、加工后台完成（_postprocess_memory 幂等、内部异常
        # 保护、daemon 线程），调用方无需再传 postprocess=False 规避同步
        # 加工成本（实测同步管线占写入 ~97%，单条 430-665ms vs 13ms）。
        # result 为共享 dict 引用，后台线程回填 pushed_memories /
        # extracted_entities / postprocess（pending → done），API 返回
        # 时可能仍为 pending，属设计内的异步语义。
        # 例外：TRINITY_LLM_EXTRACT=on 是显式同步功能（调用方期望 ingest
        # 返回时实体/关系已入库，如测试/管线），此时保持同步执行。
        llm_extract = os.environ.get(
            "TRINITY_LLM_EXTRACT", "").strip().lower() in ("1", "on", "true", "yes")
        if postprocess and memory_id:
            result.setdefault("pushed_memories", [])
            result["extracted_entities"] = 0
            result["postprocess"] = "pending"
            if llm_extract:
                self._postprocess_memory(memory_id, content, result)
            else:
                threading.Thread(
                    target=self._postprocess_memory,
                    args=(memory_id, content, result),
                    daemon=True, name="ingest-postprocess",
                ).start()
        else:
            result["pushed_memories"] = []
            result["extracted_entities"] = 0
            result["postprocess"] = "pending" if memory_id else "skipped"

        return result

    def _postprocess_memory(
        self, memory_id: str, content: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """新写入记忆的后台加工：语义关联 + 实体提取 + 主动推送。

        从 ingest 同步路径分离，供 memory_write 异步化调用（写入即时返回，
        加工后台完成）。幂等：内部各步骤均有异常保护，失败不抛出。

        2026-08-15（二轮压测修复）：全局 _postprocess_lock 串行化——
        加工是后台异步工作，无需并发（并发 sklearn fit + 抢 _write_lock
        会拖垮写入线程，实测响应 p95 3.7s）；串行后 embedding 引擎只
        fit 一次、写锁竞争收敛到单加工线程。

        Args:
            memory_id: 已写入的记忆 ID。
            content:   记忆内容。
            result:    可选，回填加工结果到已返回的 result 字典。

        Returns:
            Dict with linked_ids / extracted_entities / pushed_memories.
        """
        with self._postprocess_lock:
            linked_ids: List[str] = []
            if self._adapter and hasattr(self._adapter, "create_memory_link"):
                linked_ids = self._auto_link_semantic(memory_id, content)
            entity_ids = self._auto_extract_entities(memory_id, content)
            # ANN 增量维护（①落盘持久化，2026-08-15）：后台线程同步新记忆进索引
            if memory_id and self.use_ann:
                import threading as _th
                _th.Thread(
                    target=self._ann_incremental_add, args=(memory_id, content),
                    daemon=True,
                ).start()
            all_ids = [memory_id] + linked_ids if memory_id else linked_ids
            pushed = self.proactive_push(all_ids)
            if result is not None:
                result["pushed_memories"] = pushed
                result["extracted_entities"] = len(entity_ids)
                result["linked_ids"] = linked_ids
                result["postprocess"] = "done"
            return {
                "linked_ids": linked_ids,
                "extracted_entities": len(entity_ids),
                "pushed_memories": pushed,
            }

    def _auto_link_semantic(
        self, memory_id: str, content: str,
    ) -> List[str]:
        """为新写入的记忆自动创建语义关联链接（向量相似度 > 0.85）。

        批量嵌入（单次引擎调用）+ numpy 向量化相似度计算，避免逐条
        embed 调用导致写入路径超时（11k 记忆库实测 94s → 秒级）。

        可通过环境变量控制：
          - TRINITY_AUTO_LINK=off  关闭自动关联（写入最快速路径）
          - TRINITY_AUTO_LINK_MAX=N  候选记忆上限（默认 100）

        Args:
            memory_id: 新记忆 ID。
            content: 记忆内容。

        Returns:
            成功创建链接的目标记忆 ID 列表。
        """
        if os.environ.get("TRINITY_AUTO_LINK", "on").lower() in ("off", "0", "false"):
            return []
        linked: List[str] = []
        try:
            if not self._embedding_engine:
                from trinity.embeddings import create_engine
                # 2026-08-15（二轮压测修复）：backend="sklearn"——auto 会先
                # 探测 Ollama（本机未开时每次 embed 等 ~300ms 超时，embed_batch
                # 100 条 → 30s+，导致后台加工线程长时间"卡住"）。与聚合器
                # _get_embedding_fn 的修复一致：sklearn TF-IDF 确定性毫秒级。
                self._embedding_engine = create_engine(backend="sklearn")
            import numpy as np

            # 获取已有记忆（候选上限可配置）
            existing = []
            if self._adapter and hasattr(self._adapter, "get_all_memories"):
                try:
                    max_candidates = int(os.environ.get("TRINITY_AUTO_LINK_MAX", "100"))
                except ValueError:
                    max_candidates = 100
                existing = self._adapter.get_all_memories(limit=max_candidates)
            if not existing:
                return linked

            # 候选对齐：仅保留有内容、非自身的记忆
            candidates = [
                (mem, mem.get("content", ""))
                for mem in existing
                if mem.get("memory_id") and mem.get("memory_id") != memory_id
                and mem.get("content")
            ]
            if not candidates:
                return linked

            # 批量嵌入：新内容 + 全部候选，单次引擎调用
            texts = [content] + [c[1] for c in candidates]
            if hasattr(self._embedding_engine, "embed_batch"):
                vecs = self._embedding_engine.embed_batch(texts)
            else:
                vecs = [self._embedding_engine.embed(t) for t in texts]

            new_vec = np.asarray(vecs[0], dtype=np.float32)
            new_norm = np.linalg.norm(new_vec)
            if new_norm > 1e-8:
                new_vec = new_vec / new_norm

            matrix = np.vstack(
                [np.asarray(v, dtype=np.float32) for v in vecs[1:]]
            )
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms < 1e-8] = 1.0
            sims = (matrix @ new_vec) / norms

            for (mem, _), similarity in zip(candidates, sims):
                sim = float(similarity)
                if sim > 0.85:
                    self._adapter.create_memory_link(
                        memory_id, mem["memory_id"], link_type="semantic",
                        strength=round(sim, 3),
                    )
                    linked.append(mem["memory_id"])
        except Exception:
            pass
        return linked

    def _auto_extract_entities(
        self, memory_id: str, content: str,
    ) -> List[str]:
        """为新写入的记忆自动提取实体并创建 mentions 关系。

        LLM 驱动（2026-08-15, R2 优化）：TRINITY_LLM_EXTRACT=on 时改用
        EntityRelationExtractor（LLM 提取实体+关系谓词 → 写入 relations 表，
        对齐 Mem0/Zep 的写入即抽取）；未开启/失败时回退规则提取（原行为）。

        Args:
            memory_id: 新记忆 ID。
            content: 记忆内容。

        Returns:
            创建的实体 ID 列表。
        """
        entity_ids: List[str] = []
        if not self._adapter or not hasattr(self._adapter, "upsert_entity"):
            return entity_ids

        # ── LLM 驱动分支（env 开关，默认关）──────────────────────────
        if os.environ.get("TRINITY_LLM_EXTRACT", "").strip().lower() in ("1", "on", "true", "yes"):
            try:
                from trinity.daemon.memory_compressor import create_llm_compress_callable
                from trinity.memory.er_extractor import EntityRelationExtractor
                llm = create_llm_compress_callable()
                extractor = EntityRelationExtractor(self._adapter, llm_call=llm)
                summary = extractor.extract_from_memories([memory_id])
                for ent in summary.get("entities", []):
                    eid = ent.get("id", "")
                    if eid:
                        entity_ids.append(eid)
                return entity_ids
            except Exception:
                # LLM 不可用/失败 → 静默回退规则提取
                pass

        try:
            from trinity.core.entity_extractor import EntityExtractor
            extractor = EntityExtractor()
            entities = extractor.extract(content)
            for ent in entities:
                name = ent.get("name", "")
                etype = ent.get("type", "concept")
                if not name:
                    continue
                result = self._adapter.upsert_entity(name, etype, {})
                eid = result.get("id", "")
                if eid:
                    entity_ids.append(eid)
                    # 创建 mentions 关系
                    if hasattr(self._adapter, "create_relation"):
                        self._adapter.create_relation(
                            eid, "mentions", memory_id,
                            {"direction": "entity_to_memory"},
                        )
        except Exception:
            pass
        return entity_ids

    def age(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count.
        """
        if self._adapter:
            result = self._adapter.age_memories()
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=None, action="age", agent_id="system",
                        persona_id=None,
                        details={"aged_count": result.get("aged_count", 0)},
                    )
                except Exception:
                    pass
            return result
        return {"aged_count": 0, "error": "no adapter"}

    def stats(self) -> Dict[str, Any]:
        """返回记忆统计信息（总数、过期数、Agent 分布、平均访问频率等）。

        Returns:
            Stats dict.
        """
        if self._adapter:
            return self._adapter.get_memory_stats()
        return {"error": "no adapter"}

    def modality_stats(self) -> Dict[str, Any]:
        """返回各模态记忆数量、存储占比统计。

        Returns:
            Dict with total_active, modalities, percentages.
        """
        if self._adapter:
            return self._adapter.get_modality_stats()
        return {"error": "no adapter"}

    def touch(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。

        Args:
            memory_id: 记忆 ID。

        Returns:
            是否更新成功。
        """
        if self._adapter:
            return self._adapter.touch_memory(memory_id)
        return False

    def get_conflicts(self, memory_id: str) -> Dict[str, Any]:
        """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            冲突链信息，包含 conflict_group_id 与所有冲突版本列表。
        """
        if self._adapter:
            return self._adapter.get_conflicts(memory_id)
        return {"memory_id": memory_id, "conflicts": [], "error": "no adapter"}

    def resolve_conflict(
        self, conflict_group_id: str, keep_memory_id: str
    ) -> Dict[str, Any]:
        """解决冲突：保留选定版本，软删除同一冲突组的其他版本。

        Args:
            conflict_group_id: 冲突组 ID。
            keep_memory_id: 保留的记忆 ID。

        Returns:
            操作结果，含 resolved_count 与 discarded_ids。
        """
        if self._adapter:
            result = self._adapter.resolve_conflict(conflict_group_id, keep_memory_id)
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=keep_memory_id, action="resolve", agent_id=None,
                        persona_id=None,
                        details={"conflict_group_id": conflict_group_id,
                                 "resolved_count": result.get("resolved_count", 0)},
                    )
                except Exception:
                    pass
            return result
        return {"error": "no adapter", "resolved_count": 0}

    def dedup_stats(self) -> Dict[str, Any]:
        """返回去重统计信息（冲突组数、已解决数等）。

        Returns:
            Dedup stats dict.
        """
        if self._adapter:
            return self._adapter.dedup_stats()
        return {"error": "no adapter"}

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
            from trinity.version import __version__, VERSION_STRING
            return {
                "trinity_version": VERSION_STRING,
                "source_version": __version__,
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
        self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if self._adapter:
            return self._adapter.get_persona_memories(persona_id, agent_id=agent_id, limit=limit)
        return self.bridge("diagnostics").get("storage", {})

    def delete_memory(self, memory_id: str) -> bool:
        if self._adapter:
            result = self._adapter.delete_memory(memory_id)
            # ANN 增量维护（①落盘持久化）：后台移除索引条目
            if result and self.use_ann:
                import threading as _th
                _th.Thread(
                    target=self._ann_incremental_remove, args=(memory_id,),
                    daemon=True,
                ).start()
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=memory_id, action="delete", agent_id=None,
                        persona_id=None,
                        details={"success": result},
                    )
                except Exception:
                    pass
            return result
        return True

    def update_memory(
        self,
        memory_id: str,
        new_content: str,
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update memory (conflict-preserving versioning).

        Old version rows are retained in the audit chain; the memories row is
        bumped to version + 1 with a recomputed SHA-256 hash.

        Returns:
            Dict with memory_id, old_version, new_version, sha256_hash,
            timestamp and status.

        Raises:
            ValueError: If memory_id not found or adapter lacks update support.
        """
        if not self._adapter or not hasattr(self._adapter, "update_memory"):
            raise ValueError(
                f"update_memory not supported by adapter: {type(self._adapter).__name__}"
            )
        current = self._adapter.get_memory(memory_id)
        old_version = current.get("version", 0) if current else 0
        result = self._adapter.update_memory(
            memory_id=memory_id,
            content=new_content,
            importance=importance,
            tags=tags,
            category=category,
        )
        if result is None:
            raise ValueError(f"Memory not found: {memory_id}")
        # ANN 增量维护（①落盘持久化）：内容变更 → 后台更新索引条目
        if self.use_ann:
            import threading as _th
            _th.Thread(
                target=self._ann_incremental_add,
                args=(memory_id, new_content), daemon=True,
            ).start()
        return {
            "memory_id": memory_id,
            "old_version": old_version,
            "new_version": result.get("version"),
            "sha256_hash": result.get("sha256_hash"),
            "timestamp": result.get("updated_at"),
            "status": result.get("status"),
        }

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

    # ── 记忆回放与审计 ──────────────────────────────────────────────

    def get_audit_trail(self, memory_id: str) -> List[Dict[str, Any]]:
        """查看某条记忆的完整变更历史（审计轨迹）。"""
        if self._adapter and hasattr(self._adapter, "get_audit_trail"):
            return self._adapter.get_audit_trail(memory_id)
        return []

    def replay_session(self, agent_id: str,
                        start_time: str = None,
                        end_time: str = None) -> List[Dict[str, Any]]:
        """回放某 Agent 在时间段内的所有操作。"""
        if self._adapter and hasattr(self._adapter, "replay_agent_session"):
            return self._adapter.replay_agent_session(agent_id, start_time, end_time)
        return []

    def verify_integrity(self) -> Dict[str, Any]:
        """验证审计链完整性，检测篡改。"""
        if self._adapter and hasattr(self._adapter, "verify_audit_integrity"):
            return self._adapter.verify_audit_integrity()
        return {"integrity_ok": False, "error": "no adapter"}

    def audit_summary(self, start_time: str = None,
                       end_time: str = None) -> Dict[str, Any]:
        """审计摘要：各操作计数、活跃 Agent、峰值时段。"""
        if self._adapter and hasattr(self._adapter, "get_audit_summary"):
            return self._adapter.get_audit_summary(start_time, end_time)
        return {"error": "no adapter"}

    def audit_timeline(self, agent_id: str = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """最近操作时间线。"""
        if self._adapter and hasattr(self._adapter, "replay_agent_session"):
            session = self._adapter.replay_agent_session(agent_id) if agent_id else []
            return session[-limit:]
        return []

    def export_replay_report(self, agent_id: str,
                               start_time: str = None,
                               end_time: str = None,
                               format: str = "markdown") -> str:
        """导出回放报告为 Markdown 文件，返回报告路径。"""
        import os
        session = self.replay_session(agent_id, start_time, end_time)
        if not session:
            return ""
        lines = [
            f"# Agent 记忆回放报告",
            f"",
            f"**Agent ID**: `{agent_id}`",
            f"**时间范围**: {start_time or '(不限)'} ~ {end_time or '(不限)'}",
            f"**总操作数**: {len(session)}",
            f"**导出时间**: {__import__('datetime').datetime.now().isoformat()}",
            f"",
            f"---",
            f"",
        ]
        for i, entry in enumerate(session, 1):
            lines.append(f"## {i}. {entry.get('action', 'unknown').upper()}")
            lines.append(f"- **时间**: {entry.get('timestamp', '')}")
            lines.append(f"- **记忆 ID**: {entry.get('memory_id', 'N/A')}")
            lines.append(f"- **Agent**: {entry.get('agent_id', 'N/A')}")
            lines.append(f"- **Persona**: {entry.get('persona_id', 'N/A')}")
            details = entry.get("details", {})
            if details:
                import json
                lines.append(f"- **详情**:")
                lines.append(f"```json")
                lines.append(json.dumps(details, ensure_ascii=False, indent=2))
                lines.append(f"```")
            lines.append("")

        report_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "output",
            f"replay_report_{agent_id}_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        )
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return report_path

    def benchmark(self, name: str = "longmemeval",
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        from trinity.benchmark.runner import run_benchmark
        return run_benchmark(name, config)

    # ── 多锚点身份架构 ──────────────────────────────────────────────

    def register_identity_anchor(self, agent_id: str, anchor_type: str,
                                  content: str) -> Dict[str, Any]:
        """注册或更新身份锚点。"""
        if self._adapter and hasattr(self._adapter, "upsert_anchor"):
            return self._adapter.upsert_anchor(agent_id, anchor_type, content)
        return {"error": "no adapter"}

    def get_identity_profile(self, agent_id: str) -> Dict[str, Any]:
        """获取完整身份画像（含一致性分数）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.reconstruct_identity(agent_id)
        return {"error": "no adapter"}

    def reconstruct_identity(self, agent_id: str,
                              available_anchors: List[str] = None) -> Dict[str, Any]:
        """从锚点重建 Agent 身份画像。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            if available_anchors:
                return mgr.partial_reconstruct(agent_id, available_anchors)
            return mgr.reconstruct_identity(agent_id)
        return {"error": "no adapter"}

    def detect_drift(self, agent_id: str) -> Dict[str, Any]:
        """检测身份漂移（对比当前行为与基线锚点）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.detect_identity_drift(agent_id)
        return {"error": "no adapter"}

    def export_identity(self, agent_id: str) -> Dict[str, Any]:
        """导出完整身份包（可用于 Agent 迁移）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.export_identity_bundle(agent_id)
        return {"error": "no adapter"}

    def import_identity(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        """导入身份包。"""
        if self._adapter and hasattr(self._adapter, "upsert_anchor"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.import_identity_bundle(bundle)
        return {"error": "no adapter"}

    # ── DCSA-EJP 双循环宪法自审计 ─────────────────────────────────

    @property
    def _dcsa_auditor(self):
        """惰性初始化 DCSA Auditor。"""
        if not hasattr(self, "_dcsa_auditor_inst"):
            from trinity.audit.auditor import Auditor
            self._dcsa_auditor_inst = Auditor(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._dcsa_auditor_inst

    def audit_action(self, agent_id: str, task: str = "",
                     executor_result: str = "{}") -> Dict[str, Any]:
        """执行一次双循环审计（executor + auditor 独立审查）。"""
        auditor = self._dcsa_auditor
        return auditor.audit_action({
            "agent_id": agent_id, "task": task,
            "executor_result": executor_result,
        })

    def get_audit_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取 DCSA-EJP 审计运行历史。"""
        if self._adapter and hasattr(self._adapter, "get_audit_history"):
            return self._adapter.get_audit_history(agent_id, limit)
        return []

    def get_violations(self, agent_id: str = None,
                        limit: int = 100) -> List[Dict[str, Any]]:
        """获取宪法违规趋势。"""
        if self._adapter and hasattr(self._adapter, "get_violation_trends"):
            return self._adapter.get_violation_trends(agent_id, limit)
        return []

    def get_dcsa_metrics(self) -> Dict[str, Any]:
        """获取 DCSA-EJP 六项指标实时值。"""
        return self._dcsa_auditor.get_metrics()

    # ── A2A Protocol: Agent Registry & Task Management ────────────

    @property
    def _a2a_registry(self):
        """惰性初始化 A2A CapabilityRegistry。"""
        if not hasattr(self, "_a2a_registry_inst"):
            from trinity.a2a.capability_registry import CapabilityRegistry
            self._a2a_registry_inst = CapabilityRegistry(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._a2a_registry_inst

    @property
    def _a2a_task_manager(self):
        """惰性初始化 A2A TaskManager。"""
        if not hasattr(self, "_a2a_task_manager_inst"):
            from trinity.a2a.task_manager import TaskManager
            self._a2a_task_manager_inst = TaskManager(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._a2a_task_manager_inst

    def register_agent_card(self, agent_id: str, name: str,
                             description: str = "", version: str = "1.0.0",
                             capabilities: List[str] = None,
                             endpoints: Dict[str, str] = None,
                             skills: List[Dict[str, Any]] = None,
                             input_modes: List[str] = None,
                             output_modes: List[str] = None,
                             security_level: str = "low") -> Dict[str, Any]:
        """注册 Agent 到 A2A 联邦能力目录。"""
        from trinity.a2a.agent_card import AgentCard, SkillDef
        caps = capabilities or []
        eps = endpoints or {}
        skill_objs = [SkillDef(name=s.get("name", ""), description=s.get("description", ""),
                                input_schema=s.get("input_schema", {}), output_schema=s.get("output_schema", {}),
                                examples=s.get("examples", []))
                       for s in (skills or [])]
        card = AgentCard(
            agent_id=agent_id, name=name, description=description,
            version=version, capabilities=caps, endpoints=eps,
            skills=skill_objs,
            input_modes=input_modes or ["text"],
            output_modes=output_modes or ["text"],
            security_level=security_level,
        )
        return self._a2a_registry.register_agent(card)

    def get_agent_card(self, agent_id: str) -> Dict[str, Any]:
        """获取 Agent 能力卡片。"""
        if self._adapter and hasattr(self._adapter, "get_agent_card"):
            return self._adapter.get_agent_card(agent_id) or {}
        return {}

    def unregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """注销 Agent。"""
        return self._a2a_registry.unregister_agent(agent_id)

    def list_a2a_agents(self) -> Dict[str, Any]:
        """列出所有注册的 Agent。"""
        if self._adapter and hasattr(self._adapter, "get_agent_card"):
            return self._a2a_registry.list_all_agents()
        return {"agents": [], "total": 0}

    def create_a2a_task(self, task_id: str, from_agent: str,
                         to_agent: str, payload: str = "{}",
                         status: str = "pending",
                         result: Optional[str] = None) -> Dict[str, Any]:
        """创建跨 Agent 任务。"""
        if self._adapter and hasattr(self._adapter, "create_a2a_task"):
            ok = self._adapter.create_a2a_task(
                task_id, from_agent, to_agent, payload, status, result)
            return {"status": "ok" if ok else "error", "task_id": task_id}
        return {"error": "no adapter"}

    def query_a2a_task(self, task_id: str) -> Dict[str, Any]:
        """查询跨 Agent 任务状态。"""
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            tasks = self._adapter.list_a2a_tasks(task_id=task_id)
            return tasks[0] if tasks else {}
        return {}

    def update_a2a_task(self, task_id: str, status: str,
                         result: Optional[str] = None) -> Dict[str, Any]:
        """更新跨 Agent 任务状态。"""
        if self._adapter and hasattr(self._adapter, "update_a2a_task"):
            ok = self._adapter.update_a2a_task(task_id, status, result)
            return {"status": "ok" if ok else "error", "task_id": task_id}
        return {"error": "no adapter"}

    def list_a2a_tasks(self, agent_id: str = None,
                        status: str = None) -> List[Dict[str, Any]]:
        """列出跨 Agent 任务。"""
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            return self._adapter.list_a2a_tasks(agent_id=agent_id, status=status)
        return []

    def send_a2a_message(self, from_agent: str, to_agent: str,
                          method: str, params: Dict[str, Any] = None,
                          req_id: str = None) -> Dict[str, Any]:
        """发送 A2A 消息（JSON-RPC 2.0）。"""
        from trinity.a2a.protocol import A2AProtocol
        proto = A2AProtocol()
        return proto.send_message(from_agent, to_agent, method,
                                  params or {}, req_id)


    # ── 混合检索（向量 + BM25 + 图谱）─────────────────────────────

    @property
    def hybrid_retriever(self):
        """HybridRetriever 实例（延迟初始化）。

        组合向量/FTS + BM25 关键词 + 图谱检索，支持 fusion/rrf/cascade。
        use_ann=True 时向量源使用 ANNIndex（FAISS HNSW），否则为 SQLite FTS。
        """
        if self._hybrid_retriever is None:
            from trinity.retrieval import HybridRetriever, BM25Index, GraphRetriever

            if self._bm25_index is None:
                self._ensure_bm25_index()
            bm25 = self._bm25_index or BM25Index()
            graph = GraphRetriever(self._adapter) if self._adapter else None

            # search_fn: 闭包封装向量/FTS 逻辑
            if self.use_ann:
                def _vector_search_fn(q: str, top_k: int):
                    return self._vector_search(q, top_k)
            else:
                def _vector_search_fn(q: str, top_k: int):
                    return self._adapter.search_memories(
                        query=q, top_k=top_k,
                    ) if self._adapter else []

            self._hybrid_retriever = HybridRetriever(
                bm25_index=bm25,
                graph_retriever=graph,
                search_fn=_vector_search_fn,
            )
        return self._hybrid_retriever

    def _ensure_bm25_index(self) -> None:
        """首次使用 BM25 时启动后台线程构建倒排索引（不阻塞首次检索）。

        2026-08-15（压测优化）：原同步构建（12k 记忆 ~1-2s）是首次检索
        2.4s 冷启动尾巴的组成部分。改为：立即返回空索引（HybridRetriever
        对空索引 search 返回空 = 优雅降级，融合其余通道），后台线程
        add_documents 填充同一索引对象（CPython GIL 下 dict 并发读写安全，
        只可能读到中间态，不会损坏）。完成后 _bm25_ready=True。

        2026-08-15（二轮压测修复）：_bm25_lock 原子化"检查-创建-启动"——
        启动预热线程与首个请求并发调用时，原先各自启动构建线程导致
        双份 12k 条 BM25 构建 + GIL 竞争（实测首个请求 16s）。
        """
        from trinity.retrieval.bm25_index import BM25Index

        if self._bm25_ready:
            return
        with self._bm25_lock:
            if self._bm25_index is not None:
                return  # 已在构建（另一线程启动），等待 ready 即可
            self._bm25_index = BM25Index()  # 空索引立即可用（优雅降级）
            if self._adapter is None:
                return

            def _build() -> None:
                try:
                    all_mems = self._adapter.get_all_memories(limit=10000)
                    if not all_mems:
                        return
                    items = [(m["memory_id"], m.get("content", ""))
                             for m in all_mems]
                    self._bm25_index.add_documents(items)
                    self._bm25_ready = True
                except Exception:
                    pass

            threading.Thread(target=_build, daemon=True,
                             name="bm25-prewarm").start()

    def search_hybrid(
        self,
        query: str,
        top_k: int = 10,
        strategy: str = "fusion",
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        routing: str = "auto",
    ) -> Dict[str, Any]:
        """混合检索（向量 + BM25 + 图谱融合）。

        ②自适应预算路由（2026-08-15，对齐 Query-Aware Budget-Tier Routing）：
          routing="auto"：按 query 特征分层——短查询走 light（FTS 快路径），
            长/复杂查询走 full（5 通道融合）。
          routing="light"/"full"：强制指定。
          环境变量 TRINITY_ADAPTIVE_ROUTING=on 时 auto 生效；off 默认全走 full
          （行为兼容，A/B 可测）。
        """
        env = os.environ.get("TRINITY_ADAPTIVE_ROUTING", "off").strip().lower()
        if routing == "auto":
            if env != "on":
                routing = "full"
            else:
                # 特征规则：短查询（≤8 字符）走轻通道
                routing = "light" if len(query.strip()) <= 8 else "full"

        # ── light 路径：FTS 快通道（~3ms），天然支持过滤 ──────────
        if routing == "light" and self._adapter is not None:
            results = self._adapter.search_memories(
                query=query, top_k=top_k,
                agent_id=agent_id or None,
                persona_id=persona_id or None,
                tenant_id=tenant_id or None,
            )
            result = {
                "results": results,
                "strategy": "light",
                "query": query,
                "breakdown": {"routing": "light", "channels": ["fts"]},
            }
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=None, action="search_hybrid",
                        agent_id=agent_id, persona_id=persona_id,
                        details={"query": query, "top_k": top_k, "strategy": "light",
                                 "hits": len(results)},
                    )
                except Exception:
                    pass
            return result

        hr = self.hybrid_retriever

        # 如有 agent/persona/tenant 过滤，在向量侧 wrap search_fn；
        # 同时把隔离维度折入 retriever 语义缓存 key，防止跨租户缓存串扰。
        cache_scope = f"a={agent_id or ''}:p={persona_id or ''}:t={tenant_id or ''}"
        if agent_id or persona_id or tenant_id:
            _orig_fn = hr._search_fn

            def _filtered_fn(q: str, tk: int):
                return self._adapter.search_memories(
                    query=q, top_k=tk,
                    agent_id=agent_id or None,
                    persona_id=persona_id or None,
                    tenant_id=tenant_id or None,
                ) if self._adapter else []

            hr._search_fn = _filtered_fn
            try:
                result = hr.search(query=query, top_k=top_k, strategy=strategy, cache_scope=cache_scope)
            finally:
                hr._search_fn = _orig_fn
        else:
            result = hr.search(query=query, top_k=top_k, strategy=strategy, cache_scope=cache_scope)

        # 修复(2026-08-14): 融合后统一后过滤——
        #  1) 隔离：带 agent/persona/tenant 过滤时只保留归属匹配的记忆
        #  2) 状态：引擎库中 status != active（软删/过期）的记忆剔除，防泄漏与幽灵
        #  3) 不在引擎库的结果（聚合池记忆）保留（设计内的池通道）
        if self._adapter is not None and hasattr(self._adapter, "get_memory_owners"):
            mids = [r.get("memory_id") for r in result.get("results", []) if r.get("memory_id")]
            if mids:
                owners = self._adapter.get_memory_owners(mids)
                keep = set()
                for mid, own in owners.items():
                    if own.get("status") and own.get("status") != "active":
                        continue  # 软删/非活跃记忆剔除
                    if agent_id and own.get("agent_id") != agent_id:
                        continue
                    if persona_id and own.get("persona_id") != persona_id:
                        continue
                    if tenant_id and own.get("tenant_id") != tenant_id:
                        continue
                    keep.add(mid)
                # 存在于引擎库的按上述规则过滤；不在库的（池记忆/幽灵）：
                # 带隔离过滤时保守排除，无过滤时保留（避免误伤池通道）
                if agent_id or persona_id or tenant_id:
                    result["results"] = [
                        r for r in result["results"] if r.get("memory_id") in keep
                    ]
                else:
                    result["results"] = [
                        r for r in result["results"]
                        if r.get("memory_id") in keep or r.get("memory_id") not in owners
                    ]

        # 审计日志
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=None, action="search_hybrid",
                    agent_id=agent_id, persona_id=persona_id,
                    details={
                        "query": query, "top_k": top_k, "strategy": strategy,
                        "hits": len(result.get("results", [])),
                        "breakdown": result.get("breakdown", {}),
                    },
                )
            except Exception:
                pass

        # ②自适应路由：full 路径标记
        result.setdefault("breakdown", {})["routing"] = "full"

        return result

    # ── 跨模态检索（文字 ↔ 图片记忆）─────────────────────────────

    # ── 个性化（R3 P0-2, 2026-08-15）───────────────────────────────
    # PAHF 双反馈个性化引擎（Meta ICLR 2026 对齐）：行动前澄清 →
    # 偏好检索 → 行动后反馈整合。惰性实例化；失败静默降级（不影响主流程）。

    @property
    def personalization(self):
        """惰性获取 PAHFEngine（不可用时返回 None）。"""
        if self._personalization is None:
            try:
                from trinity.modules.second_brain.personalization_engine import PAHFEngine
                self._personalization = PAHFEngine()
            except Exception:
                self._personalization = None
        return self._personalization

    def get_preference_context(
        self, user_id: str, domain: str = "search",
    ) -> Dict[str, Any]:
        """获取用户偏好上下文（供检索/行动注入）。

        Args:
            user_id: 用户标识（persona_id）。
            domain: 偏好领域（search/style/format...）。

        Returns:
            {"user_id": ..., "preferences": [...], "enabled": bool}
        """
        eng = self.personalization
        if eng is None:
            return {"user_id": user_id, "preferences": [], "enabled": False}
        try:
            from trinity.modules.second_brain.personalization_engine import PreferenceDomain
            dom = PreferenceDomain(domain) if domain in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            prefs = eng.retriever.retrieve(user_id, dom)
            entries = [
                {"content": e.value, "confidence": e.confidence,
                 "key": e.key,
                 "domain": e.domain.value if hasattr(e.domain, "value") else str(e.domain)}
                for e in (prefs or {}).values()
            ]
            return {"user_id": user_id, "preferences": entries, "enabled": True}
        except Exception:
            return {"user_id": user_id, "preferences": [], "enabled": False}

    def integrate_feedback(
        self, user_id: str, feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        """整合用户反馈到偏好记忆（行动后）。

        Args:
            user_id: 用户标识。
            feedback: {"content": 偏好内容, "domain": 领域,
                       "feedback_type": "explicit_confirm|explicit_correction", ...}

        Returns:
            {"integrated": bool, "preference_count": int}
        """
        eng = self.personalization
        if eng is None:
            return {"integrated": False, "preference_count": 0}
        try:
            content = feedback.get("content", "")
            if not content:
                return {"integrated": False, "preference_count": 0}
            from trinity.modules.second_brain.personalization_engine import (
                FeedbackRecord, FeedbackType, PreferenceDomain,
            )
            domain_str = feedback.get("domain", "search")
            dom = PreferenceDomain(domain_str) if domain_str in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            fb_type_str = feedback.get("feedback_type", "explicit_confirm")
            fb_type = FeedbackType(fb_type_str) if fb_type_str in {f.value for f in FeedbackType} \
                else FeedbackType.EXPLICIT_CONFIRM
            record = FeedbackRecord(
                user_id=user_id,
                feedback_type=fb_type,
                raw_response=content,
                preference_changes=[{"domain": dom.value, "key": content[:60],
                                     "value": content, "confidence": 0.6}],
            )
            result = eng.integrator.integrate_feedback(
                user_id=user_id, feedback=record, retriever=eng.retriever,
            )
            return {
                "integrated": True,
                "changes": result.get("changes_made", 0),
                "drift_detected": result.get("drift_detected", False),
            }
        except Exception:
            return {"integrated": False, "preference_count": 0}

    def should_clarify(
        self, user_id: str, action_context: str, domain: str = "search",
    ) -> bool:
        """判断行动前是否需要澄清偏好。"""
        eng = self.personalization
        if eng is None or not action_context.strip():
            return False
        try:
            from trinity.modules.second_brain.personalization_engine import PreferenceDomain
            dom = PreferenceDomain(domain) if domain in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            return eng.should_clarify(user_id, action_context, dom)
        except Exception:
            return False

    # ── SAGE 自进化图记忆（R5 P0, 2026-08-15, MindMemOS 对齐）──────
    # 惰性实例化 SAGEGraphMemoryEngine：写入同步图记忆、查询证据路径、
    # 触发自进化。失败静默降级（不影响主流程）。

    @property
    def sage(self):
        if self._sage is None:
            try:
                from trinity.modules.second_brain.sage_graph_memory_engine import (
                    SAGEGraphMemoryEngine,
                )
                self._sage = SAGEGraphMemoryEngine()
            except Exception:
                self._sage = None
        return self._sage

    def sage_ingest(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """摄入一轮交互到 SAGE 图记忆（MindMemOS 自进化）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False}
        try:
            return {**eng.ingest_turn(content, metadata), "sage": True}
        except Exception:
            return {"sage": False}

    def sage_query(self, query_text: str) -> Dict[str, Any]:
        """SAGE 图检索（实体/关系/证据路径）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False, "entities": [], "relations": []}
        try:
            return {**eng.query(query_text), "sage": True}
        except Exception:
            return {"sage": False, "entities": [], "relations": []}

    def sage_evolve(self) -> Dict[str, Any]:
        """触发 SAGE 自进化轮（图结构调整）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False}
        try:
            return {**eng.evolve(), "sage": True}
        except Exception:
            return {"sage": False}

    # ── DCPM 双过程认知记忆（R5 P0, 2026-08-15, Dual-Process 对齐）──

    @property
    def dcpm(self):
        if self._dcpm is None:
            try:
                from trinity.modules.second_brain.dcpm_dual_process_memory import (
                    System1DaytimeWriter, System2NighttimeEngine,
                )
                self._dcpm = {
                    "system1": System1DaytimeWriter(),
                    "system2": System2NighttimeEngine(),
                }
            except Exception:
                self._dcpm = None
        return self._dcpm

    def dcpm_record_belief(self, subject: str, predicate: str, obj: str,
                           superseded_by: Optional[str] = None) -> Dict[str, Any]:
        """System1 记录信念（含修订链，快路径）。"""
        eng = self.dcpm
        if eng is None:
            return {"dcpm": False}
        try:
            import uuid as _uuid
            from trinity.modules.second_brain.dcpm_dual_process_memory import BeliefRevisionNode
            node = BeliefRevisionNode(
                belief_id=_uuid.uuid4().hex[:12],
                subject=subject, predicate=predicate,
                object=obj, superseded_by=superseded_by,
            )
            stored = eng["system1"].record_belief(node)
            return {"dcpm": True, "belief_id": stored.belief_id,
                    "chain_len": len(eng["system1"].get_chain(stored.belief_id))}
        except Exception:
            return {"dcpm": False}

    def dcpm_consolidate(self) -> Dict[str, Any]:
        """System2 夜间整合：schema 归纳 + 冲突检测（慢路径）。"""
        eng = self.dcpm
        if eng is None:
            return {"dcpm": False, "schemas": 0, "collisions": 0}
        try:
            beliefs = list(eng["system1"]._beliefs.values())
            schemas = eng["system2"].induce_schemas(beliefs)
            collisions = 0
            for i in range(len(schemas)):
                for j in range(i + 1, len(schemas)):
                    if eng["system2"].detect_collisions(schemas[i], schemas[j]):
                        collisions += 1
            return {"dcpm": True, "schemas": len(schemas), "collisions": collisions}
        except Exception:
            return {"dcpm": False, "schemas": 0, "collisions": 0}

    def _ensure_cross_modal_retriever(self):
        """Lazy-initialize the CrossModalRetriever.

        回归修复(2026-08-14): 首次构造可能因离线导入 torch/transformers 耗时 60s+，
        用后台线程 + 15s 上限，超时立即返回降级对象（客户端不阻塞），
        线程完成后下次调用自动换装完整检索器。
        """
        if self._cross_modal_retriever is None:
            import os as _os
            import threading
            from types import SimpleNamespace
            from trinity.retrieval.cross_modal import CrossModalRetriever

            prev = {k: _os.environ.get(k)
                    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
            _os.environ.setdefault("HF_HUB_OFFLINE", "1")
            _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            holder: dict = {}

            def _build() -> None:
                try:
                    holder["cm"] = CrossModalRetriever(trinity_instance=self)
                except Exception as exc:  # noqa: BLE001
                    holder["err"] = exc
                finally:
                    for k, v in prev.items():
                        if v is None:
                            _os.environ.pop(k, None)
                        else:
                            _os.environ[k] = v

            t = threading.Thread(target=_build, daemon=True)
            t.start()
            t.join(timeout=15)
            if "cm" in holder:
                self._cross_modal_retriever = holder["cm"]
            else:
                # 降级占位：文本/CLIP 编码器均不可用；后台线程完成后下次请求换装
                self._cross_modal_retriever = SimpleNamespace(
                    _text_encoder=None, use_clip=False, _PIL_Image=None)
                self._cross_modal_pending_holder = holder
        elif getattr(self, "_cross_modal_pending_holder", None):
            # 后台线程已完成 → 换装完整检索器
            holder = self._cross_modal_pending_holder
            if "cm" in holder:
                self._cross_modal_retriever = holder["cm"]
            self._cross_modal_pending_holder = None
        return self._cross_modal_retriever

    def search_image_by_text(
        self,
        text: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search image_description memories by text query.

        Parameters
        ----------
        text : str
            Natural language query describing the image to find.
        top_k : int
            Max results.

        Returns
        -------
        dict with results / query_type='text_to_image' / total.
        """
        cm = self._ensure_cross_modal_retriever()
        return cm.search_image_by_text(text_query=text, top_k=top_k)

    def search_text_by_image(
        self,
        image_path: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search text memories by image query.

        Parameters
        ----------
        image_path : str
            Absolute path to the query image.
        top_k : int
            Max results.

        Returns
        -------
        dict with results / query_type='image_to_text' / total.
        """
        cm = self._ensure_cross_modal_retriever()
        return cm.search_text_by_image(image_path=image_path, top_k=top_k)

    # ── 记忆压缩（Letta 虚拟上下文管理）─────────────────────────

    @property
    def compressor(self):
        """MemoryCompressor 实例（延迟初始化）。"""
        return self._ensure_compressor()

    def _ensure_compressor(self):
        """Lazy-initialize the MemoryCompressor."""
        if self._compressor is None:
            from trinity.memory.compression import MemoryCompressor
            self._compressor = MemoryCompressor(
                trinity_instance=self,
                max_tokens=4096,
                compression_threshold=0.8,
            )
        return self._compressor

    def compress_context(
        self,
        agent_id: str,
        max_tokens: int = 4096,
        no_compress: bool = False,
    ) -> Dict[str, Any]:
        """Compress agent context using the compression pipeline.

        Parameters
        ----------
        agent_id : str
            Agent whose context is being compressed.
        max_tokens : int
            Target token budget ceiling.
        no_compress : bool
            Set True to skip compression entirely (passthrough).

        Returns
        -------
        dict with the compressed context result.
        """
        if no_compress:
            return {
                "status": "skipped",
                "reason": "no_compress flag",
                "agent_id": agent_id,
            }

        # Gather all memories for this agent
        memories = []
        if self._adapter and hasattr(self._adapter, "get_all_memories"):
            try:
                memories = self._adapter.get_all_memories(
                    agent_id=agent_id,
                    limit=10000,
                ) or []
            except Exception:
                pass

        if hasattr(self._adapter, "search_memories"):
            try:
                extra = self._adapter.search_memories(
                    query="",
                    agent_id=agent_id,
                    top_k=1000,
                ) or []
                seen = {m.get("memory_id") for m in memories}
                for m in extra:
                    if m.get("memory_id") not in seen:
                        memories.append(m)
            except Exception:
                pass

        compressor = self._ensure_compressor()
        compressor.max_tokens = max_tokens
        result = compressor.compress(agent_id, memories)
        return result.to_dict()

    # ── VMS (Virtual Memory System) Integration ──────────────────────

    @property
    def vms(self):
        """VMS instance (lazy-initialised)."""
        return self._ensure_vms()

    def _ensure_vms(self):
        if getattr(self, "_vms", None) is None:
            from trinity.vms import VMS
            from trinity.vms.backends.sqlite_backend import SQLiteVMSBackend
            from trinity.adapters.sqlite import SQLiteAdapter
            import os

            db_path = os.environ.get("TRINITY_DB_PATH") or os.path.join(
                _TRINITY_STORE or str(Path.home() / ".trinity" / "store"), "trinity_store.db"
            )
            adapter = SQLiteAdapter(db_path=db_path)
            adapter.connect()

            backend = SQLiteVMSBackend(adapter=adapter)
            self._vms = VMS.from_defaults(memory_store=backend)
        return self._vms


class TrinityClient:
    """Alias for Trinity — same unified interface."""

    def __new__(cls, *args, **kwargs):
        return Trinity(*args, **kwargs)
