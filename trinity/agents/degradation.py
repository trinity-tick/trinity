"""
Trinity P1-4: Degradation Strategy Framework.
Three-tier fallback: FULL → DEGRADED → MINIMAL
"""

import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ServiceTier(Enum):
    FULL = "full"           # All channels active
    DEGRADED = "degraded"   # Core channels only (keyword + FAISS)
    MINIMAL = "minimal"     # Keyword-only fallback


class DegradationManager:
    """Three-tier degradation manager with health monitoring.

    2026-09-10（EXECUTION 661）：区分**贡献结果的通道**与**仅注册的通道**。
    retrieval_v47 是 47 路通道注册器，无独立数据源，search() 恒返回 [] 
    （modules/second_brain/engine_guardian_retrieval.py::RetrievalSystemV47.search
    硬编码 `return []`，模块 # status: frozen），但旧实现
    把它与 keyword/vector 等一视同仁地列进 statistics()["active_channels"] →
    /health 报 retrieval_v47: true，运维据此误判"47 通道在线"（健康假象，与
    docs/EVAL_CREDIBILITY.md 的诚实口径矛盾）。现拆成两个口径：
      - active_channels / contributing_channels：真正贡献结果的通道；
      - registry_only_channels：仅注册、不贡献（附原因）。
    降级层级判定（_FULL_CHANNELS）保持不变，避免影响既有降级契约。

    引用约定（2026-10-06，引用漂移修复）：本文件里的代码引用一律用**符号级**形式
    `相对 trinity/ 的路径::定义处的符号名`，例如 `aggregator/_search.py::_SearchMixin.query`。
    两点说明：
      · 符号名取**实际定义该代码的类**（mixin / 私有实现类），而不是最终继承它的
        `MemoryAggregator` —— 后者并不在 `_search.py` / `_init.py` 里定义；
      · 不再写死行号：行号是**快照**，会随它上方任何无关编辑漂移；表达式与符号才是
        **身份**（同 tests/unit/test_capability_roster_ratchet.py 的既定理由）。
        现场核对用 `scripts/citation_check.py`（只读），逐条校验符号/内容是否仍然成立。
    """

    # 仅注册、不贡献结果的通道 → 原因（可由调用方补充/覆盖）
    REGISTRY_ONLY: Dict[str, str] = {
        "retrieval_v47": "frozen channel registry: search() always returns [] (no data source)",
    }

    # 2026-09-28（四维体检 S2-C 口径修复）：**贡献名册的证据分级**。
    #
    # 动机：`get_active_channels()` 报的 5 个（keyword/vector/second_brain/
    # exabase/beamlight）来自下面那个**硬编码字面量** `self._health`，而该字面量
    # **两个方向都不准**（逐条核过代码，证据见下表 class/evidence）：
    #   · 2 个真贡献（keyword、vector）；
    #   · 1 个有调用点但数据源恒空（exabase）；
    #   · 2 个根本不是融合通道（second_brain 是 RRF 之后的重排微调；
    #     beamlight 全仓无调用点且其类**没有 search() 方法**）；
    #   · 而 2 个**真在融合里 append** 的参与者（graph/PPR、serendipity）
    #     **压根不在名册里** —— 见 FUSION_CHANNELS_UNLISTED。
    # `/diagnostics` 另有第三个数（retrieval_channels_contributing=0，作用于
    # 冻结的 V47 47 项表）。三面并存 ⇒ 任何"我们有 N 个通道"都不可引用。
    #
    # **本字典只用于报数，不参与任何逻辑**：`_health` / `_FULL_CHANNELS` /
    # `_recompute_tier()` 一律不动（改 `_health` 会连带改降级层级语义，
    # 那是行为变更，不是口径修复）。故本项**零行为回归**。
    CHANNEL_EVIDENCE: Dict[str, Dict[str, str]] = {
        "keyword": {
            "class": "wired_contributing",
            "evidence": ("aggregator/_search.py::_SearchMixin.query seeds kw_results "
                         "(keyword engine query) and initialises "
                         "ranked_lists = [kw_results]"),
        },
        "vector": {
            "class": "wired_contributing",
            "evidence": ("aggregator/_search.py::_SearchMixin.query "
                         "(gated by mode != 'keyword'); appends via "
                         "ranked_lists.append(vec_dvs)"),
        },
        "exabase": {
            "class": "wired_but_empty_source",
            "evidence": ("aggregator/_search.py::_SearchMixin.query has a real call site "
                         "(`self._exabase.search(...)`), but ExabaseRetrieval.memory_pool is "
                         "initialized empty "
                         "(second_brain/engine_retrieval.py::ExabaseRetrieval.__init__) and no "
                         "aggregator code path calls add_memory/integrate_* on it => search() "
                         "always [] => `if exa_dvs:` never fires"),
        },
        "second_brain": {
            "class": "not_a_fusion_channel",
            "evidence": ("never appends to ranked_lists; only a post-RRF rerank nudge inside "
                         "aggregator/_search.py::_SearchMixin.query (SelectiveRecallRouter "
                         "block, priority +0.1, cap 0.9). On the merge path it is used as an "
                         "embedder (aggregator/_ingest.py::_IngestMixin.merge_if_similar -> "
                         "ContextualEmbedder)"),
        },
        "beamlight": {
            "class": "no_call_site_at_all",
            "evidence": ("self._beamlight is assigned exactly once "
                         "(aggregator/_init.py::_InitMixin.__init__) and is **never read "
                         "anywhere in this repo** (0 read sites; the only other mention is a "
                         "test write, tests/unit/test_statistics_lockscope.py); BEAMLIGHT "
                         "(second_brain/engine_retrieval.py::BEAMLIGHT) has NO search() "
                         "method, so it could not satisfy the channel contract even if called"),
        },
        "retrieval_v47": {
            "class": "registry_only_frozen",
            "evidence": ("CONTRIBUTES=False "
                         "(engine_guardian_retrieval.py::RetrievalSystemV47.CONTRIBUTES), "
                         "DATA_SOURCE=None "
                         "(engine_guardian_retrieval.py::RetrievalSystemV47.DATA_SOURCE), "
                         "search() hardcoded `return []` "
                         "(engine_guardian_retrieval.py::RetrievalSystemV47.search)"),
        },
        "aggregator": {
            "class": "self",
            "evidence": "the aggregator itself; excluded from active_channels by definition",
        },
    }

    # 真在融合路径里 append、但**不在**本名册里的参与者（显式报数，消除静默遗漏）
    FUSION_CHANNELS_UNLISTED: Dict[str, str] = {
        "graph_ppr": "aggregator/_search.py::_SearchMixin.query (Graph + PPR gate)",
        "serendipity": ("aggregator/_search.py::_SearchMixin.query "
                        "(TRINITY_SERENDIPITY != 'off' gate, default 'on')"),
    }

    # ── 2026-09-28（落地率实现）：**真名册**（capability_roster）────────────────
    #
    # 判据只有一条，且可机械核验：**有没有真的往 `ranked_lists` append**（file:line 为证）。
    # 这是全系统**唯一**可被引用为"能力声明"的字段；`contributing_channels` 不是。
    #
    # 与 `contributing_channels`（`self._health` 的 7 键硬编码字面量）的关系：
    #   roster ∩ declared      = {keyword, vector}
    #   roster − declared      = {graph_ppr, serendipity}      ← 真在 append，却不在名册
    #   declared − roster      = {second_brain, exabase, beamlight}  ← 在名册，但不是融合通道
    # ⇒ 这才是"名字"与"真"的差集，逐条可查（见 NON_CAPABILITY_NAMES）。
    #
    # **`_health` / `_FULL_CHANNELS` / `_recompute_tier()` 仍然一律不动**，原因不只是保守：
    # `_health` 还被 `is_channel_available()` 用来**门控真实调用**
    # （`aggregator/_search.py::_SearchMixin.query` 的
    # `self._degradation.is_channel_available("exabase")`），改它会改行为，不是口径修复。
    FUSION_CHANNELS_WIRED: Dict[str, Dict[str, str]] = {
        "keyword": {
            "contribution": "seed",
            "site": ("aggregator/_search.py::_SearchMixin.query "
                     "(`ranked_lists = [kw_results]`; kw_results built by the keyword engine)"),
            "gate": "unconditional - it IS the initial element, so it is always present",
            "measured": "/memory/search/hybrid breakdown.keyword @2026-09-28 11:13:36",
        },
        "vector": {
            "contribution": "append",
            "site": ("aggregator/_search.py::_SearchMixin.query "
                     "(`ranked_lists.append(vec_dvs)`)"),
            "gate": ("mode != 'vector' early-return in the same method "
                     "(_SearchMixin.query) and vec_dvs non-empty"),
            "measured": "breakdown.vector=2 @2026-09-28 11:13:36",
        },
        "graph_ppr": {
            "contribution": "append",
            "site": ("aggregator/_search.py::_SearchMixin.query "
                     "(`ranked_lists.append(_active_only(graph_dvs))`)"),
            "gate": ("self._graph_channel is not None and query_text and vec_ids "
                     "(same method, _SearchMixin.query)"),
            "measured": "breakdown.graph=2 @2026-09-28 11:13:36",
        },
        "serendipity": {
            "contribution": "append",
            "site": ("aggregator/_search.py::_SearchMixin.query "
                     "(`ranked_lists.append(ser_dvs)`)"),
            "gate": ("self._serendipity is not None and query_text and vec_ids "
                     "and TRINITY_SERENDIPITY != 'off' (default 'on'); same method, "
                     "_SearchMixin.query"),
            "measured": ("gate observed; 2026-08-17 fixed a silent AttributeError "
                         "(float(dv.importance) on a field DimensionVector lacks) that "
                         "made this channel spin empty without any error"),
        },
    }

    # 判据的精确表述（棘轮测试按此核验，见 tests/unit/test_capability_roster_ratchet.py）：
    #   真名册成员 = 在聚合器融合路径里**向 `ranked_lists` 提供一个非空元素**的参与者，
    #   机制二选一：`seed`（`aggregator/_search.py::_SearchMixin.query` 的
    #   `ranked_lists = [kw_results]` 初值）或 `append`（同函数的 5 个 append 点：
    #   `ranked_lists.append(vec_dvs)`、`ranked_lists.append(_active_only(v47_dvs))`、
    #   `ranked_lists.append(_active_only(exa_dvs))`、
    #   `ranked_lists.append(_active_only(graph_dvs))`、`ranked_lists.append(ser_dvs)`）。
    #   ⇒ 本仓 `.py` 源码里 `ranked_lists.append` 实测共 **7 处**（按 AST 调用点计，
    #     不含测试与文档里的字面量字符串）：
    #       · trinity/agents/aggregator/_search.py 5 处（vec_dvs / _active_only(v47_dvs) /
    #         _active_only(exa_dvs) / _active_only(graph_dvs) / ser_dvs）；
    #       · benchmark/locomo_real_eval_v2.py:109 与
    #         benchmark/locomo_real_eval_v2.py:114 各 1 处（这两处表达式**完全相同**，
    #         都是 `ranked_lists.append(dvs)`，符号与表达式都无法区分二者 ⇒ 只有这里
    #         必须保留行号；它们由 scripts/citation_check.py 带**期望子串**现场校验，
    #         该文件一改就会红，不会被静默放过）。属评测脚本自建的融合，不进本聚合器名册。
    #     其中 `_active_only(v47_dvs)`（retrieval_v47）与 `_active_only(exa_dvs)`（exabase）
    #     **有 append 点却恒为空**（见 NON_CAPABILITY_NAMES 的逐条理由）
    #     ⇒ 有 append 点 **不等于** 有能力。
    ROSTER_CRITERION = (
        "contributes a non-empty element to ranked_lists via seed "
        "(`ranked_lists = [kw_results]`) or append (`ranked_lists.append(...)`: 5 sites in "
        "trinity/agents/aggregator/_search.py, 2 in benchmark/locomo_real_eval_v2.py); a call "
        "site alone does NOT qualify - the appended list must be able to be non-empty")

    # 出现在 `contributing_channels` 里、但**不是**融合能力 → 显式归类，不许再混进能力声明。
    NON_CAPABILITY_NAMES: Dict[str, str] = {
        "second_brain": ("reranker_not_a_channel: never appends to ranked_lists; only a "
                         "post-RRF rerank nudge "
                         "(aggregator/_search.py::_SearchMixin.query, SelectiveRecallRouter)"),
        "exabase": ("wired_but_empty_source: real call site at "
                    "aggregator/_search.py::_SearchMixin.query, but "
                    "ExabaseRetrieval.memory_pool is created empty "
                    "(second_brain/engine_retrieval.py::ExabaseRetrieval.__init__) and NO "
                    "production path calls add_memory (only the /diagnostics self-test does, "
                    "engine_core.py::_LifecycleManager._collect_retrieval_cb54) "
                    "=> search() always [] => `if exa_dvs:` never fires"),
        "beamlight": ("no_call_site_at_all: assigned once "
                      "(aggregator/_init.py::_InitMixin.__init__) and **never read anywhere in "
                      "this repo** (0 read sites; the only other mention is a test write, "
                      "tests/unit/test_statistics_lockscope.py); BEAMLIGHT "
                      "(second_brain/engine_retrieval.py::BEAMLIGHT) has NO search() method at "
                      "all"),
        "retrieval_v47": ("registry_only_frozen: CONTRIBUTES=False "
                          "(engine_guardian_retrieval.py::RetrievalSystemV47.CONTRIBUTES), "
                          "DATA_SOURCE=None "
                          "(engine_guardian_retrieval.py::RetrievalSystemV47.DATA_SOURCE), "
                          "search() hardcoded `return []` "
                          "(engine_guardian_retrieval.py::RetrievalSystemV47.search)"),
        "aggregator": "self: the aggregator itself; excluded from active_channels by definition",
    }

    def __init__(self, registry_only: Optional[Dict[str, str]] = None):
        self._registry_only: Dict[str, str] = dict(self.REGISTRY_ONLY)
        if registry_only:
            self._registry_only.update(registry_only)
        self._health: Dict[str, bool] = {
            "keyword": True,
            "vector": True,
            "second_brain": True,
            "retrieval_v47": True,
            "exabase": True,
            "beamlight": True,
            "aggregator": True,
        }
        self._tier: ServiceTier = ServiceTier.FULL
        self._failure_counts: Dict[str, int] = {}
        self._degradation_history: List[dict] = []
        self._FULL_CHANNELS = {"keyword", "vector", "second_brain", "retrieval_v47", "exabase"}
        self._DEGRADED_CHANNELS = {"keyword", "vector"}
        self._MINIMAL_CHANNELS = {"keyword"}

    def mark_failure(self, channel: str, reason: str = "") -> bool:
        """Mark a channel as failed. Returns True if tier changed."""
        self._health[channel] = False
        self._failure_counts[channel] = self._failure_counts.get(channel, 0) + 1
        self._degradation_history.append({
            "event": "failure", "channel": channel, "reason": reason,
            "failures": self._failure_counts[channel]
        })
        logger.warning("Degradation: %s marked FAILED (x%d) — %s",
                       channel, self._failure_counts[channel], reason)
        return self._recompute_tier()

    def mark_recovery(self, channel: str):
        """Mark a channel as recovered."""
        self._health[channel] = True
        self._degradation_history.append({"event": "recovery", "channel": channel})
        logger.info("Degradation: %s recovered", channel)
        self._recompute_tier()

    def _recompute_tier(self) -> bool:
        """Recompute service tier based on health. Returns True if tier changed."""
        prev_tier = self._tier
        active = {ch for ch, ok in self._health.items() if ok}

        if self._FULL_CHANNELS.issubset(active):
            self._tier = ServiceTier.FULL
        elif self._DEGRADED_CHANNELS.issubset(active):
            self._tier = ServiceTier.DEGRADED
        else:
            self._tier = ServiceTier.MINIMAL

        changed = prev_tier != self._tier
        if changed:
            logger.warning("Degradation: tier changed %s → %s", prev_tier.value, self._tier.value)
        return changed

    @property
    def tier(self) -> ServiceTier:
        return self._tier

    def is_channel_available(self, channel: str) -> bool:
        return self._health.get(channel, False)

    def get_active_channels(self) -> List[str]:
        """**贡献结果的**在用通道（不含 aggregator 自身，也不含仅注册的通道）。"""
        return [
            ch for ch, ok in self._health.items()
            if ok and ch != "aggregator" and ch not in self._registry_only
        ]

    def get_registry_only_channels(self) -> List[str]:
        """仅注册、不贡献结果的通道（健康上报用，勿计入"在用通道"）。"""
        return [ch for ch in self._health if ch in self._registry_only]

    def registry_only_reasons(self) -> Dict[str, str]:
        return {ch: self._registry_only[ch] for ch in self.get_registry_only_channels()}

    def statistics(self) -> dict:
        active = self.get_active_channels()
        return {
            "tier": self._tier.value,
            # 2026-09-27（自证面清退）：上报的 health **只含贡献通道**。
            # 旧实现把 retrieval_v47（CONTRIBUTES=False、search() 恒返回 []、
            # 模块已 frozen）与 keyword/vector 并列上报，读起来像
            # 一条能力声明 —— 与 R9「健康假象」同族。剔除必须显式报数
            # （health_all），且自带口径说明（health_scope），不许静默丢信息。
            "health": {ch: ok for ch, ok in self._health.items() if ch not in self._registry_only},
            "health_all": dict(self._health),
            "health_scope": ("contributing channels only; registry-only channels are listed "
                             "under registry_only_channels and are not capability claims"),
            "failure_counts": dict(self._failure_counts),
            "degradation_events": len(self._degradation_history),
            # 2026-09-10：active_channels 只含真正"可用"的通道；
            # 新增 registry_only_* 说明"注册但恒空"的通道，消除健康假象。
            "active_channels": active,
            # 2026-09-28：`contributing_channels` 原本直接等于 `active`，而 `active` 是从
            # `_health`（`DegradationManager.__init__` 里写死的 7 键字面量）过滤出来的 —— 也就是说这个键
            # 名义上声明"谁在**贡献结果**"，实际复制的却是"谁**可用**"。实测后果：/health 宣称
            # 5 条在贡献，而同一个 payload 里的 channel_evidence（就在下面几行）自己写着
            # beamlight "has NO search() method at all"、exabase "search() always []" ——
            # 一处接口内自相矛盾，而且两处都是绿的。
            # 现在**从证据名册推导**：FUSION_CHANNELS_WIRED 的每个成员都带调用点、门控条件
            # 与**实测 breakdown 数字**，ROSTER_CRITERION 是判定成员资格的判据。
            # `_health` / `active_channels` 语义**不动**（可用性口径），因为它还被
            # `is_channel_available()` 用来门控真实调用
            # （aggregator/_search.py::_SearchMixin.query），
            # 改它会改行为，不是口径修复。
            "contributing_channels": sorted(self.FUSION_CHANNELS_WIRED.keys()),
            "contributing_criterion": self.ROSTER_CRITERION,
            "contributing_scope": ("derived from FUSION_CHANNELS_WIRED (each entry carries its "
                                   "call site, gate and a measured breakdown reading), NOT from "
                                   "`_health`; `active_channels` remains the AVAILABILITY list "
                                   "and the two are allowed to differ"),
            # 2026-09-28：把"名册里有、`_health` 里没有"的缺口**显式报出来**，而不是让
            # 它表现为一个静默的不一致。实测：graph_ppr 与 serendipity 确实向 ranked_lists
            # 贡献（见 FUSION_CHANNELS_WIRED 的门控与实测 breakdown 数字），但 `_health`
            # 从未登记它们 —— 于是它们既不在 active_channels，也不在 registry_only：
            # 属于**漏报**（旧实现下这一点完全不可见）。
            # 这里**不改 `_health`**：它会参与 tier 计算，且被
            # tests/unit/test_capability_roster_ratchet.py::test_health_semantics_untouched
            # 钉死为 7 键集合；改它属于行为变更，
            # 应作为独立决策。先把缺口变成可读数字，供后续修复。
            "active_channels_missing_wired": sorted(
                set(self.FUSION_CHANNELS_WIRED) - set(active)),
            "registry_only_channels": self.get_registry_only_channels(),
            "registry_only_reasons": self.registry_only_reasons(),
            # 2026-09-28（S2-C 口径修复）：名册的**逐条代码证据** + **漏报名册**。
            # 纯报数（不改任何逻辑）：让 contributing_channels 不再被读成能力声明。
            "channel_evidence": {
                ch: dict(self.CHANNEL_EVIDENCE.get(ch, {})) for ch in self._health
            },
            "channel_evidence_scope": (
                "per-channel CODE evidence for the name roster above. "
                "2026-09-28 (later the same day): contributing_channels is NO LONGER a hardcoded "
                "literal -- it is DERIVED from FUSION_CHANNELS_WIRED (each entry carries its call "
                "site, gate and a measured breakdown reading). The earlier S2-C wording said "
                "'hardcoded literal', which became a stale claim the moment the derivation landed "
                "-- exactly the failure mode this project keeps hitting, so it is corrected here "
                "rather than left to misdescribe the code. NOTE: contributing_channels is a "
                "*contribution* roster; the ONE field citable as a capability claim remains "
                "capability_roster."),
            "fusion_channels_wired_but_unlisted": dict(self.FUSION_CHANNELS_UNLISTED),
            # ── 2026-09-28（落地率实现）：真名册升为一等公民字段 ──────────────
            # 判据 = 有没有真的往 ranked_lists append（file:line 为证），由代码推导，非字面量。
            # ⇒ **这是唯一可被引用为能力声明的字段**；contributing_channels 只是健康名册。
            "capability_roster": sorted(self.FUSION_CHANNELS_WIRED),
            "capability_roster_evidence": {
                ch: dict(ev) for ch, ev in self.FUSION_CHANNELS_WIRED.items()
            },
            "capability_roster_scope": (
                "THE ONLY field citable as a capability claim. Criterion: a real append to "
                "ranked_lists, with file:line. Derived from code, not a literal. "
                "contributing_channels remains a health roster and is NOT a capability claim. "
                # ── 2026-10-07（t90/B6）：**这两条不是同一条流水线** ──────────────────
                # 实测：名册 = [graph_ppr, keyword, serendipity, vector]（aggregator 融合路径）；
                # 运行期供料面 = [bm25, graph, vector]（客户端 search_hybrid 的 breakdown）。
                # 名字与成员**都不是一回事**：graph_ppr ↔ graph 同物不同名；bm25 属客户端侧；
                # keyword 是 aggregator 的 seed（本来就不该出现在客户端 breakdown 里）；
                # serendipity 有门控（需 vec_ids 非空 + TRINITY_SERENDIPITY != 'off'）。
                # ⇒ **禁止把两者相减**：那会得出"假背离"并制造假红。
                "t90/B6 addendum: this roster belongs to the AGGREGATOR `ranked_lists` FUSION "
                "pipeline; it must NOT be differenced against the CLIENT `search_hybrid` "
                "breakdown (runtime supply face, readable read-only from audit_log rows with "
                "action='search_hybrid'). The two are different pipelines: their names and "
                "members are not the same thing (graph_ppr vs runtime 'graph'; bm25 is "
                "client-side; keyword is the aggregator seed; serendipity is gated). "
                "Differencing them yields a FALSE divergence and false reds."),
            "capability_roster_vs_declared": {
                "contribution_roster": sorted(active),
                "capability_roster": sorted(self.FUSION_CHANNELS_WIRED),
                "declared_but_not_capability": sorted(
                    ch for ch in active if ch in self.NON_CAPABILITY_NAMES),
                "capability_but_not_declared": sorted(
                    ch for ch in self.FUSION_CHANNELS_WIRED if ch not in active),
            },
            # ── 2026-10-07（t90/B6）：把「可用性口径 ≠ 能力口径」变成**显式登记** ────────
            # 实测（t89/t90）：available(5) 与 capability_roster(4) **互不包含** ——
            #   available − roster = [beamlight, exabase, second_brain]（可用但无供料能力）
            #   roster − available = [graph_ppr, serendipity]（真供料但 health 从未登记）
            # ⛔ **不动 `_health`**：它是 `is_channel_available()` 的**真实门控来源**，且
            #   `tests/unit/test_capability_roster_ratchet.py::test_health_semantics_untouched`
            #   逐字钉住那 7 个键；往里加键会同时改 ③ 的**取值含义**与 tier 计算 ⇒ 那不是"登记"。
            # ⇒ 这里只做**登记与命名**：让两个方向都可见，由判据钉住。
            "health_roster_alignment": {
                "availability_face": sorted(active),
                "availability_scope": (
                    "AVAILABILITY only（`_health` 过滤出来的可用性口径）；它门控真实调用"
                    "（is_channel_available），**不是**能力声明，也不等于融合名册"),
                "wired_but_not_in_availability": sorted(
                    ch for ch in self.FUSION_CHANNELS_WIRED if ch not in active),
                "in_availability_but_not_wired": sorted(
                    ch for ch in active if ch in self.NON_CAPABILITY_NAMES),
                "scope": (
                    "两个方向**都必须读**：只读 availability_face 会**多算**未接线通道；"
                    "只读 capability_roster 会**漏掉**真供料通道 ⇒ 把一个数当结论就是"
                    "「高估 3 条 + 低估 2 条」互相抵消。相减成单一数字属禁用（见 capability_roster_scope）"),
                # ⚠️ 判据文件的**名字不写扩展名**：否则会被 `scripts/citation_check.py` 当成
                # "纯文件提及"的**引用**，从而要求登记（而 `scripts/**` 不在本项写域）。
                "criterion": "test_four_face_alignment_20261007（tests/unit 下；此处不写扩展名）",
            },
            "non_capability_names": {
                ch: self.NON_CAPABILITY_NAMES[ch]
                for ch in list(self._health) + ["aggregator"]
                if ch in self.NON_CAPABILITY_NAMES
            },
        }

    def reset(self):
        for k in self._health:
            self._health[k] = True
        self._tier = ServiceTier.FULL
        self._failure_counts.clear()
        self._degradation_history.clear()
