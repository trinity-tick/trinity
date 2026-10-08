# -*- coding: utf-8 -*-
"""_HybridIndexMixin — 混合检索器与 BM25/RRF 索引维护。

2026-09-11 结构梳理（防回胀拆分）：自 _search.py **尾部整体迁出**，
按 docs/refactor-plan-20260909.md 的行数预算（_search.py <= 1400 行）执行。
**行为不变**——仅搬家，方法实现逐行未改：

  - hybrid_retriever     : HybridRetriever 延迟初始化属性（向量/FTS + BM25 + 图谱）
  - _ensure_bm25_index   : BM25 索引构建
  - _wait_bm25_ready     : 索引就绪等待
  - _rrf_merge           : Reciprocal Rank Fusion 融合

顺带修一个既有隐患：原代码在 PPR 回退分支直接用了未定义的 logger
（原 _search.py 只在别的函数里用 __import__ 局部赋值），一旦触发会 NameError；
本模块补上标准模块级 logger。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional
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


logger = logging.getLogger("trinity.core.client.hybrid_index")

# 2026-09-29（外部审计修复）：BM25 机制迁出至 _bm25.py（体积预算 + 职责拆分）。
from ._bm25 import _Bm25Mixin  # noqa: E402


def _ppr_seed_source() -> str:
    """PPR 种子来源（2026-09-27，EXECUTION §1390）。

    靶心（§1389 实测）：图通道的候选**全部**来自 PPR（source 分布 {"graph_ppr": 40}），
    而 PPR 的实体种子为 **0** —— `search_entities(整句查询)` 返回空（整句自然语言不是实体名），
    于是落入兜底分支 `adapter.search_memories`（**词法检索**）。
    ⇒ 40 条候选 100% 源于词法种子，再被 RRF 排到最前（最终 top-10 有 10/10 来自图通道）。

    **已转正（2026-09-27 §1390）**：默认 = `semantic`。依据是决策级 A/B（n=14，同一批查询）：
    批量类 **125/140 (89%) → 74/140 (53%)**、∩真最近邻 **1 → 14**、
    逐条「批量减少 10 / 增加 2 / 持平 2」、`abstain` 0→0、p50 **616ms → 419ms**。

    开关 `TRINITY_PPR_SEED`：`semantic`（默认）/ **`lexical`（回滚位，逐字回到改动前）**。
    未知值一律回落 **semantic**（默认值），拼错不会意外退回旧行为。
    """
    v = (os.environ.get("TRINITY_PPR_SEED") or "").strip().lower()
    return "lexical" if v == "lexical" else "semantic"

def _use_embedding_channel(adapter: Any, use_ann: bool) -> bool:
    """向量通道是否该走**嵌入检索**（2026-09-27，EXECUTION §1388）。

    靶心（§1387）：原实现是 `if self.use_ann:` 二选一，而 `use_ann` **默认 False**
    （api 侧用默认构造）⇒ else 分支把向量通道接成了 `adapter.search_memories`，
    那是 **ts_rank + ILIKE 词法检索**，不含 `embedding <=>`。
    实测代价（§1388 三臂对照 5 查询）：词法臂 ∩ 真最近邻 = **0/10**(×4) 与 2/10，
    批量文档 7-10/10；嵌入臂 = **10/10** 且批量 0/10，p50 8.2ms vs 词法 80.4ms。

    **为什么不是简单地恒用 `_vector_search`**：非 PG 且未开 ANN 时，
    `_vector_search` 会走到「拉全量 + 内存建索引」那条重路（`_search.py:1112+`）；
    而 **PG 适配器在 `_search.py:1078-1091` 就早返回**（pgvector HNSW 直查，免全量）。
    所以按适配器分流：**PG 恒走嵌入**，非 PG 沿用词法（保留原作者的性能意图）。

    开关 `TRINITY_VECTOR_CHANNEL`：**`lexical`（默认 = 今天的生产行为）** / `auto` / `pgvector`。

    **为什么默认仍是 lexical（不是 auto）**：§1388 端到端 A/B 实测 —— 接线改对之后，
    `search_hybrid` 的最终 top-10 **一点没变**（相似度逐条相同、批量文档 9/10/10/10/6 不变、
    ∩真最近邻 0/0/0/0/1 不变，只有组内次序动了）。即**向量通道的候选被下游融合完全压掉**，
    修接线是**必要但不充分**。按 §1383 预注册「任一判据不满足即记负结果、保持默认」，
    故**不转正**；真正的约束在融合/排序阶段（下一轮靶心）。
    """
    mode = (os.environ.get("TRINITY_VECTOR_CHANNEL") or "lexical").strip().lower()
    if mode == "lexical":
        return False
    if mode == "pgvector":
        return True
    if adapter is None:
        return False
    is_pg = "postgres" in type(adapter).__name__.lower()
    return bool(is_pg or use_ann)


def vector_channel_impl(adapter: Any, use_ann: bool) -> str:
    """**派生**「`vector` 这个键名背后究竟是哪条实现」——供响应披露，不许猜。

    2026-09-29（外部审计修复，根因 B「能力声明改为派生事实」）：

    实测线上响应里 `breakdown.vector_channel` 恒为 **"unknown"**。
    原因：`core/client/_hybrid_search.py` 的两处 light 出口写的是
        `"vector_channel": locals().get("_vec_status", "unknown")`
    而 `_vec_status` **只在 PG 门控块内绑定**（那里开头是
    `_pg = ...find("postgres") >= 0; if _pg and hasattr(...)`）
    ⇒ **在 SQLite 上永远取不到**，于是这个"为披露实现而存在"的字段恒为 unknown。

    更麻烦的是：同一份响应里还有一个叫 `vector` 的计数。按
    `_use_embedding_channel()` 的既有实测结论，非 PG 且未开 ANN 时该通道由
    `adapter.search_memories`（ts_rank/ILIKE，**词法**）服务 ⇒ 读者会把**词法的数**
    读成**向量检索的贡献**（正是"声明与实际脱节"这一类）。

    本函数把事实**派生**出来，取代字面量 "unknown"：返回值形如
    `embedding:PostgreSQLAdapter` / `lexical:SQLiteAdapter.search_memories` /
    `none:no_adapter`。**注意**：本函数只做**披露**，不改变任何检索行为 ——
    是否把通道切成嵌入由 `TRINITY_VECTOR_CHANNEL`（默认 lexical，见上）决定。
    """
    try:
        if adapter is None:
            return "none:no_adapter"
        name = type(adapter).__name__
        if _use_embedding_channel(adapter, use_ann):
            return "embedding:%s" % name
        return "lexical:%s.search_memories" % name
    except Exception:
        return "unknown:derive_failed"


class _HybridIndexMixin(_Bm25Mixin):
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
            # 2026-09-27（§1388）：原为 `if self.use_ann:`，而 use_ann 默认 False ⇒
            # PG 上把向量通道接成了词法检索。改用判据函数：PG 恒走嵌入。
            if _use_embedding_channel(self._adapter, self.use_ann):
                def _vector_search_fn(q: str, top_k: int):
                    return self._vector_search(q, top_k, account=False)   # t28/R-6：出口层记账
            else:
                def _vector_search_fn(q: str, top_k: int):
                    return self._adapter.search_memories(
                        query=q, top_k=top_k, touch=False,   # t21：通道层不记账（出口层记账）
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
                except Exception as _e:
                    swallow(__name__, _e)
                # 直接查询记忆作为种子兜底
                # 2026-09-27（§1390）：种子默认仍是词法（= 今天的行为）；
                # 置 TRINITY_PPR_SEED=semantic 时改用嵌入检索取种子。
                # 理由：§1389 实测实体种子恒为 0 ⇒ 候选 100% 源于这条兜底。
                if not seed_mids:
                    try:
                        if _ppr_seed_source() == "semantic":
                            hits = self._vector_search(q, 8, account=False) or []   # t28/R-6：出口层记账
                            if not hits:
                                # fail-open：拿不到语义种子（嵌入引擎不可用等）时
                                # 回落词法，**避免整个图通道静默消失**（那比退化更坏）。
                                hits = self._adapter.search_memories(query=q, top_k=8, touch=False)  # t21
                        else:
                            hits = self._adapter.search_memories(query=q, top_k=8, touch=False)  # t21
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
            # 【2026-09-13 回滚记录】659.33 曾据台账"NEVER_ACTIVE"判定删除本开关，**判定有误**：
            # benchmark/hard_holdout_eval.py:96 与 benchmark/pagetree_ab_compare.py:173/185
            # 会**动态设置**它（os.environ[...] = "on"），而台账的 set 点只扫
            # dsh-ops/、dsh-plugin/、scripts/ —— **不含 benchmark/**，故误判为"全仓无设置点"。
            # 删除会让那两个 A/B 对照的两臂行为相同。已原样恢复。
            _pt_fn = None
            if os.environ.get("TRINITY_PAGETREE_HYBRID", "off").strip().lower() in ("1", "on", "true", "yes"):
                try:
                    if self.load_pagetree() is not None:
                        # novel_only：页树通道只贡献基础召回未命中的记忆（只增不减）
                        def _pt_fn(q, k):
                            return self.pagetree_search(
                                query=q, top_k=k, novel_only=True,
                            ).get("results", [])
                except Exception:
                    _pt_fn = None
            # 2026-08-25（可测域扩展）：通道权重 + rrf_k 支持 env 覆盖——
            # 此前硬编码默认（vector 0.35/bm25 0.25/graph 0.25/agg 0.15/proc 0.10,
            # rrf_k 60），自进化 A/B 无法测。env 未设时行为不变（向后兼容）。
            def _w(k, d):
                return float(os.environ.get(k, str(d)))
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
                # 2026-09-15（R41-P23）：**此处原传 `aggregator_weight=_w(
                # "TRINITY_AGGREGATOR_WEIGHT", 0.15)`，已移除——它是个死配置。**
                # 原因：构造 HybridRetriever 时**从未传 `aggregator_fn`**
                # ⇒ `self._aggregator_fn is None` ⇒ 权重恒取 0
                # （hybrid_retriever.py L725/L812）且聚合器通道恒 `return []`（L415）。
                # **构造性证明**（temp/_prove_aggregator_inert.py，确定性、不受
                # 检索非确定性干扰）：一次 `search_hybrid` 期间聚合器方法被调用
                # **0 次**，返回体 breakdown 亦为 `"aggregator": 0`。
                # 传一个永不生效的权重只会让读者以为该通道是活的
                # （`api/server/_pool_refresh.py` 的模块文档正是这么写的）。
                # **行为逐字不变**：`HybridRetriever.aggregator_weight` 的默认值
                # 同为 0.15，且它在 `_aggregator_fn is None` 时恒被忽略。
                #
                # 若将来要真正接上聚合池召回，**必须先解决两件事**，否则得不偿失：
                #   ① 成本：聚合器嵌入后端要导入 sklearn+narwhals（≈8s）并对 13k 池
                #      做 TF-IDF（≈4.3s）；现由"按需预热"隔离在检索路径之外
                #      （见 agents/aggregator/_init.py），接回检索即重新压到冷启动
                #      关键路径上（实测冷启动会从 ≈5.8s 退回 14.8~17.4s）。
                #   ② 判据：现有检索 A/B 口径**不可复现**——同一臂自比 top-5
                #      不一致 ≈30–40%（temp/_ab_aggregator.py），无法判定接线带来的
                #      排序变化是收益还是噪声。先修评估口径再接。
                # 注：`TRINITY_AGGREGATOR_WEIGHT` 在 **light 路径**另有映射
                # （`_hybrid_search.py` L543 → 辅助通道 "prior"，高重要度先验，
                # 与聚合池并非同一物）——本处移除不影响那条。
                # 2026-09-30（外部审计 · 死配置清理）：此处原传
                # `procedural_weight=_w("TRINITY_PROCEDURAL_WEIGHT", 0.10)`，与上面
                # 已移除的 `aggregator_weight` **完全同族** —— 构造时同样**从未传
                # `procedural_store`**，故该权重恒被忽略：
                #   · `_fusion_fuse`（hybrid_retriever.py:726）：
                #     `self.procedural_weight if self._procedural_store is not None else 0.0`
                #     ⇒ 恒取 0.0；
                #   · `_rrf_fuse`（hybrid_retriever.py:813）：
                #     `wp = 1.0 if self._procedural_store is not None else 0.0`
                #     ⇒ 恒取 0.0。
                # 全仓核查：`procedural_store=` 只出现在 hybrid_retriever.py 自带的
                # 自测块（:1031-1041，那里直接构造 HybridRetriever 并走其**属性**默认值），
                # **生产路径无任何调用方传入** ⇒ `TRINITY_PROCEDURAL_WEIGHT` 是死旋钮。
                # **行为逐字不变**：`HybridRetriever.procedural_weight` 的属性默认值
                # 同为 0.10，且它在 `_procedural_store is None` 时恒被忽略。
                # 若将来真要接上程序性记忆召回，需先解决与聚合器同款的两件事
                # （成本不得压回冷启动关键路径 + 评估口径可复现），再连权重一起接入。
                rrf_k=int(os.environ.get("TRINITY_RRF_K", "60")),
                # 2026-08-26（PageIndex 借鉴 Phase 1）：页树通道——
                # env TRINITY_PAGETREE_HYBRID=on 且页树存在时接入融合。
                pagetree_fn=_pt_fn,
            )
        # 2026-09-27（接线修复）：TTL 刷新原只挂在**首次构建**分支 ⇒ retriever 一建好 `is None`
        # 永假 ⇒ `_maybe_refresh_bm25()` 稳态下**永不执行**（09-18 那版是死代码；实测写 48 块后
        # 原文自检索查不到自己）。回滚 `TRINITY_BM25_REFRESH_TTL=0`；判据 test_bm25_refresh_wiring.py。
        if self._hybrid_retriever is not None:
            self._maybe_refresh_bm25()
        return self._hybrid_retriever
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

