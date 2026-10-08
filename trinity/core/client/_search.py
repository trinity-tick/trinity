"""Trinity client - search, ranking & hybrid retrieval mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
from pathlib import Path  # 2026-09-11: threading 随尾部迁至 _hybrid_index.py
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
from ._helpers import _fuse_results, _get_embedding_engine, _get_vector_index
from . import _corpus_persist as _cp, _access_account as _acct  # t28：出口记账（R-6/R-8）
from ._base import _ClientMixinBase

# 2026-09-29（Round 27）：`mode="graph"` 的回退只喊一次的进程级标记。
_GRAPH_FALLBACK_WARNED: bool = False
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

# 2026-10-06（复评 F6 收尾）：查询分层推断与其词表**已迁出**到 `_layer_infer.py`。
# 动机：本文件撞上 `docs/STRUCTURE_BUDGETS.json` 的 1400 行硬预算（迁出前 1399/1400，
# 只剩 1 行余量）。同一迁出先例见下方 `_embed_query_bounded` -> `_vec_budget.py`。
# 以原名 `_infer_layer` 导入回去 ⇒ 既有调用点（本文件 L136）与任何外部引用都不变。
from ._layer_infer import (  # noqa: E402  （本文件顶部有 sys.path 操纵，导入不在最前）
    _KNOWLEDGE_WORDS,
    _TIME_WORDS,
    infer_layer as _infer_layer,
)

# 2026-09-09 闭环修复：检索默认排除的类别。
# perception = 原始感知流（非记忆，由 perception_recall 专用路径消费）；
# benchmark/lme/stress-test = 评测语料（10 万+ 条，与生产同库）——
# 由 TRINITY_RETRIEVAL_EXCLUDE_EVAL 控制（默认 on），关闭后行为回到修复前。
_RETRIEVAL_EXCLUDE_CATEGORIES: List[str] = ["perception"]
if os.environ.get("TRINITY_RETRIEVAL_EXCLUDE_EVAL", "on").lower() in ("on", "1", "true", "yes"):
    _RETRIEVAL_EXCLUDE_CATEGORIES += ["benchmark", "lme", "stress-test"]

def _embed_query_bounded(engine, text, timeout_s: float):
    """薄封装：实现在 `_vec_budget.py`（2026-09-29 为守 `_search.py` 行数预算而迁出）。"""
    from ._vec_budget import embed_query_bounded
    return embed_query_bounded(engine, text, timeout_s)

class _SearchMixin(_ClientMixinBase):
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
        routing: Optional[str] = None,  # 2026-10-06: 贯穿 hybrid 的 light/full 预算路由
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
            routing: hybrid 预算路由透传（light/full/auto/None）。
                **None = 不指定**（沿用引擎默认，行为逐字不变）；显式给值时透传给
                `search_hybrid(routing=...)` —— 此前该参数在 `search_hybrid` 上存在
                但**从不被本入口透传**，文档所称"短查询走 light 快通道"对引擎入口
                因此不成立（引擎恒走 full）。仅对 hybrid 融合路径有意义。

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
                    category=category, include_docs=include_docs, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
                )
            # ── reason 模式（Phase 3，2026-08-26）：LLM 相关重判 ──
            #   候选（关键词+页树）→ LLM 带活跃 goal 上下文判定相关 →
            #   重排输出；无 LLM key / 失败时静默回退候选原序。
            if (mode or "").lower() == "reason":
                return self._search_reason(
                    query=query, top_k=top_k,
                    persona_id=persona_id, tenant_id=tenant_id,
                    agent_id=agent_id, app_id=app_id, session_id=session_id,
                    category=category, include_docs=include_docs, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
                    deep=reason_deep,
                )
            _mode = (mode or "hybrid").lower()
            # ── 真实 mode 路由（GEN-2，修复"mode 参数装饰性"）──────────
            #   keyword/exact→FTS5；semantic→向量（不可用回退 FTS5）；
            #   hybrid→5 通道融合（仅当 hybrid retriever 已初始化）；graph→未实现（见下）。
            # 2026-08-17 二轮验证（scripts/verify_engine_default.py，同 120 题同摄入 A/B）：
            #   FTS R@5=0.975 > hybrid-rrf 0.942 ⇒ 引擎默认保持 FTS；hybrid 只对显式
            #   调用 search_hybrid 的路径生效（脚本 calibrate_ranking.py 已标定
            #   fusion 静态权重 R@5=0.008 vs rrf R@5=0.950 ⇒ 默认 strategy=rrf）。
            _vector_available = (
                hasattr(self._adapter, "_fts_available")
                and self._adapter._fts_available()
            )
            _use_hybrid = (
                _mode == "hybrid"
                and self._hybrid_retriever is not None
            )
            if _use_hybrid:
                # 2026-09-02（brain fix）：先等 BM25 预构建完成再跑 hybrid 检索，
                # 消除构建线程与首搜惰性导入的并发崩溃竞态。
                self._wait_bm25_ready()
            _use_graph = (
                _mode in ("graph", "hybrid")
                and hasattr(self._adapter, "search_graph")
            )
            # 2026-09-29（外部审计 Round 27）：**让回退可见**。
            # 上方注释（:168）写明「graph → 图谱检索（adapter 支持时）；否则回退 FTS5」——
            # 回退本身是**已文档化的行为**；但实测 `search_graph` 在**两个适配器上都不存在**
            # （全仓零定义）⇒ `_use_graph` 恒为 False、`mode="graph"` **100% 都是回退**，
            # 而调用方**完全看不出来**。误导不在"回退"，而在"**静默**回退"。
            if _mode == "graph" and not _use_graph:
                self._graph_mode_degraded = True           # 供上层/诊断读取
                global _GRAPH_FALLBACK_WARNED
                if not _GRAPH_FALLBACK_WARNED:
                    _GRAPH_FALLBACK_WARNED = True           # 每进程只喊一次，避免刷屏
                    __import__("logging").getLogger(__name__).warning(
                        "mode='graph' 已请求但**该能力不存在**（适配器 %s 无 search_graph；"
                        "全仓从未实现）⇒ 本次以普通 FTS 检索返回。"
                        "若要真正的图检索需实现该能力；若要撤掉该宣称请从 mode 列表移除 'graph'。",
                        type(self._adapter).__name__)
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
                        include_docs=include_docs, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
                    )
            elif _use_hybrid:
                try:
                    self.hybrid_retriever  # 懒初始化（BM25 后台预热，非阻塞）
                    _hkw = {
                        "query": query, "top_k": top_k, "strategy": "rrf",
                        "agent_id": agent_id, "persona_id": persona_id,
                        "tenant_id": tenant_id,
                    }
                    if routing is not None:  # None = 不指定，不改默认行为
                        _hkw["routing"] = routing
                    raw_results = self.search_hybrid(**_hkw).get("results", [])
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
                            include_docs=include_docs, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
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
                            # D1(2026-10-06)：BLOB 裸字节不得进 JSON 出口（jsonable_encoder 会 decode() ⇒ 500；同 _routers_memories.py:490-495 的处置）
                            rec.pop("embedding", None)
                            rec.setdefault("score", rec.get("hybrid_score", 0.0))
                            enriched.append(rec)
                        raw_results = enriched
                    except Exception as _e:
                        swallow(__name__, _e)
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
                    include_docs=include_docs, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
                    visibility_rule=visibility_rule,
                )

        # ── 658.91：**多查询展开（生产，任意模式生效）**────────────────────────
        # 依据 658.89 诊断：长历史检索瓶颈 **55.6% 在召回**、仅 16.7% 在排序；评测同池实测
        # recall@100 由 0.444 → 0.611（+37.5%）。做法：原句检索后，再用 jieba 实词
        # （去疑问词/停用词）构造关键词查询检索一次，两路候选按 RRF 合并。
        # **注意**：必须放在模式分支汇合之后——生产默认走 FTS 路径（标定 FTS R@5 0.975 >
        # hybrid 0.942），若插在 hybrid 分支内则生产上永不执行（实测踩过）。
        # 658.93：**默认改为 off**——同池 A/B：仅"词覆盖重排"即达 R@1 0.222 / R@5 0.444 /
        # MRR 0.294；叠加 MQ 后**最终指标完全相同**（多出的候选未进前 5），却要付长查询
        # **581ms → 3752ms（6.5×）** 的延迟。故降级为 opt-in（`TRINITY_MQ=on`）。
        if raw_results and os.environ.get("TRINITY_MQ", "off").strip().lower() not in ("off", "0", "false", "no"):
            try:
                import jieba as _jbq
                _jbq.setLogLevel(60)
                _STOPQ = set(("what when where who which how why did do does is are was were "
                              "the a an of in on at to for and or not i my me you your it its "
                              "that this these those many much number").split())
                _kws = [w.strip() for w in _jbq.cut(str(query or ""))
                        if len(w.strip()) >= 2 and w.strip().lower() not in _STOPQ]
                _mq_mode = os.environ.get("TRINITY_MQ", "off").strip().lower()
                if _mq_mode in ("on", "1", "true", "yes") or len(_kws) >= 6:
                    _kwq = " ".join(_kws[:10])
                    if _kwq and _kwq != str(query or "").strip():
                        try:
                            _r2 = self.search_hybrid(
                                query=_kwq, top_k=top_k, strategy="rrf",
                                agent_id=agent_id, persona_id=persona_id,
                                tenant_id=tenant_id).get("results", [])
                        except Exception:
                            _r2 = []
                        if _r2:
                            _rrf = {}
                            for _lst in (raw_results, _r2):
                                for _i, _r in enumerate(_lst):
                                    _cid = str(_r.get("memory_id") or _r.get("id") or id(_r))
                                    _rrf[_cid] = _rrf.get(_cid, 0.0) + 1.0 / (60 + _i + 1)
                            _seen = set()
                            _merged = []
                            for _r in list(raw_results) + list(_r2):
                                _cid = str(_r.get("memory_id") or _r.get("id") or id(_r))
                                if _cid not in _seen:
                                    _seen.add(_cid)
                                    _merged.append(_r)
                            _merged.sort(key=lambda _r: -_rrf.get(
                                str(_r.get("memory_id") or _r.get("id") or id(_r)), 0.0))
                            raw_results = _merged[:max(top_k, len(raw_results))]
                            try:
                                from trinity.core.client._hybrid_search import _signal_tick
                                _signal_tick("mq_expanded", 1)
                            except Exception as _e:
                                swallow(__name__, _e)
            except Exception as _e:
                swallow(__name__, _e)

        # ── 658.92：**词覆盖重排（生产，任意模式生效）**─────────────────────────
        # 依据：长历史分层评测同池 A/B（n=18）——基线 R@1 0.111 / R@5 0.278 / MRR 0.169；
        # 加多查询展开后 R@5 0.333；再叠加**词覆盖重排（w=0.9）**后达 **R@1 0.222（×2）/
        # R@5 0.444（+60%）/ MRR 0.294（+74%）**，其中 **multi-session 由 R@1 0 → 0.667**。
        # 结论：**融合分数（RRF/向量相似度）对长历史是差排序器，而"查询实词在块中的覆盖率"
        # 显著更好**；且 w>=0.9 后结果饱和（0.9/0.95/1.0 完全相同）。
        # 实现：确定性、无模型、无额外检索——按 (1-w)*原名次分 + w*覆盖率 重排。
        # 门控 TRINITY_COV_RERANK：权重值；**默认 off**。
        # 658.98（重要修正）：658.92 曾报「R@5 +60%」，但那是**在评测池 1.67 万行缺向量的
        # 状态下**测得的（见 658.96）。向量通道修复后重测（n=18，同池）：
        #   有向量 基线      R@1 0.056 / R@5 0.278 / MRR 0.139
        #   有向量 +重排0.9  R@1 0.111 / R@5 0.222 / MRR 0.150   ← R@5 反而 -1 题
        # 差异均在 1 题（≈0.055）粒度内 → 证据不足，故**默认关闭**，保留机制待 n>=60 验证。
        if raw_results and len(raw_results) > 1:
            try:
                _cw_raw = os.environ.get("TRINITY_COV_RERANK", "off").strip().lower()
                if _cw_raw not in ("off", "0", "false", "no", "0.0"):
                    _cw = max(0.0, min(1.0, float(_cw_raw)))
                    import jieba as _jbc
                    _jbc.setLogLevel(60)
                    _STOPC = set(("what when where who which how why did do does is are was were "
                                  "the a an of in on at to for and or not i my me you your it its "
                                  "that this these those many much number").split())
                    _qkc = [w.strip() for w in _jbc.cut(str(query or ""))
                            if len(w.strip()) >= 2 and w.strip().lower() not in _STOPC]
                    if _qkc and _cw > 0:
                        def _cov_score(_ir):
                            _i2, _r2r = _ir
                            _blob = str(_r2r.get("content") or _r2r.get("text") or "").lower()
                            _cov = sum(1 for _t in _qkc if _t in _blob) / float(len(_qkc))
                            return -((1.0 - _cw) * (1.0 / (_i2 + 1)) + _cw * _cov)
                        _reranked = [r for _, r in sorted(enumerate(raw_results), key=_cov_score)]
                        # 仅当重排真的改变顺序时才采纳（避免无谓开销与日志噪音）
                        if [id(x) for x in _reranked] != [id(x) for x in raw_results]:
                            raw_results = _reranked
                            try:
                                from trinity.core.client._hybrid_search import _signal_tick
                                _signal_tick("coverage_rerank", 1)
                            except Exception as _e:
                                swallow(__name__, _e)
            except Exception as _e:
                swallow(__name__, _e)

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
            self._last_query = query  # EXECUTION 132: affect 钩子用
            raw_results = self._apply_layered_ranking(
                raw_results=raw_results,
                top_k=top_k,
                agent_weight=agent_weight,
            )

        # 2026-08-24（R6 P1-③）：证据/置信度标注（区分"检索到"与"确定对"）
        try:
            raw_results = self._enrich_evidence(raw_results)
        except Exception as _e:
            swallow(__name__, _e)  # 标注失败不影响检索

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
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-08-26（Budibase 借鉴 Phase 1）：事件驱动自动化——memory.search
        # 事件（默认关闭；emit 在规则未启用时零开销）。
        if _view_spec and raw_results:
            try:
                from trinity.views import apply_view as _apply_view
                raw_results = _apply_view(raw_results, _view_spec)
            except Exception as _e:
                swallow(__name__, _e)
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
        except Exception as _e:
            swallow(__name__, _e)

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

        # 2026-09-02（brain fix）：检索出口统一解密（enc:v1 → 明文；fail-open）
        # SQLite 侧存储加密默认开启（**PG 侧无实装**，口径更正见
        # `trinity/security/crypto.py` 模块头注与 `docs/SECURITY_BOUNDARIES.md`，
        # 2026-09-29）⇒ content 密文落盘，此前所有检索路径均未解密——
        # 此处为 search() 唯一出口，覆盖 hybrid/semantic/graph/exact/keyword。
        from trinity.security.crypto import decrypt_content
        for _rr in raw_results:
            if isinstance(_rr, dict) and _rr.get("content"):
                _rr["content"] = decrypt_content(_rr["content"])
        for _pm in pushed:
            if isinstance(_pm, dict) and _pm.get("content"):
                _pm["content"] = decrypt_content(_pm["content"])

        # 2026-09-13（P2 LLM 列表式重排）：默认 off（TRINITY_LLM_RERANK=on 开启）。
        # 位置在门控**之前**：先排序、再判相关度。fail-open：异常/超时保留原顺序。
        try:
            from trinity.retrieval.llm_rerank import enabled as _lr_on, llm_rerank as _lr
            if _lr_on():
                raw_results = _lr(query, raw_results)
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-13（P0 证据门控）：search() 唯一出口统一挂相关度判定。
        # 实测动机：出口永远返回 top_k、无任何门控 → "我的车牌号是多少" 返回 5 条
        # 历史 [Session Start] 任务提示，"trip to Japan" 返回 WMS 仓储文档。
        # 默认**只标注**（evidence_gate/abstain），不删结果、不改排序；
        # TRINITY_EVIDENCE_GATE_STRICT=on 才清空。详见 retrieval/evidence_gate.py。
        _gate_verdict = None
        _abstain = False
        _eval_dropped = 0
        try:
            from trinity.retrieval.evidence_gate import apply_evidence_gate
            # 归属信息用于识别评测/压测语料（P1-H 隔离）——一次批量查询，top_k 有界
            _owners = None
            try:
                if self._adapter is not None and hasattr(self._adapter, "get_memory_owners"):
                    _mids = [r.get("memory_id") for r in raw_results
                             if isinstance(r, dict) and r.get("memory_id")]
                    if _mids:
                        _owners = self._adapter.get_memory_owners(_mids)
            except Exception as _e:
                swallow(__name__, _e)
            # scope_agent/scope_persona：显式把检索限定在评测命名空间时豁免隔离
            # （否则基准会滤掉自己的语料；persona 这条腿 2026-09-16 补齐）
            # 2026-09-19（Jev 借鉴）：把检索层 top confidence 透传给门控，
            # 让"不确定带"有真实概率源。缺省 None ⇒ 带内判定不触发，行为不变
            # （_enrich_evidence 在 :437 已接线，故置信度通常存在）。
            _top_conf = None
            try:
                if raw_results:
                    _c = (raw_results[0] or {}).get("confidence") if isinstance(raw_results[0], dict) else None
                    if _c is not None:
                        _c = float(_c)
                        if 0.0 <= _c <= 1.0:
                            _top_conf = _c
            except Exception as _e:
                swallow(__name__, _e)
            _g = apply_evidence_gate(query, {"results": raw_results, "top_confidence": _top_conf},
                                     source="search", owners=_owners, scope_agent=agent_id,
                                     scope_persona=persona_id)
            _gate_verdict = _g.get("evidence_gate")
            _abstain = bool(_g.get("abstain"))
            _eval_dropped = int(_g.get("eval_corpus_dropped") or 0)
            raw_results = _g.get("results", raw_results)
        except Exception as _e:
            swallow(__name__, _e)
        return {
            "results": raw_results,
            "pushed_memories": pushed,
            "usage": _usage,
            "evidence_gate": _gate_verdict,
            "abstain": _abstain,
            "eval_corpus_dropped": _eval_dropped,
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

    # 2026-09 (EXECUTION 139): 连续状态——自动情境（最近查询 + 感知事件）
    def _build_auto_situation(self) -> str:
        """构造自动情境：上次查询 + 最近感知记忆的关键词。

        模拟大脑的"当下"：检索默认带最近上下文（会话延续）。
        失败返回空串（保持无情境行为）。
        """
        try:
            parts = []
            # 2026-09-02 (EXECUTION 457): 情境明文助手——解密 enc:v1 + 滤噪音
            # （raw PG 读取无解密，密文/错误串会污染情境嵌入）
            def _plain(_v, _max=None):
                try:
                    _s = str(_v or "").strip()
                    if _s.startswith("enc:v1:"):
                        try:
                            from trinity.security.crypto import decrypt_content as _dc
                            _s = str(_dc(_s) or "").strip()
                        except Exception:
                            return ""
                    if _s.startswith("enc:v1:"):
                        return ""  # 解密失败 → 丢弃密文
                    _s = _s.replace("[self-identity] ", "").replace("[vision] ", "")
                    if _s.lower().startswith("error") or "fserror" in _s[:80].lower():
                        return ""
                    return _s[:_max] if _max else _s
                except Exception:
                    return ""
            _lq = getattr(self, "_last_query", "")
            # 2026-09 (EXECUTION 149): 自我模型雏形——会话身份进入情境
            try:
                from trinity.brain.self_model import build_identity as _bi
                _sidm = getattr(self, "_last_session_id", None) or "default"
                _pctx2 = None
                if self._adapter is not None and hasattr(self._adapter, "context_load"):
                    _pctx2 = self._adapter.context_load(_sidm)
                _aff2 = (_pctx2 or {}).get("affect") if _pctx2 else None
                _lq2 = (_pctx2 or {}).get("last_query", "") if _pctx2 else ""
                _idn = _bi(_lq2 or _lq, _aff2)
                if _idn:
                    parts.append(_idn)
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09 (EXECUTION 174): 全局自我——持续身份进入情境（跨会话）
            try:
                import psycopg2 as _pg2
                _gconn = _pg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                     user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))
                _gcur = _gconn.cursor()
                _gcur.execute("SELECT content FROM memories WHERE category='self-identity' ORDER BY created_at DESC LIMIT 1")
                _grow = _gcur.fetchone()
                _gconn.close()
                if _grow:
                    _gid = _plain(_grow[0], 110)
                    if _gid and _gid not in parts:
                        parts.append("[自我] " + _gid)
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-02 (EXECUTION 457): 情境持续上下文流——"当下"摘要注入
            # （意识蓝图情境维度从"按查询现算"升级为"持续在线上下文"）
            try:
                from trinity.brain.situation_stream import get_stream as _gs
                _sx = _gs(max_age_sec=600, allow_refresh=True)
                if _sx and _sx not in parts:
                    parts.append(str(_sx)[:140])
            except Exception as _e:
                swallow(__name__, _e)
            # EXECUTION 141: persistent context load
            _pctx = None
            if self._adapter is not None and hasattr(self._adapter, "context_load"):
                try:
                    _pctx = self._adapter.context_load(getattr(self, "_last_session_id", None) or "default")
                except Exception:
                    _pctx = None
            if _pctx and _pctx.get("last_query"):
                _lq = _pctx.get("last_query")
                self._last_query = _lq
            if _lq:
                parts.append(str(_lq)[:60])
            # 最近感知事件（process 内缓存，避免每次查库）
            _pc = getattr(self, "_recent_percepts", None)
            if _pc is None:
                _pc = []
                self._recent_percepts = _pc
                try:
                    # 2026-10-06：原为 `hasattr(self._adapter, "_get_conn")` + **硬编码 psycopg2**
                    # （host/port/dbname/user 全写死），即绕开适配器在客户端里直连 PG：非默认
                    # PG 连接参数的部署上静默失败，且 SQLite 后端上该能力**恒不可用**。改为
                    # **方言正确**写法（复用本仓 `trinity._tags._conn_ctx`：
                    # PG→`_get_conn()`；SQLite→`nullcontext(_conn)`）⇒ 两个后端都走适配器。
                    from trinity._tags import _conn_ctx as _cc
                    with _cc(self._adapter) as _conn:
                        _cur = _conn.cursor()
                        _cur.execute("SELECT content FROM memories WHERE category='perception' AND status='active' ORDER BY created_at DESC LIMIT 3")
                        for _row in _cur.fetchall():
                            _c = _plain(_row[0], 40)
                            if _c:
                                _pc.append(_c)
                except Exception as _e:
                    swallow(__name__, _e)
            for _p in _pc[:2]:
                parts.append(str(_p)[:40])
            # 2026-09 (EXECUTION 146/157): 工作记忆接入——优先从持久化上下文读
            # （跨进程/重启保留），fallback 进程内
            try:
                _wm_items = []
                if _pctx and _pctx.get("wm"):
                    _wm_items = _pctx.get("wm", [])[:3]
                else:
                    from trinity.brain.working_memory import get_working_memory
                    _wm = get_working_memory()
                    _sid = getattr(self, "_last_session_id", None) or "default"
                    _wm_items = _wm.get(_sid, top_k=3)
                for _wi in _wm_items[:3]:
                    parts.append(str(_wi.get("content", ""))[:40])
            except Exception as _e:
                swallow(__name__, _e)
            return " ".join(parts).strip() if parts else ""
        except Exception:
            return ""

    # 2026-09 (EXECUTION 129): prediction coding helpers
    def _predict_hits(self, query, top_k):
        """Predict hit count before retrieval (query features + EMA baseline)."""
        try:
            _qlen = len(str(query).strip())
            _base = int(top_k * 0.8) if _qlen <= 4 else int(top_k * 0.5)
            _ema = getattr(self, "_pred_ema", None)
            if _ema:
                _bucket = "short" if _qlen <= 4 else "long"
                _b = _ema.get(_bucket)
                if _b:
                    return int(_base * 0.6 + _b * 0.4)
            return _base
        except Exception:
            return max(1, int(top_k * 0.5))

    def _update_prediction_ema(self, query, actual):
        """Update prediction EMA by query length bucket (alpha=0.3)."""
        try:
            _qlen = len(str(query).strip())
            _bucket = "short" if _qlen <= 4 else "long"
            _ema = getattr(self, "_pred_ema", None)
            if _ema is None:
                _ema = {"short": None, "long": None}
                self._pred_ema = _ema
            _prev = _ema.get(_bucket)
            if _prev is None:
                _ema[_bucket] = float(actual)
            else:
                _ema[_bucket] = _prev * 0.7 + float(actual) * 0.3
        except Exception as _e:
            swallow(__name__, _e)

    def _score_retrieval_confidence(self, item: dict, semantic_score: float):
        """EXECUTION 127: 单条检索结果的四维置信度（元认知层）。

        category→SourceType 映射（权威性基础分）+ 新鲜度 + 语义相似度。
        失败返回 None（调用方静默）。
        """
        try:
            from trinity.modules.second_brain.confidence_scored_retrieval import (
                ConfidenceScorer, SourceType, ValidityCategory,
            )
            _cat = str(item.get("category") or "general").lower()
            _src_map = {
                "decision": SourceType.USER_CONFIRMED,
                "preference": SourceType.USER_CONFIRMED,
                "lesson": SourceType.VERIFIED_DATABASE,
                "incident": SourceType.VERIFIED_DATABASE,
                "security": SourceType.VERIFIED_DATABASE,
                "dcpm-schema": SourceType.LLM_GENERATED,
                "dcpm-core": SourceType.LLM_GENERATED,
                "semantic-generalization": SourceType.LLM_GENERATED,
                "perception": SourceType.OFFICIAL_DOCUMENT,
            }
            _st = _src_map.get(_cat, SourceType.UNVERIFIED)
            _created = None
            _ca = item.get("created_at")
            if _ca:
                try:
                    from datetime import datetime, timezone as _tz
                    _dt = _ca if not isinstance(_ca, str) else datetime.fromisoformat(_ca.replace("Z", "+00:00"))
                    if _dt.tzinfo is None:
                        _dt = _dt.replace(tzinfo=_tz.utc)
                    _created = _dt.timestamp()
                except Exception:
                    _created = None
            _vc = ValidityCategory.FACTS if _cat in ("decision", "incident") else ValidityCategory.GENERAL_KNOWLEDGE
            _score = ConfidenceScorer().score(
                source_type=_st,
                citation_count=int(item.get("access_count") or 0),
                created_at=_created,
                validity_category=_vc,
                semantic_similarity=max(0.0, min(1.0, float(semantic_score))),
            )
            return round(_score.overall, 4)  # overall 是属性
        except Exception:
            return None

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

            # 2026-09 (EXECUTION 132): 情感匹配加权（情感层）——查询含情感
            # 词时，同极性记忆 affect_match=1.0 排序前移（情绪色彩调制回忆）。
            try:
                from trinity.brain.affect import query_affect_terms as _qat
                _qterms = _qat(getattr(self, "_last_query", ""))
                if _qterms:
                    _meta = item.get("metadata") or {}
                    if isinstance(_meta, str):
                        import json as _j
                        try:
                            _meta = _j.loads(_meta)
                        except Exception:
                            _meta = {}
                    _aff = _meta.get("affect") or {}
                    _pol = str(_aff.get("polarity") or "neu")
                    _qpol = _qterms[0][1]
                    if _pol == _qpol:
                        item["affect_match"] = 1.0
            except Exception as _e:
                swallow(__name__, _e)

            # 2026-09 (EXECUTION 121): 价值编码加权（杏仁核通路）——            # 2026-09 (EXECUTION 121): 价值编码加权（杏仁核通路）——
            # importance_score（五因素价值模型，value-recalib 补标）以温和
            # 系数调制检索得分：高价值记忆更容易被想起。与突触衰减互补
            # （价值=编码强度，衰减=遗忘速度）。系数可配置（value_boost_k）。
            # 2026-09 (EXECUTION 180): 进化偏好行为化——谨慎模式调制排序
            # 2026-09-29（外部审计修复，根因 D：ruff F821 暴露的真实缺陷）：
            # 原来 `_value_k` 只在下面第 ~952 行才赋值，而上面的"谨慎模式"分支
            # 已经用 `max(_value_k, 0.45)` **读**它 ⇒ 每次进入该分支都抛
            # NameError，被外层 `except` 静默吞掉 ⇒ **`self:cautious_mode`
            # 进化偏好从未生效过一次**（本项目反复出现的"写好了但没有跑过"型缺陷）。
            # 修法：先把默认值取出来，再允许谨慎模式把它抬高。
            # 语义与原来注释的意图一致（谨慎 ⇒ 提高价值加权 ⇒ 更偏向重要记忆）。
            _value_k = float(getattr(self, "value_boost_k", 0.30))
            try:
                import os as _os2, json as _json2
                _evf = _os2.path.join(_os2.path.expanduser("~"), ".trinity", "evolution_state.json")
                _cautious = 0.0
                if _os2.path.exists(_evf):
                    _evd = _json2.load(open(_evf, encoding="utf-8"))
                    _cautious = float(_evd.get("active_preferences", {}).get("self:cautious_mode", 0) or 0)
                if _cautious > 0.5:
                    _value_k = max(_value_k, 0.45)  # 价值加权（选重要记忆）
            except Exception as _e:
                swallow(__name__, _e)
            _imp_score = float(item.get("importance_score") or item.get("importance") or 0.5)
            value_weight = 1.0 + _value_k * (_imp_score - 0.5)
            # 2026-09 (EXECUTION 150): 情绪驱动行为——会话情绪偏置接入排序
            try:
                _ebias = getattr(self, "_emo_bias", None)
                if _ebias is None:
                    from trinity.brain.affect_state import retrieval_bias as _rb
                    _sidb = getattr(self, "_last_session_id", None) or "default"
                    _pctxb = None
                    if self._adapter is not None and hasattr(self._adapter, "context_load"):
                        _pctxb = self._adapter.context_load(_sidb)
                    _ebias = _rb((_pctxb or {}).get("affect") if _pctxb else None)
                    self._emo_bias = _ebias
                if _ebias:
                    _boost = float(_ebias.get("value_boost") or 0.0)
                    if _boost > 0:
                        value_weight *= (1.0 + _boost)
                    _hint = _ebias.get("category_hint")
                    if _hint and str(item.get("category") or "").lower() == _hint:
                        value_weight *= 1.15
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09 (EXECUTION 201): 情绪一致性检索（mood-congruent）
            # affective-episodic 借鉴：情绪状态匹配记忆内容情绪 → 加权
            if os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(77)+chr(79)+chr(79)+chr(68)+chr(95)+chr(67)+chr(79)+chr(78)+chr(83)+chr(73)+chr(83)+chr(84)+chr(69)+chr(78)+chr(84), '0') == '1':
                try:
                    _cur_emo = getattr(self, "_emo_bias", None)
                    if _cur_emo and _cur_emo.get("category_hint") == "incident":
                        # 当前消极 → 检查该记忆内容情绪（快速启发：否定词/事故词）
                        _ct = str(item.get("content") or "")[:150]
                        _neg_hit = any(w in _ct for w in ("失败", "错误", "崩溃", "丢失", "故障", "事故", "损失"))
                        if _neg_hit:
                            value_weight *= 1.12  # 消极状态偏好消极经验（一致性）
                except Exception as _e:
                    swallow(__name__, _e)

            # 2026-09 (EXECUTION 127): 置信度评分（元认知层）——
            # 来源权威（category→SourceType 映射）+ 新鲜度 + 语义相似度
            # 四维置信度，注入 confidence_score 字段；失败静默。
            try:
                _conf_score = self._score_retrieval_confidence(item, semantic_score)
                if _conf_score is not None:
                    item["confidence_score"] = _conf_score
            except Exception as _e:
                swallow(__name__, _e)

            # ── 综合得分 ─────────────────────────────────────────
            final_score = (
                semantic_score * time_decay_score * agent_weight_score * modality_weight * value_weight
            )

            ranked.append({
                **item,
                "final_score": round(final_score, 6),
                "layer_scores": {
                    "semantic_score": round(semantic_score, 6),
                    "time_decay_score": round(time_decay_score, 6),
                    "agent_weight_score": round(agent_weight_score, 4),
                    "modality_weight": round(modality_weight, 4),
                    "value_weight": round(value_weight, 4),
                },
            })

        ranked.sort(key=lambda x: (0.0 if x.get("affect_match") else 1.0, x["final_score"]), reverse=True)
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
        # 1. SQLite 搜索（获取基准结果）—— t28/R-8：候选池不记账（出口层记最终返回集）
        sqlite_results = self._adapter.search_memories(
            query=query,
            persona_id=persona_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            top_k=top_k * 2,  # 多取一些用于融合
            touch=False, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
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
        else:
            fused = sqlite_results[:top_k]
        # t28/R-8：记账与**最终返回集**对齐（过取被截断的行不再被记）
        _acct.account_returned_hits(fused, self._adapter)
        return fused
    def _vector_search(self, query: str, top_k: int, account: bool = True) -> List[Dict[str, Any]]:
        """执行向量语义搜索。`account`（t28/R-6）：独立入口（vector 模式）=True；
        `search_hybrid` 的嵌入通道/PPR 种子传 False（由出口层统一记账）—— 详见报告 §15。
        """
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
                # 2026-09-29（①‑A）：查询嵌入**必须有墙钟上限**（见 _embed_query_bounded）。
                try:
                    _to = float(os.environ.get("TRINITY_QUERY_EMBED_TIMEOUT_S", "3") or 3)
                except Exception:  # noqa: BLE001
                    _to = 3.0
                # 2026-10-04（**实测根因修复**）：**预热不受请求侧上限约束**。
                # 病象（一行打点给出真因）：`VS-EARLY-RETURN L1119: 查询嵌入超时(3.0s)`，发起它的
                # 正是 `_deps._warm` 的预热带 ⇒ **闭环死锁**：预热要嵌完 ~1.9 万条才能建索引，
                # 却走请求侧 3s 上限 ⇒ `_vector_index` 永远建不起来 ⇒ 空转 300 轮、一行没嵌，
                # 却稳定烧一个核（实测 `60s CPU 增量 = 61.8s`）。判据见 `_vec_budget`。
                from ._vec_budget import effective_query_embed_timeout_s as _eqt
                _to = _eqt(_to)
                query_vec = _embed_query_bounded(self._embedding_engine, query, _to)
                if query_vec is None:
                    # 嵌入器被占满（预热在跑等）⇒ 本通道降级；**不写缓存**（下次再试）
                    logger = __import__("logging").getLogger("trinity.core.client")
                    logger.warning(
                        "查询嵌入超过 %.1fs 未返回 ⇒ 本次向量通道降级（请求由其它通道完成）。"
                        "常见原因：启动预热正在占满串行嵌入器；调整上限："
                        "TRINITY_QUERY_EMBED_TIMEOUT_S", _to)
                    return []
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
                        # 2026-09-09 修复：原 chr(34) 拼接把引号拼进值 →
                        # category != ALL(ARRAY['"perception"']) 恒为真，排除从未生效。
                        # 现按字面量排除 perception，并按 env 追加评测语料类。
                        exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
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
                    # t28/R-6：`touch=account`（极性！首版写成 not account，被功能探针抓到）
                    return self._adapter.search_memories(
                        query=query, top_k=top_k, touch=account, exclude_categories=list(_RETRIEVAL_EXCLUDE_CATEGORIES),
                    ) if self._adapter else []

                # 非 ANN 路径才在此拉全量
                # 2026-09-29（①‑A 第三次修正）：先定预算，再决定要不要**连查询嵌入都不算**。
                # 语义/依据/实测见 `_vec_budget.py` 的模块 docstring（为守行数预算迁出）。
                # ⚠️ 预热自身（`_override` 存在）**不得**走下面的早返回，否则索引永远建不起来
                #    —— 我第一版就这么错过，被 `test_跨调用收敛到全覆盖` 当场抓到。
                from ._vec_budget import corpus_embed_budget, current_corpus_override
                # 2026-09-30（外部审计修复）：**线程局部优先**，实例属性仅作兜底。原实现只读
                # `self._vec_corpus_budget_override`（共享实例属性）⇒ 预热设 200 时请求线程也读到
                # 200、在请求内嵌语料并与预热抢 `_SESSION_RUN_LOCK` ⇒ 排队超 20s 硬上限而 degraded
                # （栈取证 `_stack_evidence.txt`）。现预热用 `corpus_budget_scope()`（线程局部）⇒
                # 请求线程读到 None ⇒ 回到政策默认 `0`（下一行早返回、不嵌）。
                _override = current_corpus_override()
                if _override is None:
                    _override = getattr(self, "_vec_corpus_budget_override", None)
                _budget = corpus_embed_budget(_override)
                if _budget == 0 and not getattr(self, "_vec_index_seen", None):
                    from trinity.core.client import _corpus_persist as _cp_pre
                    _V = _cp_pre.preload_and_seed(self, dim)
                    if _V is not None:
                        self._vector_index = _V
                if _budget == 0 and not getattr(self, "_vec_index_seen", None):
                    return []
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
                except Exception as _e:
                    swallow(__name__, _e)

                # 编码批量记忆
                #
                # 2026-09-29（用户授权 ①，根因来自 py-spy）：`_ppr_fn`（`_hybrid_index.py:207`，
                # PPR 语义种子，**默认走这条路**）在**每次** full 路由查询里调到 `_vector_search`，
                # 而这里原先**无条件** `embed_batch(全量 2 万条)`；现场栈
                # `onnxruntime InferenceSession.run ← _embed_batch_raw(engine.py:389) ←
                # _vector_search ← _ppr_fn`。实测：重启后首个 full 查询 **20,223 ms**
                # （争用 163,182 ms）后被请求级硬上限 503，且每个孤儿任务占住执行器槽位数分钟
                # ⇒ 后续查询全被快速 503（三条不同查询 24–98 ms 返 503）。
                # 修法：**增量入索引** —— 只嵌"没见过"或"updated_at 变了"的行（首次仍付一次全量，
                # 之后毫秒级）。检索语义不变：与无条件重嵌**等价**（同 id 同向量，只是不重复算）。
                _seen: Dict[str, Any] = getattr(self, "_vec_index_seen", None) or {}
                self._vec_index_seen = _seen
                # 2026-10-04：从已加载的持久化索引播种 `_seen`（`_corpus_persist.py`）。
                # `_seen` 是进程内判据、重启即空 ⇒ 不播种则"能加载也照样全量重嵌"。
                if self._vector_index is not None:
                    _cp.seed_seen(_seen, self._vector_index)
                _new_all = [m for m in all_memories
                            if _seen.get(str(m.get("memory_id"))) != (m.get("updated_at") or "")]
                # 负数=不限、0=不嵌、N>0=最多 N 行（语义与依据见 `_vec_budget.py`）
                from ._vec_budget import select_rows
                _new = select_rows(_new_all, _budget)
                if _new:
                    texts = [m["content"] for m in _new]
                    vectors = self._embedding_engine.embed_batch(texts)

                    # 添加到索引
                    for memory, vec in zip(_new, vectors):
                        if vec is None:
                            continue
                        self._vector_index.add(memory["memory_id"], vec, {
                            "content": memory["content"],
                            "importance": memory.get("importance", 0.5),
                            "created_at": memory.get("created_at", ""),
                        })
                        _seen[str(memory["memory_id"])] = memory.get("updated_at") or ""
                # 覆盖度自述（供启动预热循环判断"还要不要再跑一轮"，也是可观测的降级依据）
                self._vec_index_complete = bool(
                    all_memories) and len(_seen) >= len(all_memories)

                # 2026-10-04：**脏计数触发落盘**（原 `_ann_dirty` 的既有惯例）。
                # 只在真正嵌了新行时累加；到阈值存一次（`TRINITY_CORPUS_INDEX_SAVE_EVERY`，
                # 默认 5000），另有 atexit 兜底。存失败不影响检索。
                _cp.maybe_save(len(_new or []))

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
            # 2026-09-29（外部审计修复）：原为 %s 格式化 —— 而 str(InvalidTag()) 是**空串**，
            # 于是这条最关键的降级路径只留下「向量搜索失败，回退到纯 SQLite 搜索: 」，
            # 后面什么都没有 —— 实测因此长期无人发现**整条嵌入通道恒返回空**。
            logger.warning("向量搜索失败，回退到纯 SQLite 搜索: %s: %s", type(e).__name__, e)

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
            # 2026-09-29（外部审计修复，根因 A）：原实现**只看 60 秒 TTL** 就无条件重建，
            # 而全量重建（19k 行 embed_batch + 建索引）实测约 **2.5 分钟** ⇒ 生产上
            # **缓存永远命中不了、每次查询都重建**。这正是 `use_ann` 一直被关着、
            # 24k 条嵌入用不上的真实原因。它还把 key `(dim, count, max_upd)` 存进
            # `_ann_cache[0]` **却从不比较**（死数据）。
            # 修法：把 key 用起来 —— **库没变就复用**，TTL 只作偶发刷新。
            _max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            _key = (dim, len(all_memories), _max_upd)
            _has_index = self._ann_cache is not None or getattr(ann, "ntotal", 0) > 0
            if self._ann_cache is not None and self._ann_cache[0] == _key:
                self._ann_cache = (_key, self._ann_cache[1], _time.time())
                mem_map = self._ann_cache[1]
            elif _has_index:
                # 2026-09-29（外部审计 Round 28，根因 A）：**库变了不等于要全量重嵌入**。
                # 仓库里早已有完整的**增量维护**（`_vector.py::_ann_incremental_add`：移除旧向量 →
                # 嵌入新内容 → 加入活索引 → 每 20 次脏写落盘），而这里却无视它、每次都拉全量重建
                # ——实测 300 行 22–30 秒 ⇒ 20k 行约 **25 分钟**，而库在被持续写入 ⇒
                # **每次查询都命中这条路** ⇒ `use_ann=True` 事实上不可用、24,270 条嵌入无人使用。
                # 正解：新增/更新已由增量钩子进索引，这里**只刷新 mem_map**（id→内容映射），
                # 并靠它**过滤掉已删除/归档**的 id（下一步 search 后执行）。
                mem_map = {m["memory_id"]: m for m in all_memories}
                self._ann_cache = (_key, mem_map, _time.time())
            else:
                # 真·没有可用索引时才全量构建（并落盘，跨进程免重建）
                self._ann_index = None
                ann = self._get_ann_index(dim)
                texts = [m["content"] for m in all_memories]
                vectors = self._embedding_engine.embed_batch(texts)
                mem_ids = [m["memory_id"] for m in all_memories]
                ann.add_vectors(mem_ids, vectors)
                mem_map = {m["memory_id"]: m for m in all_memories}
                self._ann_cache = (_key, mem_map, _time.time())
                try:
                    import os as _os
                    _os.makedirs(_os.path.dirname(self._ann_index_path) or ".", exist_ok=True)
                    ann.save(self._ann_index_path)
                except Exception as _se:                  # noqa: BLE001
                    __import__("logging").getLogger(__name__).warning(
                        "ANN index save failed: %s", _se)

        # 搜索
        results = ann.search(query_vec, k=top_k, ef=50)

        # 构建结果映射（mem_map 来自缓存/构建分支）
        vector_results = []
        for mem_id, score in results:
            if mem_id not in mem_map:
                # 2026-09-29：走增量维护后，索引里可能残留**已删除/归档**的 id
                # （增量钩子只在"内容更新"时移除旧向量）。用 mem_map 过滤掉，
                # 否则那些 id 会以空内容的形式进入融合。
                continue
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

    def _adapter_cache_fingerprint(self) -> str:
        """2026-09-04：稳定且廉价的库身份指纹（防语义缓存跨库串扰）。

        只读取属性（不查询、不连接）；缺省回退类名，保证 key 不崩溃。
        """
        try:
            ad = self._adapter
            if ad is None:
                return "none"
            for attr in ("db_path", "store_path", "database", "_db_path"):
                v = getattr(ad, attr, None)
                if v:
                    return str(v).replace("\\", "/").lower()[:160]
            host = getattr(ad, "host", None) or getattr(ad, "_host", None)
            dbn = getattr(ad, "dbname", None) or getattr(ad, "_dbname", None)
            if host and dbn:
                return "%s/%s" % (host, dbn)
            return type(ad).__name__
        except Exception:
            return "unknown"

