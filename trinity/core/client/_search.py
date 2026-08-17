"""Trinity client - search, ranking & hybrid retrieval mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
from ._helpers import _fuse_results, _get_embedding_engine, _get_vector_index

class _SearchMixin:
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
            #   hybrid         → 5 通道融合（rrf；仅当 hybrid retriever 已初始化）
            #   graph          → 图谱检索（adapter 支持时）；否则回退 FTS5
            # 2026-08-17 二轮验证（scripts/verify_engine_default.py, 同 120 题同摄入 A/B）:
            #   FTS R@5=0.975 > hybrid-rrf 0.942 → 引擎默认保持 FTS；
            #   hybrid 仅对显式初始化/调用 search_hybrid 的路径生效（strategy 已标定 fusion→rrf）。
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
                    self.hybrid_retriever  # 懒初始化（BM25 后台预热，非阻塞）
                    raw_results = self.search_hybrid(
                        # 2026-08-17 标定（120 题官方子集）: fusion 静态权重 R@5=0.008,
                        # rrf R@5=0.950 → 默认改为 rrf（见 scripts/calibrate_ranking.py）
                        query=query, top_k=top_k, strategy="rrf",
                        agent_id=agent_id, persona_id=persona_id,
                        tenant_id=tenant_id,
                    ).get("results", [])
                except Exception:
                    raw_results = []
                if not raw_results:
                    # hybrid 空结果（BM25 未就绪/通道退化）→ FTS 兜底防丢召回
                    try:
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
                    except Exception:
                        raw_results = []
                else:
                    # hybrid 结果为 lean dict（memory_id/hybrid_score…），按 memory_id
                    # 回补完整字段（content/persona_id/score/created_at…），与 FTS 路径
                    # 返回同构，保证调用方（DSH/MCP/API/测试）schema 兼容。
                    try:
                        enriched = []
                        for m in raw_results:
                            mid = m.get("memory_id")
                            full = {}
                            if mid and self._adapter:
                                try:
                                    full = self._adapter.get_memory(mid) or {}
                                except Exception:
                                    full = {}
                            rec = {**full, **m}
                            rec.setdefault("score", rec.get("hybrid_score", 0.0))
                            enriched.append(rec)
                        raw_results = enriched
                    except Exception:
                        pass
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
        strategy: str = "rrf",  # 2026-08-17 标定: rrf 远优于 fusion（0.950 vs 0.008）
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
