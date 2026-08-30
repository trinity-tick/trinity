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

_TIME_WORDS = ("最近", "刚才", "昨天", "今天", "上次", "之前", "刚", "刚刚", "前几天", "先前")
_KNOWLEDGE_WORDS = ("规则", "规范", "标准", "流程", "步骤", "配置", "指南", "手册", "制度")


def _infer_layer(query: str) -> Optional[str]:
    """2026-08-27（方向A 认知分层）：查询性质 -> 记忆层。

    时间词 → STM/IM（会话延续）；知识词 → LTM（事实/规范）；无信号 → None（全层）。
    """
    try:
        q = str(query or "")
        if any(w in q for w in _TIME_WORDS):
            return "episodic"
        if any(w in q for w in _KNOWLEDGE_WORDS):
            return "semantic"
    except Exception:
        pass
    return None


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
        include_docs: bool = False,
        page_tree: bool = False,
        page_k: int = 3,
        view: Optional[str] = None,
        visibility_rule: Optional[str] = None,
        reason_deep: bool = False,
        layer_hint: Optional[str] = None,
        forgetting_rerank: bool = True,  # 2026-08-27: A/B 20/20 一致后默认开启
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
            include_docs: 是否包含 doc:* 知识库内容（默认 False——
                2026-08-24 R6 P0-① 记忆/知识分层；True = 知识检索面）。
            page_tree: 是否走页优先检索（PageIndex 式主题页树，2026-08-26
                Phase 1；默认 False 保持既有行为；需先 build_pagetree()）。
            page_k: 页树模式选页数（默认 3）。
            view: 命名记忆视图（Budibase 借鉴 Phase 2，~/.trinity/views.yaml）。
                展开为过滤/排序/截断；显式参数优先于视图缺省；视图不存在忽略。
                仅作用于基础检索路径（keyword/hybrid/graph/semantic）。
            visibility_rule: 行级可见性规则（Budibase 借鉴 Phase 3，白名单字段+
                参数化防注入），如 "importance >= 0.6 AND category != 'lme'"。
                解析失败时忽略；仅作用于基础检索路径。

        Returns:
            Dict with 'results' (匹配条目列表) and 'pushed_memories' (主动推送列表)。
        """
        raw_results: List[Dict[str, Any]] = []
        _view_spec: Optional[Dict[str, Any]] = None
        import time as _t0mod
        _t0 = _t0mod.time()

        # 2026-08-27（方向A 认知分层）：查询层感知——layer_hint=auto 按查询性质选层
        _layer_filter: Optional[str] = None
        if layer_hint == "auto":
            _layer_filter = _infer_layer(query)
        elif layer_hint:
            _layer_filter = layer_hint

        if self._adapter:
            # ── 记忆视图（Budibase 借鉴 Phase 2）：显式参数优先，视图补缺省 ──
            if view:
                try:
                    from trinity.views import resolve as _resolve_view, apply_view as _apply_view
                    _view_spec = _resolve_view(view)
                    if _view_spec:
                        if not category and _view_spec.get("categories"):
                            category = _view_spec["categories"][0] if len(_view_spec["categories"]) == 1 else None
                        if not persona_id and _view_spec.get("personas"):
                            persona_id = _view_spec["personas"][0] if len(_view_spec["personas"]) == 1 else None
                except Exception:
                    _view_spec = None
            # ── 页树模式（PageIndex 式，2026-08-26 Phase 1）──────────
            #   显式启用（默认关闭）；先定位页再读页内，基础召回兜底。
            if page_tree:
                return self.pagetree_search(
                    query=query, top_k=top_k, page_k=page_k,
                    persona_id=persona_id, tenant_id=tenant_id,
                    agent_id=agent_id, app_id=app_id, session_id=session_id,
                    category=category, include_docs=include_docs,
                )
            # ── reason 模式（Phase 3，2026-08-26）：LLM 相关重判 ──
            #   候选（关键词+页树）→ LLM 带活跃 goal 上下文判定相关 →
            #   重排输出；无 LLM key / 失败时静默回退候选原序。
            if (mode or "").lower() == "reason":
                return self._search_reason(
                    query=query, top_k=top_k,
                    persona_id=persona_id, tenant_id=tenant_id,
                    agent_id=agent_id, app_id=app_id, session_id=session_id,
                    category=category, include_docs=include_docs,
                    deep=reason_deep,
                )
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
                        include_docs=include_docs,
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
                            include_docs=include_docs,
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
                    include_docs=include_docs,
                    visibility_rule=visibility_rule,
                )


        # 2026-08-27（方向A 认知分层）：层过滤（layer_hint）
        if _layer_filter and raw_results:
            _kept = [r for r in raw_results if (r.get("memory_layer") or "ltm") == _layer_filter]
            if len(_kept) >= max(1, min(top_k, 3)):
                raw_results = _kept

        # 2026-08-27 (方向A 阶段3): 高遗忘分检索降权 - 后置不删除 (默认 off)
        if forgetting_rerank and raw_results:
            import time as _tm
            _now = _tm.time()
            def _fscore(x):
                try:
                    _la = str(x.get("last_accessed_at") or x.get("created_at") or "")
                    if len(_la) >= 19:
                        _ts = _tm.mktime(_tm.strptime(_la[:19], "%Y-%m-%dT%H:%M:%S"))
                    else:
                        _ts = _now
                    _idle = min(1.0, max(0.0, (_now - _ts) / 86400.0) / 90.0)
                except Exception:
                    _idle = 0.0
                _acc = min(1.0, max(0.0, 1.0 - float(x.get("access_count") or 0) / 20.0))
                return 0.5 * _idle + 0.3 * _acc + 0.2 * max(0.0, 1.0 - float(x.get("importance") or 0.5) / 0.6)
            raw_results = sorted(raw_results,
                key=lambda x: (1.0 if _fscore(x) < 0.6 else 0.0, x.get("score", 0) or 0),
                reverse=True)

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

        # 2026-08-24（R6 P1-③）：证据/置信度标注（区分"检索到"与"确定对"）
        try:
            raw_results = self._enrich_evidence(raw_results)
        except Exception:
            pass  # 标注失败不影响检索

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
                             "hits": len(raw_results), "memory_ids": memory_ids[:10],
                             "elapsed_ms": round((_t0mod.time() - _t0) * 1000, 1),
                             "layer": _layer_filter},
                )
            except Exception:
                pass

        # 2026-08-26（Budibase 借鉴 Phase 1）：事件驱动自动化——memory.search
        # 事件（默认关闭；emit 在规则未启用时零开销）。
        if _view_spec and raw_results:
            try:
                from trinity.views import apply_view as _apply_view
                raw_results = _apply_view(raw_results, _view_spec)
            except Exception:
                pass
        try:
            from trinity.automation import emit as _automation_emit
            _automation_emit(
                "memory.search",
                {
                    "query": query,
                    "top_k": top_k,
                    "mode": (mode or "hybrid") + ((":" + view) if view else ""),
                    "hit_count": len(raw_results),
                    "top_score": float(raw_results[0].get("score") or 0.0) if raw_results else 0.0,
                },
                audit_fn=lambda rule, ok, detail: self._adapter.write_audit_log(
                    memory_id=None, action="automation",
                    agent_id=agent_id, persona_id=persona_id,
                    details={"rule": rule, "ok": ok, **detail},
                ) if self._adapter else None,
            )
        except Exception:
            pass

        # 2026-08-27（Claude-Mem 对比 P1-2）：渐进式披露——token 成本可见性。
        # 每条结果附 est_tokens（中文 ~2 字符/token 估算），响应附 usage 汇总
        # （deepseek-chat 输入价 $0.14/M tokens 估算；TRINITY_TOKEN_COST_PER_K 可覆盖）。
        try:
            _tok_total = 0
            for _rr in raw_results:
                _c = str(_rr.get("content") or _rr.get("content_preview") or "")
                _t = max(1, len(_c) // 2)
                _rr["est_tokens"] = _t
                _tok_total += _t
            _price_k = float(os.environ.get("TRINITY_TOKEN_COST_PER_K", "0.00014"))
            _usage = {"est_tokens": _tok_total,
                      "est_cost_usd": round(_tok_total / 1000.0 * _price_k, 6)}
        except Exception:
            _usage = {}
        return {
            "results": raw_results,
            "pushed_memories": pushed,
            "usage": _usage,
        }
    def _enrich_evidence(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """2026-08-24（R6 P1-③）：检索结果附证据/置信度标注。

        对齐 2026 可信记忆方向（ERA: Evidence-based Reliability Alignment、
        mnemos 证据背书）：区分"检索到了"与"确定是对的"——
          - evidence: 来源（category/source_uri）、版本数（多版本=多次确认）、
            审计可查（audit_available）；
          - confidence: importance 加权 + 版本数修正，弱证据（无版本/低
            importance）标注 "verify"（需复核）。
        只读补充（从 adapter 查版本数，失败降级），不改变排序。
        """
        enriched = []
        for r in results or []:
            rec = dict(r)
            mid = rec.get("memory_id") or rec.get("id")
            category = rec.get("category") or ""
            importance = float(rec.get("importance") or 0.5)
            version_count = 0
            if mid and self._adapter is not None:
                try:
                    chain = self._adapter.get_version_chain(mid) or []
                    version_count = len(chain)
                except Exception:
                    version_count = 0
            # confidence：importance 0-1 与版本修正（≥2 版本 +0.1，0 版本 -0.15）
            confidence = min(1.0, max(0.0, importance + (0.1 if version_count >= 2 else -0.15)))
            rec["evidence"] = {
                "category": category,
                "source_uri": rec.get("source_uri") or "",
                "version_count": version_count,
                "audit_available": bool(mid and self._adapter is not None),
            }
            rec["confidence"] = round(confidence, 3)
            if confidence < 0.4:
                rec["verify_hint"] = "需复核（弱证据：低 importance 或无版本链）"
            enriched.append(rec)
        return enriched
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
                            # 2026-09 (EXECUTION 118): 突触权重衰减——
                            # 自适应半衰期：高重要性（强突触）衰减更慢，
                            # 高访问频率（突触使用）也增强持久性。
                            # half_life_eff = half_life * (1 + imp*K1) * (1 + min(acc,cap)*K2)
                            _imp = float(item.get("importance") or 0.5)
                            _acc = int(item.get("access_count") or 0)
                            _syn_k1 = float(getattr(self, "synapse_importance_boost", 1.0))
                            _syn_k2 = float(getattr(self, "synapse_access_boost", 0.15))
                            _hl = half_life_days * (1.0 + _imp * _syn_k1) * (1.0 + min(_acc, 20) * _syn_k2)
                            time_decay_score = math.pow(2, -days_since / max(_hl, 0.5))
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

            # 2026-09 (PG 融合): PG adapter 且已回填向量 → 直接 pgvector HNSW 直查
            # （免全量拉取 + 免内存重建；未回填/失败自动回退下方内存 ANN 路径）
            if self._adapter and type(self._adapter).__name__.lower().find("postgres") >= 0:
                try:
                    _pgv = self._adapter.vector_search(
                        query_vec, top_k=top_k,
                        agent_id=getattr(self, "_search_agent_id", None),
                        persona_id=getattr(self, "_search_persona_id", None),
                        tenant_id=getattr(self, "_search_tenant_id", None),
                    )
                    if _pgv:
                        return _pgv
                except Exception as _pgexc:  # noqa: BLE001 列/索引未就绪时回退内存路径
                    logger = __import__("logging").getLogger("trinity.core.client")
                    logger.warning("pgvector search failed, falling back to in-memory ANN: %s", _pgexc)

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
        # 2026-08-25（可测域扩展）：tuning env 变化时重建实例——A/B 的
        # base/exp 同进程运行，共享实例会让 exp 的权重 env 不生效。
        _tune_env = ("TRINITY_VECTOR_WEIGHT", "TRINITY_BM25_WEIGHT",
                     "TRINITY_GRAPH_WEIGHT", "TRINITY_AGGREGATOR_WEIGHT",
                     "TRINITY_PROCEDURAL_WEIGHT", "TRINITY_RRF_K",
                     "TRINITY_BM25_K1", "TRINITY_BM25_B",
                     "TRINITY_PAGETREE_HYBRID")
        _sig = tuple(os.environ.get(k, "") for k in _tune_env)
        if self._hybrid_retriever is not None and getattr(
                self, "_hybrid_sig", None) != _sig:
            self._hybrid_retriever = None  # env 变化 → 重建
            # 2026-08-25（BM25 k1/b 维度）：k1/b 变化时 BM25 索引也需重建
            # （索引用 k1/b 计算分数，旧索引分数无效）。
            if os.environ.get("TRINITY_BM25_K1") or os.environ.get("TRINITY_BM25_B"):
                self._bm25_index = None
                self._bm25_ready = False
        if self._hybrid_retriever is None:
            self._hybrid_sig = _sig
            from trinity.retrieval import HybridRetriever, BM25Index, GraphRetriever

            if self._bm25_index is None:
                self._ensure_bm25_index()
            # 2026-08-25（新维度）：BM25 k1/b 支持 env 覆盖——经典 BM25 参数，
            # 直接影响关键词检索排序（默认 k1=1.5/b=0.75）。
            bm25 = self._bm25_index or BM25Index(
                k1=float(os.environ.get("TRINITY_BM25_K1", "1.5")),
                b=float(os.environ.get("TRINITY_BM25_B", "0.75")),
            )
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

            # 2026-08-24（R8 P1-4）：PPR 图谱通道——实体种子 + 记忆关联图
            # PPR 扩散（HippoRAG 式），供 HybridRetriever 图谱通道增强。
            def _ppr_fn(q: str, top_k: int):
                if self._adapter is None:
                    return []
                # 实体种子：实体名模糊匹配 → 关联记忆
                seed_mids = set()
                try:
                    entities = self._adapter.search_entities(name=q, etype=None, limit=8)
                    for e in entities:
                        eid = e.get("entity_id") or e.get("id")
                        if not eid:
                            continue
                        links = self._adapter.get_all_links(eid)
                        for link in links:
                            mid = link.get("source_id") or link.get("target_id") or link.get("memory_id")
                            if mid and mid != eid:
                                seed_mids.add(mid)
                except Exception:
                    pass
                # 直接查询记忆作为种子兜底
                if not seed_mids:
                    try:
                        hits = self._adapter.search_memories(query=q, top_k=8)
                        for h in hits:
                            mid = h.get("memory_id") or h.get("id")
                            if mid:
                                seed_mids.add(mid)
                    except Exception:
                        return []
                if not seed_mids:
                    return []
                # 邻接表：从种子出发逐层 BFS 收集 2 跳 memory_links（不预载全图）
                graph: Dict[str, Dict[str, Any]] = {}
                frontier = list(seed_mids)
                seen_nodes = set(seed_mids)
                for _ in range(2):
                    nxt = []
                    for mid in frontier:
                        try:
                            links = self._adapter.get_all_links(mid)
                        except Exception:
                            links = {"outgoing": [], "incoming": []}
                        for link in links.get("outgoing", []):
                            tgt = link.get("target_id")
                            if tgt:
                                graph.setdefault(mid, {})[tgt] = link.get("link_type", "semantic")
                                graph.setdefault(tgt, {})[mid] = link.get("link_type", "semantic")
                                if tgt not in seen_nodes:
                                    seen_nodes.add(tgt)
                                    nxt.append(tgt)
                        for link in links.get("incoming", []):
                            src = link.get("source_id")
                            if src:
                                graph.setdefault(src, {})[mid] = link.get("link_type", "semantic")
                                graph.setdefault(mid, {})[src] = link.get("link_type", "semantic")
                                if src not in seen_nodes:
                                    seen_nodes.add(src)
                                    nxt.append(src)
                    frontier = nxt
                    if not frontier:
                        break
                if not graph:
                    return []
                try:
                    from trinity.kgraph.ppr_core import ppr_from_graph
                    hits = ppr_from_graph(graph, list(seed_mids), top_k=top_k * 2)
                    out = []
                    for h in hits:
                        mid = h.get("id")
                        if not mid:
                            continue
                        out.append({
                            "id": mid,
                            "memory_id": mid,
                            "score": float(h.get("score", 0.0)),
                            "content": "",
                        })
                    return out
                except Exception as exc:
                    logger.debug("PPR search failed, fallback: %s", exc)
                    return []

            # 2026-08-26（PageIndex 借鉴 Phase 1）：页树通道 fn——
            # TRINITY_PAGETREE_HYBRID=on 且页树已构建时接入 hybrid 融合。
            _pt_fn = None
            if os.environ.get("TRINITY_PAGETREE_HYBRID", "off").strip().lower() in ("1", "on", "true", "yes"):
                try:
                    if self.load_pagetree() is not None:
                        # novel_only：页树通道只贡献基础召回未命中的记忆（只增不减）
                        _pt_fn = lambda q, k: self.pagetree_search(
                            query=q, top_k=k, novel_only=True,
                        ).get("results", [])
                except Exception:
                    _pt_fn = None
            # 2026-08-25（可测域扩展）：通道权重 + rrf_k 支持 env 覆盖——
            # 此前硬编码默认（vector 0.35/bm25 0.25/graph 0.25/agg 0.15/proc 0.10,
            # rrf_k 60），自进化 A/B 无法测。env 未设时行为不变（向后兼容）。
            _w = lambda k, d: float(os.environ.get(k, str(d)))
            self._hybrid_retriever = HybridRetriever(
                bm25_index=bm25,
                graph_retriever=graph,
                search_fn=_vector_search_fn,
                ppr_fn=_ppr_fn,
                vector_weight=_w("TRINITY_VECTOR_WEIGHT", 0.35),
                bm25_weight=_w("TRINITY_BM25_WEIGHT", 0.25),
                # 2026-08-25（P4 排序优先简化）：默认 0.25→0.1——SmartSearch 验证：
            # n=20 nDCG 下 GW 0.1/0 均 delta=0（图谱通道在当前评测配置无贡献），
            # 降权 60% 无损并降低图谱检索开销；可用 TRINITY_GRAPH_WEIGHT 覆盖。
            graph_weight=_w("TRINITY_GRAPH_WEIGHT", 0.1),
                aggregator_weight=_w("TRINITY_AGGREGATOR_WEIGHT", 0.15),
                procedural_weight=_w("TRINITY_PROCEDURAL_WEIGHT", 0.10),
                rrf_k=int(os.environ.get("TRINITY_RRF_K", "60")),
                # 2026-08-26（PageIndex 借鉴 Phase 1）：页树通道——
                # env TRINITY_PAGETREE_HYBRID=on 且页树存在时接入融合。
                pagetree_fn=_pt_fn,
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
            # 2026-08-25（新维度）：BM25 k1/b env 覆盖（默认 1.5/0.75）
            self._bm25_index = BM25Index(
                k1=float(os.environ.get("TRINITY_BM25_K1", "1.5")),
                b=float(os.environ.get("TRINITY_BM25_B", "0.75")),
            )  # 空索引立即可用（优雅降级）
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
    def _rrf_merge(
        self,
        a: List[Dict[str, Any]],
        b: List[Dict[str, Any]],
        top_k: int,
        k: int = 60,
    ) -> List[Dict[str, Any]]:
        """RRF 融合两路结果（2026-09，EXECUTION 104.9）：按 rank 加权合并。"""
        scores: Dict[str, Dict[str, Any]] = {}
        for rank, item in enumerate(a):
            mid = item.get("memory_id") or item.get("id")
            if not mid:
                continue
            entry = scores.setdefault(mid, {"item": item, "s": 0.0})
            entry["s"] += 1.0 / (k + rank + 1)
        for rank, item in enumerate(b):
            mid = item.get("memory_id") or item.get("id")
            if not mid:
                continue
            entry = scores.setdefault(mid, {"item": item, "s": 0.0})
            entry["s"] += 1.0 / (k + rank + 1)
        ranked = sorted(scores.values(), key=lambda x: -x["s"])
        return [x["item"] for x in ranked[:top_k]]

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
          环境变量 TRINITY_ADAPTIVE_ROUTING=off 可关闭；默认 on——
          2026-08-24（R8 P0-3）：短查询走 FTS 轻通道是引擎已验证的最优路径
          （FTS R@5=0.975 > hybrid-rrf 0.942），此前默认 off 使该性能特性空转。
        """
        # 2026-08-29 (PG): non-SQLite adapter (PG) - force light (BM25 index not built for PG)
        _pg_mode = self._adapter is not None and type(self._adapter).__name__.lower().find("postgres") >= 0
        if _pg_mode:
            routing = "light"
        env = os.environ.get("TRINITY_ADAPTIVE_ROUTING", "on").strip().lower()
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
            # 2026-09（EXECUTION 104.9）：PG 主存储 light 路径补向量融合——
            # tsvector simple 对中文分词无效（FTS 召回中文语义查询为空，
            # 实测 "用户偏好 咖啡" FTS=0 / 向量=3），pgvector HNSW 直查 +
            # RRF 融合恢复语义召回；失败静默回退纯 FTS（行为与之前一致）。
            _pg = type(self._adapter).__name__.lower().find("postgres") >= 0
            if _pg and hasattr(self._adapter, "vector_search"):
                try:
                    from trinity.core.client._helpers import _get_embedding_engine
                    _eng = _get_embedding_engine()
                    if _eng is not None:
                        _qv = _eng.embed(query)
                        _vec = self._adapter.vector_search(
                            _qv, top_k=max(top_k * 2, 10),
                            agent_id=agent_id or None,
                            persona_id=persona_id or None,
                            tenant_id=tenant_id or None,
                        )
                        if _vec:
                            results = self._rrf_merge(results, _vec, top_k)
                except Exception:
                    pass
            # 2026-09 (P1-1): CrossEncoder 两阶段 rerank——RRF 融合后对 top
            # candidates 语义精排；模型不可用/加载失败自动降级 no-op（原行为）。
            try:
                from trinity.vector_index.reranker import CrossEncoderReranker
                _rk = getattr(self, "_reranker", None)
                if _rk is None:
                    _rk = CrossEncoderReranker(model_name="chinese")
                    self._reranker = _rk
                if results:
                    _rk_results = _rk.rerank(
                        query=query,
                        candidates=results,
                        top_k=top_k,
                        text_key="content",
                        id_key="memory_id",
                        score_key="rerank_score",
                    )
                    if _rk_results:
                        results = _rk_results
            except Exception:
                pass
            # 2026-09 (EXECUTION 116): DCPM 双过程钩子——System1 快路径信念命中
            # + 元认知置信评估（大脑化：检索即信念验证）
            try:
                from trinity.brain.metacognition import assess_confidence
                _conf = assess_confidence(results, channels=["fts", "vector"] if _pg else ["fts"])
                result = {
                    "results": results,
                    "strategy": "light",
                    "query": query,
                    "breakdown": {"routing": "light", "channels": ["fts", "vector"] if _pg else ["fts"]},
                    "metacognition": _conf,
                }
                # System1：高信心时持久化信念命中（PG，跨进程可见；不阻塞，失败静默）
                if _conf.get("level") in ("high", "medium") and results:
                    _top = results[0].get("content", "")[:200]
                    try:
                        if self._adapter is not None and hasattr(self._adapter, "dcpm_store_belief"):
                            self._adapter.dcpm_store_belief(
                                belief_id=__import__("uuid").uuid4().hex[:12],
                                subject=query[:60], predicate="retrieved", obj=_top,
                            )
                    except Exception:
                        pass
            except Exception:
                result = {
                    "results": results,
                    "strategy": "light",
                    "query": query,
                    "breakdown": {"routing": "light", "channels": ["fts", "vector"] if _pg else ["fts"]},
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
