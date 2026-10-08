# -*- coding: utf-8 -*-
"""_HybridSearchMixin — 多通道融合检索（2026-09-09 结构梳理 P1-5 第 2 刀）。

从 trinity/core/client/_search.py 原样切出 search_hybrid，逻辑零改动；
沿用仓库既有 mixin 组合范式（core/client/_*Mixin），便于后续继续切分。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from ._search import _get_embedding_engine, _RETRIEVAL_EXCLUDE_CATEGORIES as _EXCL
from ._hybrid_stages import (
    _HybridStagesMixin, _lmc_channel_weights, _lmc_rrf_additions,
)
from ._hybrid_stages import (_scoped_topup_enabled, _merge_scoped_topup,  # noqa: F401 迁移 re-export
                             _hydrate_display_fields, _DISPLAY_FILL)  # §1306：出口水合（两条出口共用一处实现）
from ._access_account import account_returned_hits  # t18：出口记账（唯一实现，两条出口各调一次）
# 2026-09-30：按 `docs/STRUCTURE_BUDGETS.json` 的 `_policy`（超预算优先**拆分/迁移**）
# 把 `_mirror_hybrid_score` 拆到 `_hybrid_score.py`，此处仅 re-export 以保持既有引用可用。
from ._hybrid_score import _mirror_hybrid_score as _mirror_hybrid_score_impl  # noqa: E402
from ._hybrid_score import _forced_light_flag  # noqa: E402,F401 — 迁移 re-export（测试引用此名）
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

try:  # 687：元认知策略旁路（两出口共用一处实现；可选依赖，失败即 no-op）
    from trinity.retrieval.memory_policy_hook import attach as _memory_policy_attach
except Exception:  # noqa: BLE001
    def _memory_policy_attach(query, result):
        return result

def _vector_channel_impl(obj: Any) -> str:
    """派生「`vector` 键名背后是哪条实现」，供响应披露。

    2026-09-29（外部审计，根因 B）：此前两处 light 出口把 `_vec_status` 的缺省值**字面量**
    写成 `"unknown"`，而它只在 PG 门控块内绑定 ⇒ SQLite 上该字段恒为 unknown：一个专为
    披露实现而存在的字段等于没披露，同一响应里的 `vector` 计数遂被读成向量检索贡献。
    完整理由见 `_hybrid_index.vector_channel_impl` 的 docstring。失败**显式**标注。
    """
    try:
        from trinity.core.client._hybrid_index import vector_channel_impl
        return vector_channel_impl(getattr(obj, "_adapter", None),
                                   bool(getattr(obj, "use_ann", False)))
    except Exception:
        return "unknown:derive_failed"

# ── 2026-09-11（第三轮审计建议 P0-1）：可生效性一等指标 ──────────────────────
# 2026-09-14（R41-S 行数预算拆分）：本块**原样搬出**到 ./_hybrid_telemetry.py
# （自包含：一个进程级计数 + 3 个纯函数），以满足 docs/STRUCTURE_BUDGETS.json 的
# _hybrid_search.py ≤ 1400 行硬预算（实测 1415 行）。此处只留 import + 薄包装，
# **行为不变**：_SIGNAL_TELEMETRY 仍是同一个 dict 对象（import 即同一引用）。
from ._hybrid_telemetry import (  # noqa: E402
    _SIGNAL_TELEMETRY, _signal_tick, _synthetic_inline,
    signal_telemetry as _signal_telemetry_impl,
)

def signal_telemetry() -> Dict[str, Any]:
    """返回进程内检索信号计数（副本，供诊断/监控读取）。P0-1。

    薄包装：维持对**本模块** _LMC_TELEMETRY 的 globals() 语义——该 dict 在
    search_hybrid 内由 globals().setdefault("_LMC_TELEMETRY", ...) 定义。
    """
    return _signal_telemetry_impl(globals().get("_LMC_TELEMETRY") or {})

# （_synthetic_inline / _signal_tick 已于 2026-09-14 R41-S 迁至 ./_hybrid_telemetry.py；
#  _forced_light_flag 于 2026-09-30 迁至 ./_hybrid_score.py（行预算），此处 re-export。）

def _mirror_hybrid_score(results) -> None:
    return _mirror_hybrid_score_impl(results)

class _HybridSearchMixin(_HybridStagesMixin):
    """search_hybrid：向量 + BM25 + 图谱 + 工作记忆多通道融合。"""

    def search_hybrid(
        self,
        query: str,
        top_k: int = 10,
        strategy: str = "rrf",  # 2026-08-17 标定: rrf 远优于 fusion（0.950 vs 0.008）
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        routing: str = "auto",
        situation: Optional[str] = None,  # 2026-09 (EXECUTION 119): 情境文本（编码特异性原则）
        session_id: Optional[str] = None,  # 2026-09 (EXECUTION 148): 会话隔离贯穿（工作记忆+上下文）
        # 2026-09-30（外部审计）：暴露文档知识面。此前 hybrid 入口没有这个开关，
        # 而 `search_memories` 默认排除 `doc:*` ⇒ 生产路径看不见文档语料，
        # `scripts/doc_retrieval_eval.py` 因此测得 R@10=0.0（20 题全"未解析"）。
        # 底层早已支持，只差这一层贯穿；默认 False ⇒ 行为逐字不变。
        include_docs: bool = False, account: bool = True,  # account=False（t32）：内部/派生检索不记账
    ) -> Dict[str, Any]:
        if session_id is not None:
            self._last_session_id = session_id
        """混合检索（向量 + BM25 + 图谱融合）。

        ②自适应预算路由（2026-08-15，对齐 Query-Aware Budget-Tier Routing）：
          routing="auto"：按 query 特征分层——短查询走 light（FTS 快路径），
            长/复杂查询走 full（5 通道融合）。
          routing="light"/"full"：强制指定。
          环境变量 TRINITY_ADAPTIVE_ROUTING=off 可关闭；默认 on——
          2026-08-24（R8 P0-3）：短查询走 FTS 轻通道是引擎已验证的最优路径
          （FTS R@5=0.975 > hybrid-rrf 0.942），此前默认 off 使该性能特性空转。
        """
        # P0-1：本查询的墙钟起点（用于 retrieval_telemetry.last_query_ms）
        _TEL_T0 = time.time()
        # 2026-08-29 (PG): non-SQLite adapter (PG) 曾**无条件**强制 light
        # （理由："BM25 index not built for PG"）。2026-09-13 实测推翻了该依据：
        #   · 该分支连调用方显式传入的 routing="full" 也一并吞掉（文档却写"可强制指定"）
        #     → PG 主存储下融合路径**根本不可达**，语义结果缓存与 cross-encoder 重排
        #     因此都不在生产链路上（Redis 实测 0 keys）。
        #   · 黄金集 A/B（n=10，3 轮复测，引擎口径）：
        #       light (FTS+vector): R@1 = 0.6 / 0.6 / 0.6   p50 ≈ 1,232–1,299 ms
        #       full  (5 通道融合): R@1 = 0.9 / 0.9 / 0.8   p50 ≈ 190–402 ms
        #     → 融合路径**既更准又更快**，且 3 轮无失败。
        # 故 PG 下 auto 默认改为 full；显式 light/full 均按调用方意图。
        # 回滚：TRINITY_PG_ROUTING=light（恢复 2026-08-29 行为）。
        _pg_mode = self._adapter is not None and type(self._adapter).__name__.lower().find("postgres") >= 0
        _routing_requested = routing
        if _pg_mode:
            if routing == "auto":
                _pg_pref = str(os.environ.get("TRINITY_PG_ROUTING", "full")).strip().lower()
                routing = "light" if _pg_pref == "light" else "full"
            # 显式 "light"/"full" 一律尊重调用方（原实现会把显式 full 吞掉）
        env = os.environ.get("TRINITY_ADAPTIVE_ROUTING", "on").strip().lower()
        if routing == "auto":
            if env != "on":
                routing = "full"
            else:
                # 特征规则（仅非 PG 适配器会走到这里）：短查询（≤8 字符）走轻通道
                routing = "light" if len(query.strip()) <= 8 else "full"

        # 2026-09-14（742）**HyDE 查询扩展接入生产 hybrid 主路径**（TRINITY_QUERY_EXPANSION，默认 off→auto）：
        # 背景：claim `ssp_hyde_20260914.json` 已 PASS（SS-P R@1 0.30→0.50），但当时只在**评测探针**里生效；
        # 生产 router 里虽已接线却默认 off ⇒ "验证过却没生效"。此处接到 client 主路径（harness/MCP/API 共用），
        # 且**只改检索用的 query、不改 top-k**；失败静默（fail-open）。
        _qexp_meta = None
        try:
            from trinity.retrieval.query_expansion import expand as _qexp, enabled as _qexp_enabled
            if _qexp_enabled(query):
                _q2, _qexp_meta = _qexp(query)
                if _q2 and _q2 != query:
                    query = _q2
        except Exception as _e:  # noqa: BLE001
            swallow(__name__, _e)

        # ── light 路径：FTS 快通道（~3ms），天然支持过滤 ──────────
        self._last_query = query  # EXECUTION 132: affect 钩子用
        if routing == "light" and self._adapter is not None:
            # 2026-09 (EXECUTION 129): 预测编码（大脑预测-误差机制）——
            # 检索前预测命中数（基于查询长度/词数 + 历史 EMA），检索后
            # 计算误差并修正（低命中时补充检索一次）。误差反馈进 result。
            _pred = self._predict_hits(query, top_k)
            # 2026 优化轮 P0b：检索多样性补充接线（**默认 off**）。
            # 本模块此前零生产调用点（见 trinity/retrieval/diversity_supplement.py 文件头原始自述）。
            #
            # 语义取自仓库既有 A/B harness（scripts/eval_diversity_ab.py）：
            #   base = pool[:K]                       ← 基线：原序前 K
            #   var  = select_supplement(pool, top_k=K-max_add)["merged"][:K]
            # 即 **结果长度不变，把尾部 K-main_n 个换成来自更深候选的多样性补充**。
            # 所以这里：开启时才深取池（top_k + max_add），关闭时 top_k 原样、路径零变化。
            _ds_on = False
            _ds_main_n = top_k
            _ds_pool_k = top_k
            _ds_max_add = 3
            try:
                from trinity.retrieval.diversity_supplement import enabled as _ds_enabled
                _ds_on = bool(_ds_enabled())
            except Exception as _e:
                swallow(__name__, _e)
                _ds_on = False
            if _ds_on and top_k > _ds_max_add:
                _ds_pool_k = top_k + _ds_max_add   # 为补充预留候选，避免截断后池为空
                _ds_main_n = top_k - _ds_max_add
            results = self._adapter.search_memories(
                query=query, top_k=_ds_pool_k,
                agent_id=agent_id or None,
                persona_id=persona_id or None,
                tenant_id=tenant_id or None,
                include_docs=include_docs, touch=False,   # t18：通道层不记账（记账在检索出口）
            )
            # 补充选条（仅开启且确实深取过池时才做；失败即回退原序，绝不影响主流程）
            if _ds_on and _ds_pool_k > top_k:
                try:
                    from trinity.retrieval.diversity_supplement import (
                        select_supplement as _ds_select,
                        DEFAULT_MAX_CHARS as _ds_max_chars)
                    _ds_res = _ds_select(list(results or []), top_k=_ds_main_n,
                                         max_add=_ds_max_add, max_chars=_ds_max_chars)
                    if _ds_res.get("supplement"):
                        results = _ds_res["merged"][:top_k]   # 长度不变
                    _ds_stats = _ds_res.get("stats")
                except Exception as _e:
                    swallow(__name__, _e)
                    _ds_stats = None
            # 误差计算 + 修正：实际命中 < 预测 70% 且 > 0 → 补充检索
            _actual = len(results)
            _prediction_error = abs(_pred - _actual) / max(top_k, 1)
            _corrected = False
            if _actual > 0 and _actual < _pred * 0.7 and _pred > 0:
                try:
                    _extra = self._adapter.search_memories(
                        query=query, top_k=top_k * 2,
                        agent_id=agent_id or None,
                        persona_id=persona_id or None,
                        tenant_id=tenant_id or None,
                        include_docs=include_docs, touch=False,   # t18：通道层不记账（记账在检索出口）
                    )
                    if len(_extra) > _actual:
                        results = _extra
                        _corrected = True
                except Exception as _e:
                    swallow(__name__, _e)
            self._update_prediction_ema(query, _actual)
            # 2026-09（EXECUTION 104.9）：PG 主存储 light 路径补向量融合——
            # tsvector simple 对中文分词无效（FTS 召回中文语义查询为空，
            # 实测 "用户偏好 咖啡" FTS=0 / 向量=3），pgvector HNSW 直查 +
            # RRF 融合恢复语义召回；失败静默回退纯 FTS（行为与之前一致）。
            _pg = type(self._adapter).__name__.lower().find("postgres") >= 0
            if _pg and hasattr(self._adapter, "vector_search"):
                _vec_status = "skipped"
                try:
                    from trinity.core.client._helpers import _get_embedding_engine
                    _eng = _get_embedding_engine()
                    # 658.33 防御：helper 可能因并发/onnx 会话异常返回 None——
                    # 此时按显式后端重建引擎（与库内向量空间一致），并把状态
                    # 写入 breakdown 供观测（API 路径曾静默丢向量通道 → hybrid R@5 0.2）。
                    if _eng is None:
                        try:
                            from trinity.embeddings.engine import create_engine as _ce
                            _eng = _ce(backend=os.environ.get("TRINITY_EMBED_BACKEND") or "auto")
                            import logging as _lg2
                            _lg2.getLogger(__name__).warning(
                                "hybrid: helper engine None → rebuilt via create_engine(backend=%s)",
                                os.environ.get("TRINITY_EMBED_BACKEND") or "auto")
                        except Exception as _e2:
                            import logging as _lg3
                            _lg3.getLogger(__name__).warning("hybrid: engine rebuild failed: %s", str(_e2)[:120])
                            _eng = None
                    if _eng is not None:
                        # 2026-09 (EXECUTION 119): 情境依赖检索——情境文本与查询
                        # 融合嵌入（编码特异性原则：记忆编码时的情境是检索线索）。
                        # 情境向量召回 + 查询向量召回 RRF 融合；失败静默回退。
                        _q_for_vec = query
                        _sit_vec = None
                        # 2026-09 (EXECUTION 139): 连续状态——situation 为空时自动注入最近上下文
                        # （上次查询 + 最近感知事件），会话延续（大脑的"当下"）。
                        if not situation:
                            situation = self._build_auto_situation()
                        if situation:
                            try:
                                _qv_sit = _eng.embed(str(situation)[:200])
                                _sit_vec = self._adapter.vector_search(
                                    _qv_sit, top_k=max(top_k, 5),
                                    agent_id=agent_id or None,
                                    persona_id=persona_id or None,
                                    tenant_id=tenant_id or None,
                                    exclude_categories=list(_EXCL) + ["self-reflection"],  # G12: 唯一来源(engine)+语义通道追加
                                )
                            except Exception:
                                _sit_vec = None
                        try:
                            _qv = _eng.embed(_q_for_vec)
                        except Exception as _e4:
                            import logging as _lg4
                            _lg4.getLogger(__name__).warning("hybrid: query embed failed: %s", str(_e4)[:120])
                            _qv = None
                        if _qv is None:
                            _ok_vec = False
                            _qva = None
                        # 658.28 三修：查询向量健全性校验——API 路径实测杂交结果
                        # 与客户端不一致（0.2 vs 0.53）且 top 为无关中文记忆，疑
                        # 查询向量异常（零/非有限）导致 pgvector 返回任意近邻。
                        # 校验失败即放弃向量通道（宁缺勿错），并留下可观测日志。
                        try:
                            import numpy as _np
                            _qva = _np.asarray(_qv, dtype="float32").ravel()
                            _norm = float(_np.linalg.norm(_qva)) if _qva.size else 0.0
                            _ok_vec = bool(_qva.size and _np.isfinite(_qva).all() and _norm > 1e-6)
                        except Exception:
                            _qva, _ok_vec = None, False
                        if not _ok_vec:
                            import logging as _lg
                            _lg.getLogger(__name__).warning(
                                "hybrid vector channel skipped: unhealthy query vector (norm=%s)", locals().get("_norm"))
                        if not _ok_vec:
                            _vec = []
                        else:
                            try:
                                _vec = self._adapter.vector_search(
                                    _qva, top_k=max(top_k * 4, 24),
                                    agent_id=agent_id or None,
                                    persona_id=persona_id or None,
                                    tenant_id=tenant_id or None,
                                    exclude_categories=list(_EXCL) + ["self-reflection"],  # G12: 唯一来源(engine)+语义通道追加
                                )
                                _vec_status = "ok"
                            except Exception as _e5:
                                import logging as _lg5
                                _lg5.getLogger(__name__).warning("hybrid: vector_search failed: %s", str(_e5)[:120])
                                _vec = []
                                _vec_status = "error"
                        # 2026-09 (EXECUTION 154): 感知记忆降权——环境噪音类不主导语义检索
                        _r_perc = [x for x in results if str(x.get('category') or '') == 'perception']
                        if _r_perc and len(_r_perc) > len(results) // 2:
                            results = [x for x in results if str(x.get('category') or '') != 'perception']
                        # 2026-09 (EXECUTION 195): 自我类限流——自省/观察/评估/叙事
                        # 是"内部独白"非语义知识，最多保留 1 条（自我参照提示）
                        _SELF_CATS = {'self-reflection', 'self-observation', 'self-assessment', 'self-narrative'}
                        _selfs = [x for x in results if str(x.get('category') or '') in _SELF_CATS]
                        if len(_selfs) > 1:
                            _keep = set()
                            kept = 0
                            out = []
                            for x in results:
                                cat = str(x.get('category') or '')
                                if cat in _SELF_CATS:
                                    if kept == 0:
                                        out.append(x); kept += 1
                                else:
                                    out.append(x)
                            results = out
                        if _sit_vec:
                            _sit_ids = {x.get("memory_id") for x in _sit_vec}
                            for _r in _sit_vec:
                                _r["situation_score"] = 1.0
                        if _vec:
                            results = self._rrf_merge(results, _vec, top_k)
                        # 情境 boost：情境命中记忆加权 + 至多 3 条补入尾部。
                        # 2026-09-09（658.28 修复）：原实现 sort(key=(situation_flag,
                        # -score)) 把"当前上下文"记忆**整体前置**于查询相关性之上——
                        # 外部评测（coding-agent-life 15 查询）实测 gold 全部被挤出
                        # top-k（hybrid R@5=0 而 vector 单通道=0.6）；手工复现证明
                        # RRF 融合本身正确（gold 排 1-2），元凶是这行排序。
                        # 现改为：小加成（+0.05）参与分数排序、情境独有条目最多 3 条
                        # 且置于尾部——保留"会话延续"语义，不再压过事实召回。
                        # 2026-09-09（658.28 二修）：情境记忆**严格尾部放置**——
                        # 一轮实测（API 路径 R@5=0.2 vs 无情境客户端 0.53）证明：
                        # 即使 +0.05 小加成，情境向量行（相似度 ~0.6）仍会挤掉查询
                        # 相关行；情境的语义是"会话延续提示"，不该参与召回竞争。
                        # 现策略：查询结果按分排序取 top_k，情境独有条目仅在其后
                        # 追加（有余量才出现），对事实召回零损害。
                        if _sit_vec:
                            _seen2 = {x.get("memory_id") for x in results}
                            results.sort(key=lambda x: -float(x.get("score") or 0))
                            results = results[:top_k]
                            _sit_only = []
                            for _s in _sit_vec:
                                if _s.get("memory_id") not in _seen2 and len(_sit_only) < 3:
                                    _sit_only.append(_s)
                                    _seen2.add(_s.get("memory_id"))
                            room = max(0, top_k - len(results))
                            if room:
                                results.extend(_sit_only[:room])
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09 (EXECUTION 125): SAGE 图谱召回通道——图记忆实体/关系
            # 注入检索结果（graph_score 标记 + 实体名 boost）；失败静默。
            try:
                _sage = getattr(self, "sage", None)
                if _sage is not None:
                    _gq = self.sage_query(query) if hasattr(self, "sage_query") else _sage.query(query)
                    if _gq.get("sage") and _gq.get("entities"):
                        _gnames = [e.get("name", "") for e in _gq["entities"]]
                        for _r in results:
                            _rc = str(_r.get("content", ""))
                            if any(_n and _n.lower() in _rc.lower() for _n in _gnames):
                                _r["graph_score"] = 1.0
                        results.sort(key=lambda x: (0.0 if x.get("graph_score") else 1.0, -float(x.get("score") or 0)))
                        results = results[:top_k]
                        _sg = getattr(self, "_last_graph", None) or {}
                        _sg["entities"] = _gnames[:5]
                        self._last_graph = _sg
            except Exception as _e:
                swallow(__name__, _e)

            # 2026-09 (EXECUTION 127): serendipity 意外发现——低置信查询时
            # 启用温度采样（TRINITY_SERENDIPITY=1，默认关）；intent 意图感知
            # 重排（TRINITY_INTENT_ACTIVE=1，默认关）。两者默认关闭保持确定性。
            try:
                import os as _os
                if _os.environ.get("TRINITY_SERENDIPITY", "0") == "1" and results:
                    from trinity.modules.second_brain.serendipity_retrieval_engine import (
                        WanderRetriever, RetrievalHit,
                    )
                    _hits = []
                    for _r in results[:10]:
                        try:
                            _hits.append(RetrievalHit(
                                memory_id=str(_r.get("memory_id") or ""),
                                content=str(_r.get("content", ""))[:200],
                                relevance=float(_r.get("score") or 0.1),
                            ))
                        except Exception:
                            continue
                    if _hits:
                        _wander = WanderRetriever(temperature=1.5, sample_count=min(len(_hits), 3))
                        _sampled = _wander.wander(_hits)
                        _sampled_ids = {h.memory_id for h in _sampled}
                        # 采样的结果前移（惊喜项）
                        results.sort(key=lambda x: (0.0 if str(x.get("memory_id")) in _sampled_ids else 1.0,
                                                    -float(x.get("score") or 0)))
                if _os.environ.get("TRINITY_INTENT_ACTIVE", "0") == "1" and results:
                    from trinity.modules.second_brain.intent_compression import IntentAwareRetriever
                    _ia = IntentAwareRetriever()
                    # 意图感知重排（轻量：按 query 与内容词重叠微调）
                    _qwords = set(str(query).lower().split())
                    results.sort(key=lambda x: (-sum(1 for w in _qwords if w in str(x.get("content", "")).lower()),
                                                -float(x.get("score") or 0)))
            except Exception as _e:
                swallow(__name__, _e)

            # 2026-09 (P1-1): CrossEncoder 两阶段 rerank——RRF 融合后对 top
            # candidates 语义精排；模型不可用/加载失败自动降级 no-op（原行为）。
            # 2026-09-04（EXECUTION 556）实测：CPU CE 推理 ~1.5ms/token，本路径
            # 曾 2.5s/查询（首查 ~4s 的最大热点）。只重排已选中的 top_k（召回集不变），
            # 故入模文本截断 TRINITY_RERANK_CHAR_CAP（默认 120 字符，~0.7-1.2s）即可；
            # 240→160→120 各档均实测（240 档 ~2.3s / 160 ~1.9s / 120 ~0.6-1.9s 受 CPU 争用影响）。
            try:
                # 2026-09-02（CE 修复后默认开）：API 预加载后走真 CE（0.2s/批）；
                # 未预加载进程（worker）由 _preload_ok 守卫安全降级 ollama bge-m3，
                # 零崩溃。TRINITY_CROSSENCODER_RERANK=off 可关。
                if os.environ.get("TRINITY_CROSSENCODER_RERANK", "on").strip().lower() in ("1", "on", "true", "yes"):
                    from trinity.vector_index.reranker import CrossEncoderReranker
                    _rk = getattr(self, "_reranker", None)
                    if _rk is None:
                        _rk = CrossEncoderReranker(model_name="chinese")
                        self._reranker = _rk
                    # 2026-09-04（EXECUTION 556, Unsloth 借鉴 #3 热路径）: rerank 是 PG light
                    # 路径最大热点（实测 2.5s/查询，占首查 ~4s 的 60%+）。CE 推理成本随文本长度
                    # 近线性增长（512 token/对 → 数百毫秒至秒级）：
                    #   a) 入模文本按 TRINITY_RERANK_CHAR_CAP（默认 240 字符）截断——排序证据
                    #      通常位于内容头部，召回集合不变（rerank 只改变 top_k 内次序）；
                    #   b) 单候选跳过（重排无意义）；截断在副本上进行，分数映射回原结果。
                    if results and len(results) > 1:
                        _rk_cap = int(os.environ.get("TRINITY_RERANK_CHAR_CAP", "120") or 120)
                        _rk_cands = []
                        _rk_map = {}
                        for _i, _rc in enumerate(results):
                            if isinstance(_rc, dict) and _rc.get("content"):
                                _cc = dict(_rc)
                                _cc["content"] = str(_rc.get("content"))[:_rk_cap]
                                _key = str(_rc.get("memory_id") or _rc.get("id") or _i)
                                _rk_cands.append(_cc)
                                _rk_map[_key] = _rc
                            else:
                                _rk_cands.append(_rc)
                        _rk_results = _rk.rerank(
                            query=query,
                            candidates=_rk_cands,
                            top_k=top_k,
                            text_key="content",
                            id_key="memory_id",
                            score_key="rerank_score",
                        )
                        if _rk_results:
                            _out = []
                            for _rc in _rk_results:
                                _key = str(_rc.get("memory_id") or _rc.get("id") or "")
                                _orig = _rk_map.get(_key)
                                if _orig is not None:
                                    _orig["rerank_score"] = _rc.get("rerank_score")
                                    _out.append(_orig)
                                else:
                                    _out.append(_rc)
                            results = _out
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-10（658.57）：**多通道融合接入 light 路径**——此前 PG 生产路径只有
            # FTS+向量两通道（47 通道的全路径权重在 PG 上完全不参与，8 组消融证实）。
            # 这里把 4 个可由现有表直接查的通道以 RRF 融合进来，让"多通道"在生产路径真正生效：
            #   graph   —— memory_links 一跳关联扩展（联想召回，14,098 条边）
            #   proc    —— skill/procedural 类目（程序性知识）
            #   obs     —— observation 类目（跨上下文信念）
            #   prior   —— 高重要度先验（trigram 相似 + importance 排序）
            # TRINITY_LIGHT_MULTICHANNEL=off 可关闭；失败静默、不影响原有召回。
            try:
                if (_os.environ.get("TRINITY_LIGHT_MULTICHANNEL", "on").lower()
                        in ("1", "on", "true", "yes") and results is not None):
                    _lq = str(query or "").strip()
                    _ch_rows = {}
                    if _lq and len(_lq) >= 2 and _pg:
                        _seed_ids = [str(_r.get("memory_id")) for _r in (results or [])
                                     if _r.get("memory_id")][:10]
                        with self._adapter._get_conn() as _c4:
                            _cur4 = _c4.cursor()
                            if _seed_ids:
                                try:
                                    # 658.57c：**必须带命名空间过滤**——首版漏了 persona/agent
                                    # 条件，导致评测时把生产记忆注入基准上下文（judge 0.167→0.0）。
                                    _cur4.execute(
                                        "SELECT m.memory_id, m.content, m.category, m.importance, "
                                        "m.memory_layer, l.strength FROM memory_links l "
                                        "JOIN memories m ON m.memory_id = l.target_id "
                                        "WHERE l.source_id = ANY(%s) AND m.status='active' "
                                        "AND (%s IS NULL OR m.persona_id = %s) "
                                        "AND (%s IS NULL OR m.agent_id = %s) "
                                        "ORDER BY l.strength DESC NULLS LAST LIMIT 12",
                                        (_seed_ids, persona_id, persona_id, agent_id, agent_id))
                                    _ch_rows["graph"] = _cur4.fetchall()
                                except Exception:
                                    _cur4.connection.rollback() if hasattr(_cur4, "connection") else None
                            # 2026-09-11（第三轮审计 P0-1 遥测暴露并修复）：**proc/obs 两条通道
                            # 供料恒为 0**（实测 lmc.fetched = {graph:2, proc:0, obs:0, prior:6}）。
                            # 根因双重的：① 原查询用 websearch_to_tsquery('simple', 原始中文)——
                            # 'simple' 配置不切分中文，恒 0 命中（与 658.57b 记录同类）；
                            # ② 兜底 similarity(content, 查询) 走的是 **ciphertext 列** —— 实测
                            # skill/procedural 326 条里 277 条（85%）、observation 129 条里 124 条
                            # （96%）content 为 enc:v1: 密文，trigram 相似度恒≈0。
                            # 处置（复用生产 FTS 同源分词，不新增模块）：改用 jieba 分词后的
                            # to_tsquery 打 **content_tsv_zh**（该 tsvector 由明文生成、有 GIN 索引，
                            # 不受加密影响）。实测同一中文查询：proc 0 → **32** 命中、obs 0 → **4**。
                            # 开关 TRINITY_LMC_PROC_OBS_FTS（默认 on）；失败静默回退旧的 trigram 版。
                            try:
                                _po_on = _os.environ.get("TRINITY_LMC_PROC_OBS_FTS", "on").lower() in ("1", "on", "true", "yes")
                            except Exception:
                                _po_on = True
                            _po_tsq = ""
                            if _po_on:
                                try:
                                    import jieba as _jbp
                                    _jbp.setLogLevel(60)
                                    _po_terms = [w.strip() for w in _jbp.cut(_lq)
                                                 if w.strip() and len(w.strip()) >= 2][:12]
                                    _po_tsq = " | ".join(_po_terms)
                                except Exception:
                                    _po_tsq = ""
                            for _chan, _cats in (("proc", ("skill", "procedural")),
                                                 ("obs", ("observation",))):
                                try:
                                    if _po_tsq:
                                        _cur4.execute(
                                            "SELECT memory_id, content, category, importance, memory_layer "
                                            "FROM memories WHERE status='active' AND category = ANY(%s) "
                                            "AND (%s IS NULL OR persona_id = %s) "
                                            "AND (%s IS NULL OR agent_id = %s) "
                                            "AND content_tsv_zh @@ to_tsquery('simple', %s) "
                                            "ORDER BY importance DESC NULLS LAST LIMIT 6",
                                            (list(_cats), persona_id, persona_id, agent_id, agent_id, _po_tsq))
                                    else:
                                        # 658.57b 旧口径（无分词/降级）：trigram 相似度兜底
                                        _cur4.execute(
                                            "SELECT memory_id, content, category, importance, memory_layer "
                                            "FROM memories WHERE status='active' AND category = ANY(%s) "
                                            "AND (%s IS NULL OR persona_id = %s) "
                                            "AND (%s IS NULL OR agent_id = %s) "
                                            "AND (content_tsv_zh @@ websearch_to_tsquery('simple', %s) "
                                            "OR similarity(content, %s) > 0.08) "
                                            "ORDER BY importance DESC NULLS LAST LIMIT 6",
                                            (list(_cats), persona_id, persona_id, agent_id, agent_id, _lq, _lq))
                                    _ch_rows[_chan] = _cur4.fetchall()
                                except Exception as _e:
                                    swallow(__name__, _e)
                            try:
                                # 658.57b：原用 content % %s（% 被 psycopg2 当占位符 → 异常被吞，
                                # 该通道**从未供料**）。改用 word_similarity + 显式参数。
                                _cur4.execute(
                                    "SELECT memory_id, content, category, importance, memory_layer "
                                    "FROM memories WHERE status='active' AND importance >= 0.7 "
                                    "AND (%s IS NULL OR persona_id = %s) "
                                    "AND (%s IS NULL OR agent_id = %s) "
                                    "AND word_similarity(%s, content) > 0.15 "
                                    "ORDER BY importance DESC LIMIT 6",
                                    (persona_id, persona_id, agent_id, agent_id, _lq))
                                _ch_rows["prior"] = _cur4.fetchall()
                            except Exception as _e:
                                swallow(__name__, _e)
                    # 遥测：记录每个通道供料数与被采纳进最终结果数（供离线统计"通道是否真在供料"）
                    try:
                        _tel = globals().setdefault("_LMC_TELEMETRY", {"queries": 0, "fetched": {}, "adopted": {}})
                        _tel["queries"] = int(_tel.get("queries", 0)) + 1
                        for _cn, _rw in (_ch_rows or {}).items():
                            _tel["fetched"][_cn] = int(_tel["fetched"].get(_cn, 0)) + len(_rw)
                    except Exception as _e:
                        swallow(__name__, _e)
                    # 658.59（脑化开关 → 真实通道）+ 658.68（6 个权重 env 接进 light 路径）
                    # 的配权块**已整块迁出**到 ./_hybrid_stages.py（P0-2，2026-09-17）：
                    # structure_gate 实测本文件 1427/1400，按预算政策「拆分/迁出、不上调数字」。
                    # 逻辑零改动；解释性注释随函数搬走，见 _lmc_channel_weights 的 docstring。
                    _CW, _K = _lmc_channel_weights()
                    _have = {str(_r.get("memory_id")) for _r in (results or [])}
                    _add = _lmc_rrf_additions(_ch_rows, _have, _CW, _K)
                    if _add:
                        for _r in (results or []):
                            _k2 = str(_r.get("memory_id"))
                            if _k2 in _add:
                                _r["score"] = float(_r.get("score") or 0) + _add.pop(_k2)
                        _new_ids = list(_add.keys())[:8]
                        if _new_ids and _pg:
                            try:
                                with self._adapter._get_conn() as _c5:
                                    _cur5 = _c5.cursor()
                                    _cur5.execute(
                                        "SELECT memory_id, content, category, importance, memory_layer "
                                        "FROM memories WHERE memory_id = ANY(%s) AND status='active' "
                                        "AND (%s IS NULL OR persona_id = %s) "
                                        "AND (%s IS NULL OR agent_id = %s)",
                                        (_new_ids, persona_id, persona_id, agent_id, agent_id))
                                    _vals = [float(_r.get("score") or 0) for _r in (results or [])]
                                    _base = max(_vals or [0.0])
                                    # 2026-09-11（第三轮审计建议 P1-3 实测暴露的缺陷）：
                                    # 注入行原用 _base * 0.9 + contrib 定分，而 _base 取的是**最大**
                                    # 基分。当基分呈双峰（FTS ts_rank 0.6+ / 向量通道 0.05 附近）时，
                                    # 注入行落到**上峰**（实测 0.55–0.67），把下峰里的**真实命中**挤出
                                    # top-k（微观测：gold 由 rank4 → 掉出前 5）。消融实测：关闭本通道
                                    # R@3 0.100 → 0.667（Δ+0.567）。处置：注入行的基准改为**基分最小值**
                                    # ——辅助通道只补位、不越位（本块的设计意图），开关
                                    # TRINITY_LMC_SCORE_BOUND（默认 on）；off 回到旧的"最大值"口径。
                                    try:
                                        _bound_on = _os.environ.get("TRINITY_LMC_SCORE_BOUND", "on").lower() in ("1", "on", "true", "yes")
                                    except Exception:
                                        _bound_on = True
                                    _base_inject = (min(_vals) if (_bound_on and _vals) else _base)
                                    for _row in _cur5.fetchall():
                                        _mid = str(_row[0])
                                        results.append({
                                            "memory_id": _mid, "content": _row[1],
                                            "category": _row[2], "importance": _row[3],
                                            "memory_layer": _row[4],
                                            "score": _base_inject * 0.9 + _add.get(_mid, 0.0),
                                            "channels": ["light_multichannel"],
                                        })
                            except Exception as _e:
                                swallow(__name__, _e)
                        for _r in (results or []):
                            _r.setdefault("channels", ["fts", "vector"])
                        results.sort(key=lambda x: -float(x.get("score") or 0))
                        results = results[: max(top_k, 5)]
                        try:
                            _tel2 = globals().setdefault("_LMC_TELEMETRY", {"queries": 0, "fetched": {}, "adopted": {}})
                            _lmc_adopted = 0
                            for _r2 in results:
                                for _c2 in (_r2.get("channels") or []):
                                    if _c2 == "light_multichannel":
                                        _lmc_adopted += 1
                            _tel2["adopted"]["multichannel"] = int(
                                _tel2["adopted"].get("multichannel", 0)) + _lmc_adopted
                            # 2026-09-11（P0-1 收口）：把原本**无人读取**的 _LMC_TELEMETRY
                            # 接进 signal_telemetry()，使"辅助通道每轮供料/被采纳多少行"
                            # 也成为可经 diagnostics 读到的运行期指标（此前只有写方无读者）。
                            _signal_tick("light_multichannel_adopted", _lmc_adopted)
                        except Exception as _e:
                            swallow(__name__, _e)
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-10（658.53 脑化闭环③）：**神经调制参与最终排序**——四通道状态
            # （DA/NE/SHT/ACh）转成有界分数调整（±0.05）。**位置很关键**：必须在重排
            # （rerank）之后，否则排序会被重排覆盖（首版放在重排前 → 实测零效果）。
            # TRINITY_MODULATION=off 可关闭；失败静默不影响召回。
            try:
                if _os.environ.get("TRINITY_MODULATION", "on").lower() in ("1", "on", "true", "yes") and results:
                    from trinity.brain.neuromodulators import modulation_factors as _mf_fn
                    _mf = _mf_fn()
                    _w_imp = float(_mf.get("importance_weight", 1.0)) - 1.0
                    _w_nov = float(_mf.get("novelty_weight", 1.0)) - 1.0
                    # 658.56：在四通道调制之上，再接入**分层先验**与**优先图三维**
                    # （ZenBrain PriorityMap 的轻量版，避免在热路径做重计算）：
                    #   layer_prior  —— 七层差异化（core/procedural/cross-context 略升，
                    #                   working 略降：刚写入的短时记忆不该压过沉淀知识）
                    #   valence      —— 情绪效价强度（|valence|×arousal）略升，对应杏仁核
                    #   总调整仍**有界 ±0.05**，失败静默。
                    _lp_on = _os.environ.get("TRINITY_LAYER_PRIOR", "on").lower() in ("1", "on", "true", "yes")
                    _LP = {"core": 0.030, "procedural": 0.020, "cross-context": 0.015,
                           "semantic": 0.005, "episodic": 0.000, "working": -0.015}
                    # 658.56：检索结果行**不含 memory_layer**（实测为 None → 分层先验形同虚设）。
                    # 用一次批量查询补齐层信息（仅 top_k 行，开销毫秒级），让分层真正参与排序。
                    if _lp_on and results:
                        try:
                            _ids = [str(_r.get("memory_id")) for _r in results if _r.get("memory_id")]
                            if _ids and _pg:
                                _lmap = {}
                                _mmap = {}
                                with self._adapter._get_conn() as _c2:
                                    _cur2 = _c2.cursor()
                                    # 2026-09-11（第三轮审计 I4）：同一次批量查询**顺带取回
                                    # metadata**。实测（生产 light 路径 5/5 结果行）结果行
                                    # **不含 metadata** ⇒ 紧随其后的 affect 效价加成
                                    # （_md.get("affect")）恒为空——一条已接线的调整从未生效；
                                    # 而 brain/precision_tiers.py:78 每轮写入的
                                    # metadata.precision_tier / cognitive_value（PG 实测
                                    # 4,597 行，其中 active 4,373 = 23.5%）同样因为取不到
                                    # metadata 而无法参与排序。复用同一连接、同一次往返，
                                    # 零额外查询开销。
                                    _cur2.execute("SELECT memory_id, memory_layer, metadata FROM memories "
                                                  "WHERE memory_id = ANY(%s)", (_ids,))
                                    for _row2 in _cur2.fetchall():
                                        try:
                                            _m2, _l2, _md2 = _row2[0], _row2[1], _row2[2]
                                        except Exception:
                                            continue
                                        _lmap[str(_m2)] = _l2
                                        if isinstance(_md2, str):
                                            try:
                                                import json as _json_md
                                                _md2 = _json_md.loads(_md2 or "{}")
                                            except Exception:
                                                _md2 = {}
                                        _mmap[str(_m2)] = _md2 if isinstance(_md2, dict) else {}
                                for _r in results:
                                    if not _r.get("memory_layer"):
                                        _r["memory_layer"] = _lmap.get(str(_r.get("memory_id")))
                                    if not isinstance(_r.get("metadata"), dict):
                                        _r["metadata"] = _mmap.get(str(_r.get("memory_id"))) or None
                        except Exception as _e:
                            swallow(__name__, _e)
                    # 2026-09-11（审计 T1）：**元认知偏差反馈入检索**——复用调制钩子的
                    # 有界调整机制，不新增模块。动机：brain/metacog_monitor.py 每轮算出
                    # recency_bias / anchoring 等偏差并写 brain/metacog_bias.json，但**全仓
                    # 无任何读取点**（实测 grep 命中全在其自身）；而实测 recency_bias=0.5353
                    # （近 24h 写入占 active 的 53.5%）、anchoring=0.3463，两项均 flagged。
                    # 即：系统自己诊断出"检索面被近期写入淹没"，却不让这个诊断影响检索。
                    # 接线方式：偏差 flagged 时，对**近 24h 写入的结果**施加有界负向调整
                    # （幅度随偏差线性增长、上限 0.025，约为调制上限的一半，避免叠加过猛）。
                    # 开关 TRINITY_BIAS_FEEDBACK（默认 on）；失败静默，绝不影响召回。
                    _bf_damp = 0.0
                    _bf_info = None
                    try:
                        if _os.environ.get("TRINITY_BIAS_FEEDBACK", "on").lower() in ("1", "on", "true", "yes"):
                            import json as _json_b
                            _bp = _os.path.expanduser("~/.trinity/brain/metacog_bias.json")
                            _bm = _os.path.getmtime(_bp)
                            _bc = getattr(self, "_bias_cache", None)
                            if _bc and _bc[0] == _bm:
                                _bf_info = _bc[1]
                            else:
                                with open(_bp, encoding="utf-8") as _fb:
                                    _bj = _json_b.load(_fb)
                                _rb = (_bj.get("biases") or {}).get("recency_bias") or {}
                                _bf_info = {"flagged": bool(_rb.get("flagged")),
                                            "value": float(_rb.get("value") or 0.0)}
                                self._bias_cache = (_bm, _bf_info)
                            if _bf_info.get("flagged"):
                                # 0.25(阈值) → 0，0.75 → 上限 0.025，线性且有界
                                _bf_damp = -min(0.025, 0.025 * max(0.0, (_bf_info["value"] - 0.25) / 0.5))
                                import datetime as _dt_b
                                _bf_cut = (_dt_b.datetime.now(_dt_b.timezone.utc)
                                           - _dt_b.timedelta(hours=24)).isoformat()
                            else:
                                _bf_cut = None
                    except Exception:
                        _bf_damp = 0.0
                        _bf_info = None
                        _bf_cut = None
                    # 2026-09-11（审计 T5）：**agent 权重入排序**——与 T1 同构，复用同一
                    # 有界调整机制，不新增模块。动机：agent_weights 表、set/get API
                    # (postgresql.py:1757/1765)、诊断计数(:2660) 三者齐全，但**打分路径
                    # 一次都没读过它**（_hybrid_search.py 零提及）——又一处「API 齐全、排序不用」。
                    # 实测 0 行 ⇒ 未配权重时本块为 no-op，不改变任何现有行为。
                    _aw_map = {}
                    try:
                        if _os.environ.get("TRINITY_AGENT_WEIGHTS", "on").lower() in ("1", "on", "true", "yes"):
                            _awc = getattr(self, "_aw_cache", None)
                            if _awc is not None:
                                _aw_map = _awc
                            else:
                                _aw_map = dict(self._adapter.get_agent_weights() or {})
                                self._aw_cache = _aw_map
                    except Exception:
                        _aw_map = {}
                    # 2026-09-11（第三轮审计 I4）：**认知精度分档先验**——复用同一条有界
                    # 调整机制（与 layer_prior / agent_weight / bias_feedback 同构）。
                    # 动机：brain/precision_tiers.py:78 每轮把 {precision_tier, cognitive_value,
                    # precision} 写进 memories.metadata（PG 实测 4,597 行 / active 4,373 行 =
                    # 23.5%），但其读取点在**全仓为 0**（唯一提及是 _ingestion.py:646 的注释）
                    # ——"分档算了、写了、检索不用"。此处按档位给**有界 ±0.01**（低于调制
                    # ±0.05 与 agent 权重 ±0.02，避免喧宾夺主），使既有产物真正参与排序。
                    # 开关 TRINITY_PRECISION_PRIOR（默认 on）；失败静默，不影响召回。
                    _pp_on = _os.environ.get("TRINITY_PRECISION_PRIOR", "on").lower() in ("1", "on", "true", "yes")
                    _PP = {"hot": 0.010, "warm": 0.000, "cold": -0.010}
                    # P0-1 运行期计数（本查询各信号真正落到多少行）
                    _n_mod = _n_aw = _n_bf = _n_pp = _n_lp = 0
                    for _r in results:
                        _pp_applied = 0.0
                        _imp = float(_r.get("importance") or 0.5)
                        _d = 0.15 * (_imp - 0.5) * _w_imp + 0.05 * _w_nov
                        # agent 权重：以 0.5 为中性，有界映射到 ±0.02（低于调制上限，避免喧宾夺主）
                        _aw_applied = 0.0
                        if _aw_map:
                            try:
                                _w = _aw_map.get(str(_r.get("agent_id") or ""))
                                if _w is not None:
                                    _aw_applied = round(max(-0.02, min(0.02, 0.04 * (float(_w) - 0.5))), 4)
                                    _d += _aw_applied
                            except Exception:
                                _aw_applied = 0.0
                        _r["agent_weight_adj"] = _aw_applied
                        if _lp_on:
                            _lay = str(_r.get("memory_layer") or "")
                            _d += _LP.get(_lay, 0.0)
                            _md = _r.get("metadata")
                            if isinstance(_md, dict):
                                _aff = _md.get("affect")
                                if isinstance(_aff, dict):
                                    try:
                                        _va = abs(float(_aff.get("valence") or 0.0)) * max(
                                            0.3, abs(float(_aff.get("arousal") or 0.0)))
                                        _d += 0.02 * min(1.0, _va)
                                    except Exception as _e:
                                        swallow(__name__, _e)
                            # 审计 I4：精度分档先验（hot/warm/cold，有界 ±0.01）
                            if _pp_on:
                                try:
                                    _tier = str(_md.get("precision_tier") or "") if isinstance(_md, dict) else ""
                                    if _tier:
                                        _pp_applied = _PP.get(_tier, 0.0)
                                        _d += _pp_applied
                                except Exception:
                                    _pp_applied = 0.0
                        _r["precision_prior"] = _pp_applied
                        # 审计 T1：近期写入承担元认知偏差的代价（有界、可观测）
                        _bf_applied = 0.0
                        if _bf_damp and _bf_cut:
                            try:
                                _ca = str(_r.get("created_at") or "")
                                if _ca and _ca >= _bf_cut:
                                    _d += _bf_damp
                                    _bf_applied = round(_bf_damp, 4)
                            except Exception as _e:
                                swallow(__name__, _e)
                        _r["bias_feedback"] = _bf_applied
                        _r["modulation"] = round(max(-0.05, min(0.05, _d)), 4)
                        _r["score"] = float(_r.get("score") or 0) + _r["modulation"]
                        # ── P0-1：把"哪条信号真的落到了行上"计数 ──
                        try:
                            if _r.get("modulation"):
                                _n_mod += 1
                            if _r.get("agent_weight_adj"):
                                _n_aw += 1
                            if _r.get("bias_feedback"):
                                _n_bf += 1
                            if _r.get("precision_prior"):
                                _n_pp += 1
                            if _LP.get(str(_r.get("memory_layer") or ""), 0.0):
                                _n_lp += 1
                        except Exception as _e:
                            swallow(__name__, _e)
                    results.sort(key=lambda x: -float(x.get("score") or 0))
                    # P0-1：汇总到进程级计数（供 diagnostics 读取；失败静默）
                    _signal_tick("modulation", _n_mod)
                    _signal_tick("agent_weight", _n_aw)
                    _signal_tick("bias_feedback", _n_bf)
                    _signal_tick("precision_prior", _n_pp)
                    _signal_tick("layer_prior", _n_lp)
                    _SIGNAL_TELEMETRY["rows_scored"] = int(_SIGNAL_TELEMETRY.get("rows_scored") or 0) + len(results)
                    _SIGNAL_TELEMETRY["last_applied"] = {
                        "modulation": _n_mod, "agent_weight": _n_aw, "bias_feedback": _n_bf,
                        "precision_prior": _n_pp, "layer_prior": _n_lp, "rows": len(results),
                    }
                    # P0-1：查询级计数（次数 + 时间戳 + 本查询墙钟耗时）——"这条生产路径
                    # 到底跑过没有、跑得多久"
                    try:
                        _SIGNAL_TELEMETRY["queries"] = int(_SIGNAL_TELEMETRY.get("queries") or 0) + 1
                        _SIGNAL_TELEMETRY["last_query_ts"] = time.time()
                        if _TEL_T0:
                            _SIGNAL_TELEMETRY["last_query_ms"] = round((time.time() - _TEL_T0) * 1000, 1)
                    except Exception as _e:
                        swallow(__name__, _e)
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-04（EXECUTION 558, 问题 B2）: 自我叙事/观察类不得侵占 top1——
            # 仅当查询本身不含自我指向词时生效（"我/自己/状态/回顾/我是谁"等查询仍可命中）。
            try:
                _q_self = any(w in str(query).lower() for w in ("我", "自己", "自省", "状态", "回顾", "我是谁", "观察到自己", "做了什么"))
                if results and len(results) > 1 and not _q_self:
                    _SELF_C = {"self-reflection", "self-observation", "self-assessment", "self-narrative"}
                    _top_cat = str((results[0].get("category") or "")).lower()
                    if _top_cat in _SELF_C:
                        for _ri, _rc in enumerate(results):
                            if str((_rc.get("category") or "")).lower() not in _SELF_C:
                                results.insert(0, results.pop(_ri))
                                break
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09 (EXECUTION 116): DCPM 双过程钩子——System1 快路径信念命中
            # + 元认知置信评估（大脑化：检索即信念验证）
            try:
                from trinity.brain.metacognition import assess_confidence
                _conf = assess_confidence(results, channels=["fts", "vector"] if _pg else ["fts"])
                # 2026-09 (EXECUTION 140): 权重级记忆——Hebbian 检索强化（高置信命中时 top1 微调）
                try:
                    if _conf.get("level") in ("high", "medium") and results and _pg:
                        _h_top = results[0]
                        _h_acc = int(_h_top.get("access_count") or 0)
                        if _h_top.get("memory_id") and _h_acc >= 5:
                            from trinity.brain.hebbian import consolidate as _hb
                            _hb(self._adapter, _h_top.get("memory_id"), _eng.embed(str(query)[:200]))
                except Exception as _e:
                    swallow(__name__, _e)
                _mirror_hybrid_score(results)  # 2026-09-30：light 路径补齐 hybrid_score
                result = {
                    "results": results,
                    "strategy": "light",
                    "query": query,
                    "breakdown": {"routing": "light",
                                  "channels": ["fts", "vector"] if _pg else ["fts"],
                                  "vector_channel": locals().get("_vec_status") or _vector_channel_impl(self)},
                    "metacognition": _conf,
                    "prediction": {"expected": _pred, "actual": _actual,
                                  "error": round(_prediction_error, 3),
                                  "corrected": _corrected},
                }
                # 2026-09 (EXECUTION 165): 认知编排层观测——各认知阶段状态
                try:
                    from trinity.brain.cognition_pipeline import run_pipeline as _cp
                    _stages = {
                        "context": bool(situation or getattr(self, "_last_query", None)),
                        "affect": bool(getattr(self, "_emo_bias", None)),
                        "graph": bool(getattr(self, "_last_graph", None)),
                        "confidence": True,
                        "prediction": True,
                        "hebbian": bool(results and int(results[0].get("access_count") or 0) >= 5),
                    }
                    result["cognition"] = _cp(self, query, results, _stages)
                except Exception as _e:
                    swallow(__name__, _e)
                # 2026-09 (EXECUTION 171): token_budget 激活——检索成本报告
                # + TRINITY_TOKEN_BUDGET 硬截断（对标 Mem0 token budgeting）
                try:
                    from trinity.modules.second_brain.token_budget import (
                        TokenBudgetManager, estimate_tokens,
                    )
                    _budget = int(os.environ.get("TRINITY_TOKEN_BUDGET", "0") or 0)
                    _tb = TokenBudgetManager(limit=_budget) if _budget else None
                    _budget_report = {
                        "estimated_tokens": sum(estimate_tokens(str(r.get("content", ""))[:200]) for r in results),
                        "results": len(results),
                    }
                    if _tb:
                        _tb.register([
                            (str(r.get("memory_id")), str(r.get("content", ""))[:200],
                             float(r.get("importance") or 0.5)) for r in results
                        ])
                        _ctx = _tb.build_context([str(r.get("memory_id")) for r in results])
                        _budget_report["budget_limit"] = _budget
                        _budget_report["included"] = len(_ctx.get("memories_included", []))
                        _budget_report["dropped"] = len(_ctx.get("memories_dropped", []))
                        if _ctx.get("memories_dropped"):
                            _drop = set(_ctx.get("memories_dropped", []))
                            results = [r for r in results if str(r.get("memory_id")) not in _drop]
                    result["budget"] = _budget_report
                except Exception as _e:
                    swallow(__name__, _e)
                # 2026-09 (EXECUTION 141): persistent session context
                try:
                    if (self._adapter is not None and hasattr(self._adapter, 'context_save')
                            and hasattr(self._adapter, 'context_load')):
                        _pp = getattr(self, '_recent_percepts', None)
                        # EXECUTION 149: 情绪状态机——查询情绪 EMA 累积进会话状态
                        _aff = None
                        try:
                            from trinity.brain.affect import assess as _affassess
                            from trinity.brain.affect_state import update_state as _upd
                            _ar = _affassess(str(query))
                            _prev = None
                            try:
                                _prev = self._adapter.context_load(getattr(self, "_last_session_id", None) or "default").get("affect")
                            except Exception:
                                _prev = None
                            _aff = _upd(_prev, _ar)
                        except Exception as _e:
                            swallow(__name__, _e)
                        # EXECUTION 157: 工作记忆持久化——随上下文保存
                        _wm_items = []
                        try:
                            from trinity.brain.working_memory import get_working_memory
                            _wms = get_working_memory().get(getattr(self, "_last_session_id", None) or "default", top_k=7)
                            _wm_items = [{"content": str(i.get("content", ""))[:200],
                                          "importance": float(i.get("importance") or 0.5)}
                                         for i in _wms[:7]]
                        except Exception as _e:
                            swallow(__name__, _e)
                        self._adapter.context_save(str(query)[:100], _pp or [], affect=_aff,
                                                     session_id=getattr(self, "_last_session_id", None) or "default",
                                                     wm=_wm_items)
                except Exception as _e:
                    swallow(__name__, _e)
                # System1：高信心时持久化信念命中（PG，跨进程可见；不阻塞，失败静默）
                if _conf.get("level") in ("high", "medium") and results:
                    _top = results[0].get("content", "")[:200]
                    try:
                        if self._adapter is not None and hasattr(self._adapter, "dcpm_store_belief"):
                            self._adapter.dcpm_store_belief(
                                belief_id=__import__("uuid").uuid4().hex[:12],
                                subject=query[:60], predicate="retrieved", obj=_top,
                            )
                    except Exception as _e:
                        swallow(__name__, _e)
            except Exception:
                _mirror_hybrid_score(results)  # 2026-09-30：light 路径补齐 hybrid_score
                result = {
                    "results": results,
                    "strategy": "light",
                    "query": query,
                    "breakdown": {"routing": "light",
                                  "channels": ["fts", "vector"] if _pg else ["fts"],
                                  "vector_channel": locals().get("_vec_status") or _vector_channel_impl(self)},
                }
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=None, action="search_hybrid",
                        agent_id=agent_id, persona_id=persona_id,
                        # R4（§785.6 遗留②）：**记下读到了哪些**（此前只有 hits 计数）。
                        # 没有它，"谁读了哪一条"在**主导通路**上根本算不出来
                        # （24h 实测 search_hybrid 1654 行 vs search 1226 行）；
                        # 与既有 `_search.py` 同形（同样截断到 10 条 ⇒ 读数只能声称**下界**）。
                        details={"query": query, "top_k": top_k, "strategy": "light",
                                 "hits": len(results),
                                 "memory_ids": [r.get("memory_id") for r in results
                                                if isinstance(r, dict) and r.get("memory_id")][:10]},
                    )
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09 (EXECUTION 193): 联想补充检索（激活扩散）——TRINITY_ASSOCIATIVE=1 启用
            if os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(65)+chr(83)+chr(83)+chr(79)+chr(67)+chr(73)+chr(65)+chr(84)+chr(73)+chr(86)+chr(69), '0') == '1':
                try:
                    from trinity.brain.associative_memory import associative_jump
                    if results:
                        _src = str(results[0].get('memory_id') or '')
                        _aj = associative_jump(_src, top_k=2) if _src else {}
                        _assoc = _aj.get('associations', []) if _aj.get('jumped') else []
                        if _assoc:
                            result['associations'] = _assoc
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09 (EXECUTION 198): 心智工作空间——思考痕迹（Mental Workspace 借鉴）
            # TRINITY_THINKING=1 时记录检索决策轨迹（情境/调制/联想）——可追溯
            if os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(84)+chr(72)+chr(73)+chr(78)+chr(75)+chr(73)+chr(78)+chr(71), '0') == '1':
                try:
                    _trace = {
                        "query": str(query)[:80],
                        "situation": str(situation or "")[:120],
                        "self": getattr(self, "_last_session_id", None) or "default",
                        "stages": {k: v.get("status") for k, v in result.get("cognition", {}).get("stages", {}).items()},
                        "modulation": {
                            "cautious": bool(getattr(self, "_emo_bias", None)) or "emotion",
                        },
                        "top1": (results[0].get("content") or "")[:60] if results else "",
                    }
                    result["thinking_trace"] = _trace
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09 (EXECUTION 203): 反思驱动检索（Hindsight 借鉴）
            # TRINITY_REFLECTIVE=1：检索后反思质量 → 改进信号
            if os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(82)+chr(69)+chr(70)+chr(76)+chr(69)+chr(67)+chr(84)+chr(73)+chr(86)+chr(69), '0') == '1':
                try:
                    _n = len(results)
                    _conf = float(results[0].get("confidence") or 0) if results else 0
                    _reflection = {
                        "retrieved": _n,
                        "top_confidence": _conf,
                        "quality": "good" if (_n >= 3 and _conf >= 0.5) else "low",
                        "improvement": None,
                    }
                    if _n < 3:
                        _reflection["improvement"] = "expand_topk"
                    elif _conf < 0.5:
                        _reflection["improvement"] = "rerank"
                    else:
                        _reflection["improvement"] = "none"
                    result["reflection"] = _reflection
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09 (EXECUTION 204): 记忆重构（reconstructive）
            # TRINITY_RECONSTRUCTIVE=1：检索结果→连贯回忆摘要（LLM 优先）
            if os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(82)+chr(69)+chr(67)+chr(79)+chr(78)+chr(83)+chr(84)+chr(82)+chr(85)+chr(67)+chr(84)+chr(73)+chr(86)+chr(69), '0') == '1':
                try:
                    from trinity.brain.reconstructive_memory import reconstruct as _rec
                    _rr = _rec(str(query)[:40], results)
                    if _rr.get("reconstructed"):
                        result["recall"] = _rr.get("recall")
                except Exception as _e:
                    swallow(__name__, _e)
            # 2026-09-02（brain fix）：检索出口统一解密（enc:v1 → 明文；fail-open）
            from trinity.security.crypto import decrypt_content
            for _r in (result.get("results") or []):
                if isinstance(_r, dict) and _r.get("content"):
                    _r["content"] = decrypt_content(_r["content"])
            # 2026-09-13（P0 证据门控）：**快路径出口同样要挂门控**。
            # 本函数有**两个** return（此处为 routing=fast 早返回，另一处在 full 融合路径末尾）；
            # 只挂后者会让短查询/路由到 fast 的查询绕过门控（实测 /memory/search/hybrid
            # 的返回体里看不到 evidence_gate 正是这个原因）。
            # 2026-09-13（P2 LLM 列表式重排）：默认 off；先重排、再判相关度。
            # _llm_reranked 幂等标记：search() 内部若再走一遍同路径不会重复调用模型。
            try:
                from trinity.retrieval.llm_rerank import enabled as _lr_on, llm_rerank as _lr
                if _lr_on() and not result.get("_llm_reranked"):
                    result["results"] = _lr(query, result.get("results"))
                    result["_llm_reranked"] = True
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（H2-3）：检索侧「更新值优先」——默认 off；先重排版本对，再判相关度。
            # 实测动机：同槽位旧值在 top-1 的占率 100%（见 trinity/retrieval/recency_pref.py 文件头）。
            try:
                from trinity.retrieval.recency_pref import enabled as _rw_on, prefer_newer as _rw
                if _rw_on():
                    result["results"] = _rw(result.get("results"))
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（H2-3）：时序上下文字期标注 + 时间序（默认 off）
            try:
                from trinity.retrieval.temporal_context import enabled as _tc_on, annotate_and_order as _tc
                if _tc_on(query):
                    # reorder=False：**只标注、不重排**（重排会让 top-k 变成「最旧 k 条」）
                    result["results"] = _tc(result.get("results"), reorder=False)
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（W2/P0-b）：偏好卡。**默认旁路**（result["preference_card"]），
            # 不再插行挤占 top-k（实测：插行会被 R@k 结构性扣分）；
            # TRINITY_SYNTHETIC_INLINE=on 时退回旧的“插行”行为（仅实验用）。
            try:
                from trinity.retrieval.preference_card import (
                    enabled as _pc_on, attach as _pc, build_card as _pc_build)
                if _pc_on(query):
                    if _synthetic_inline():
                        result["results"] = _pc(result.get("results"), query)
                    else:
                        _card = _pc_build(result.get("results") or [])
                        if _card:
                            result["preference_card"] = _card["text"]
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（W3/P0-c）：会话级注入；**默认旁路**（result["session_tiers"]）
            try:
                from trinity.retrieval.session_tier import (
                    enabled as _st_on, summarize_sessions as _st_sum, inject as _st_inj,
                    expand as _st_exp)
                if _st_on(query):
                    _sums = _st_sum(result.get("results") or [], self, max_sessions=3)
                    if _synthetic_inline():
                        result["results"] = _st_inj(result.get("results"), query, summaries=_sums)
                    result["session_tiers"] = _sums
                    # W3 第二版：真·会话级扩展（换召回集合：下钻会话内、剔除已召回行）
                    _exp = _st_exp(result.get("results") or [], query, self._adapter,
                                   max_sessions=3, per_session=2)
                    if _exp:
                        result["session_expansion"] = _exp
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（W1 第3层）：事件时间线；**默认旁路**（result["event_timeline"]）
            try:
                from trinity.retrieval.event_layer import (
                    enabled as _ev_on, attach as _ev, build_timeline as _ev_build)
                if _ev_on(query):
                    if _synthetic_inline():
                        result["results"] = _ev(result.get("results"), query)
                    else:
                        _tl = _ev_build(result.get("results") or [])
                        if _tl:
                            result["event_timeline"] = _tl
            except Exception as _e:
                swallow(__name__, _e)
            # 2026 优化轮 A3：**版本链可见性**（默认 on；纯增量标注，不改顺序、不删行）。
            # 存储层早就有 superseded_by / supersedes / valid_from / version_chain，
            # 但检索出口此前对它们的引用数是 0 —— 数据在库里，不呈现给调用方，
            # 于是"同一件事先后有过不同结论"时会把过时的那条当成最新返回。
            try:
                from trinity.retrieval.supersede_chain import annotate as _sc_annotate
                _sc = _sc_annotate(result.get("results") or [])
                result["results"] = _sc["rows"]
                if _sc["stats"].get("superseded_n"):
                    result["supersede_chain"] = _sc["stats"]
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（W3 第三版 / H2-5 聚合路径）：覆盖扩展（默认 off / auto=聚合型查询）
            try:
                from trinity.retrieval.coverage_expand import (
                    enabled as _cv_on, expand as _cv_exp)
                if _cv_on(query):
                    _cvexp = _cv_exp(result.get("results") or [], query, self, k=40, max_items=20)
                    if _cvexp:
                        result["coverage_expansion"] = _cvexp
            except Exception as _e:
                swallow(__name__, _e)
            try:
                from trinity.retrieval.evidence_gate import apply_evidence_gate
                # 2026-09-23（§1310）：**快路径也要把显式作用域传进门**。
                # 另两条出口（`_search.py` 的 `search()`、`_hybrid_stages.py` 的出口阶段）早就传了
                # `scope_agent/scope_persona`，只有这里漏了 ⇒ `filter_eval_corpus` 的**豁免**
                # （「调用方显式点名命名空间就按它的意愿放行」，2026-09-13 / P0-2 两次修过）
                # 在这条路径上**永远不生效**：实测矩阵（隔离库、只差这一个实参）
                # light×无作用域 ⇒ 0 行 / light×显式作用域 ⇒ **仍然 0 行**（gate 收到的 scope 是 None）。
                result = apply_evidence_gate(query, result, source="search_hybrid_fast",
                                             scope_agent=agent_id, scope_persona=persona_id)
            except Exception as _e:
                swallow(__name__, _e)
            result = _memory_policy_attach(query, result)  # 687 元认知策略旁路（快路径同样要挂）
            # 2026-09-23（§1306）：**快路径也要水合** —— 否则这条路出来的行没有 agent_id，
            # GraphQL 的 Memory.agentId（非空）会让整条查询回 null。一处实现、两处调用。
            try:
                _hydrate_display_fields(result.get("results") or [], self._adapter)
            except Exception as _e:  # noqa: BLE001 — 水合失败不改原结果
                swallow(__name__, _e)
            if _qexp_meta:  # 742 HyDE 旁路字段（不参与排序，仅标注）
                result["query_expansion"] = _qexp_meta
            account_returned_hits(result, adapter=self._adapter, enabled=account)   # t18/t32 出口记账
            return result

        hr = self.hybrid_retriever
        # 2026-09-02（brain fix）：先等 BM25 预构建完成再跑 full 路径检索，
        # 消除构建线程与首搜惰性导入的并发崩溃竞态。
        self._wait_bm25_ready()

        # 如有 agent/persona/tenant 过滤，在向量侧 wrap search_fn；
        # 同时把隔离维度折入 retriever 语义缓存 key，防止跨租户缓存串扰。
        # 2026-09-04（Unsloth 借鉴 #3 热路径/缓存正确性）：加库指纹——同进程
        # 多库 A/B（fidelity_ab / 评测 runner）时，缓存行（融合后仅有 memory_id）
        # 曾跨库命中导致第二库 hybrid 全空（fidelity-ab mid/low tier hybrid R@5=0）。
        cache_scope = f"a={agent_id or ''}:p={persona_id or ''}:t={tenant_id or ''}:db={self._adapter_cache_fingerprint()}"
        _scoped = bool(agent_id or persona_id or tenant_id)
        _over = top_k   # 2026-09-16：超取机制已移除（见 _hybrid_stages._finalize_scoped_unused_removed）
        if agent_id or persona_id or tenant_id:
            _orig_fn = hr._search_fn

            def _filtered_fn(q: str, tk: int):
                return self._adapter.search_memories(
                    query=q, top_k=tk,
                    agent_id=agent_id or None,
                    persona_id=persona_id or None,
                    tenant_id=tenant_id or None,
                    include_docs=include_docs, touch=False,   # t18：通道层不记账（记账在检索出口）
                ) if self._adapter else []

            hr._search_fn = _filtered_fn
            try:
                result = hr.search(query=query, top_k=_over, strategy=strategy, cache_scope=cache_scope)
            finally:
                hr._search_fn = _orig_fn
        else:
            result = hr.search(query=query, top_k=_over, strategy=strategy, cache_scope=cache_scope)

        # 修复(2026-08-14): 融合后统一后过滤——归属隔离 / 非 active 剔除 / 评测语料隔离。
        # 2026-09-24（§1314）：这三条**搬进** `_HybridStagesMixin._filter_result_ownership`
        # （本文件 1391/1400 行，结构门只给搬不给抬预算；搬过去后行为逐字节相同，
        # 只有评测语料那条多了一个**默认 off** 的开关，见那边的 docstring）。
        result = self._filter_result_ownership(result, agent_id, persona_id, tenant_id)

        # ── 2026-09-16（§779.5 遗留①②）：作用域**欠量补齐**（默认 on）──────────
        # 实测：同一引擎，REST `search_hybrid` 作用域下 5.15/10、4/20 空；
        # 而 `search(mode="hybrid")` 6.45/10、0 空 —— 差别就在 `_search.py:221-230`
        # 那条"融合空则 FTS 兜底（persona 前置过滤）"，而本方法没有。
        # ⇒ 这里补上**域内补齐**：过滤后不足 top_k 时，向**本域**要（SQL 前置过滤），
        #    只填剩余空位、不挤掉融合命中（`_merge_scoped_topup` 保序语义）。
        # 回滚：TRINITY_SCOPED_TOPUP=off（逐字节回到旧行为）。
        if _scoped and _scoped_topup_enabled() and self._adapter is not None:
            _got = len(result.get("results") or [])
            if _got < top_k:
                try:
                    _extra = self._adapter.search_memories(
                        query=query, top_k=top_k,
                        persona_id=persona_id or None,
                        agent_id=agent_id or None,
                        tenant_id=tenant_id or None,
                        include_docs=include_docs, touch=False,   # t18：通道层不记账（记账在检索出口）
                    ) or []
                    _merged, _added = _merge_scoped_topup(result.get("results") or [],
                                                          _extra, top_k)
                    if _added:
                        result["results"] = _merged
                    result["scoped_topup"] = {
                        "before": _got, "added": _added,
                        "after": len(result.get("results") or []),
                        "requested_top_k": top_k,
                        "saturated": len(result.get("results") or []) >= top_k,
                    }
                except Exception as _e:  # noqa: BLE001 — 补齐失败绝不改变原结果
                    swallow(__name__, _e)

        # 2026-09-04 (Unsloth 借鉴#4 通道质量): full 融合路径的行只含 memory_id + 各通道分数，
        # 从不回填 content——所有 >8 字符查询（routing=full）的结果对下游
        # answer_eval/rerank/情境注入均不可用（实测 5 条库内 hybrid-rrf 漏检）。
        # 修复：按 memory_id 批量回填展示字段（每行 ≤1 次 get_memory，top_k 有界）。
        # 2026-09-23（§1306）：实现**提到模块级 `_hydrate_display_fields`**，与快路径**共用一份**
        # （白名单与触发条件都只有一处；两出口各自调用 —— 否则就是「多入口里修一处」的老坑）。
        # 附注（§1306 实测）：原来的白名单里**没有 `agent_id`**（`memories` 表里有这一列且值是对的），
        # 且触发条件只看「没有 content」⇒ 关键字通道的行（自带 content）永远不被水合。
        _hydrate_display_fields(result.get("results") or [], self._adapter)

        # 审计日志
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=None, action="search_hybrid",
                    agent_id=agent_id, persona_id=persona_id,
                    # R4（§785.6 遗留②）：同上一处 —— 记下读到了哪些（截断到 10 条）。
                    details={
                        "query": query, "top_k": top_k, "strategy": strategy,
                        "hits": len(result.get("results", [])),
                        "breakdown": result.get("breakdown", {}),
                        "memory_ids": [r.get("memory_id")
                                       for r in (result.get("results") or [])
                                       if isinstance(r, dict) and r.get("memory_id")][:10],
                    },
                )
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09-04（EXECUTION 558, B2）: full 路径同样应用自我类 top1 限流
        try:
            _q_self = any(w in str(query).lower() for w in ("我", "自己", "自省", "状态", "回顾", "我是谁", "观察到自己", "做了什么"))
            _res_ = result.get("results") or []
            if _res_ and len(_res_) > 1 and not _q_self:
                _SELF_C = {"self-reflection", "self-observation", "self-assessment", "self-narrative"}
                if str((_res_[0].get("category") or "")).lower() in _SELF_C:
                    for _ri, _rc in enumerate(_res_):
                        if str((_rc.get("category") or "")).lower() not in _SELF_C:
                            _res_.insert(0, _res_.pop(_ri))
                            break
        except Exception as _e:
            swallow(__name__, _e)
        # ②自适应路由：full 路径标记（2026-09-13：附带记录原始请求，便于发现被强制降级）
        result.setdefault("breakdown", {})["routing"] = "full"
        result["breakdown"]["routing_requested"] = _routing_requested
        # 2026-09-27（§1381）：按**生效路由**算，不再拿 _routing_requested 表达（后者在 REST 路径恒为 "auto" ⇒ 原式恒 True ⇒ 与事实相反，见 _forced_light_flag）。
        result["breakdown"]["pg_forced_light"] = _forced_light_flag(_pg_mode, routing)
        result["breakdown"]["routing_defaulted"] = bool(_routing_requested != "full")
        result["breakdown"]["vector_channel"] = _vector_channel_impl(self)
        result["breakdown"]["pg_forced_light_scope"] = (
            "true only when the PG adapter actually ran the light path; "
            "routing_defaulted separately reports that the caller did not explicitly request full")

        # 2026-09-02（brain fix）：检索出口统一解密（enc:v1 → 明文；fail-open）
        from trinity.security.crypto import decrypt_content
        for _r in (result.get("results") or []):
            if isinstance(_r, dict) and _r.get("content"):
                _r["content"] = decrypt_content(_r["content"])

        # 2026-09-16（EXECUTION 775）：doc 域两阶段检索——目录粗排（路径+各级标题的零依赖 BM25）
        # → 命中文档内章节精排并补位。动机：EXECUTION 774 实测 doc 语料占记忆池 4%，
        # 生产混合检索 doc 域 recall@page 仅 0.25，而纯目录粗排 R@1=0.90。
        # 默认 off（TRINITY_DOC_ROUTE=on 开启）；任何异常 fail-open，不改原排序。
        try:
            from trinity.retrieval.doc_router import enabled as _dr_on, rerank as _dr
            if _dr_on() and not result.get("_doc_routed"):
                result["results"] = _dr(query, result.get("results"), adapter=self._adapter, top_k=top_k)
                result["_doc_routed"] = True
                result.setdefault("breakdown", {})["doc_route"] = "on"
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-13（P2 LLM 列表式重排）：默认 off；先重排、再判相关度（幂等标记同上）。
        try:
            from trinity.retrieval.llm_rerank import enabled as _lr_on, llm_rerank as _lr
            if _lr_on() and not result.get("_llm_reranked"):
                result["results"] = _lr(query, result.get("results"))
                result["_llm_reranked"] = True
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-13（H2-3）：检索侧「更新值优先」（默认 off）。放在门控之前，使门控看到最终次序。
        try:
            from trinity.retrieval.recency_pref import enabled as _rw_on, prefer_newer as _rw
            if _rw_on():
                result["results"] = _rw(result.get("results"))
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-13（H2-3）：时序上下文日期标注 + 时间序（默认 off）
        try:
            from trinity.retrieval.temporal_context import enabled as _tc_on, annotate_and_order as _tc
            if _tc_on(query):
                # reorder=False：**只标注、不重排**（重排会让 top-k 变成「最旧 k 条」）
                result["results"] = _tc(result.get("results"), reorder=False)
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-13（W2/P0-b）：偏好卡；**默认旁路**（result["preference_card"]）
        try:
            from trinity.retrieval.preference_card import (
                enabled as _pc_on, attach as _pc, build_card as _pc_build)
            if _pc_on(query):
                if _synthetic_inline():
                    result["results"] = _pc(result.get("results"), query)
                else:
                    _card = _pc_build(result.get("results") or [])
                    if _card:
                        result["preference_card"] = _card["text"]
        except Exception as _e:
            swallow(__name__, _e)

        result = self._apply_post_stages(query, result, agent_id=agent_id,
                                         persona_id=persona_id, _qexp_meta=_qexp_meta)

        result = _memory_policy_attach(query, result)  # 687 元认知策略旁路（见 memory_policy_hook）

        # 2026-09-16：作用域超取的**收尾截断**（所有后置阶段之后，用最终次序）。
        # 不截断会返回超出调用方要求条数的结果（下游按 top_k 假设做预算/上下文装配）。
        account_returned_hits(result, adapter=self._adapter, enabled=account)   # t18/t32 出口记账
        return result
