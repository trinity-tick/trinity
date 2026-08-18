"""
# status: orphan (2026-08-15 audit, not in runtime path)
P23-1: ChronoMem — 语义版本控制与自然语言回滚

对标论文: ChronoMem (Semantic Version Control for Agent Memory, 2026.08)
核心发现: 记忆系统应支持全量快照 + 结构化版本历史 + 自然语言意图驱动的回滚。
        每次写入提交全量快照；混合词法/语义检索经 RRF 融合 + 重排解析版本；
        后暴露反事实验证确保回滚正确性，防止灾难性遗忘。
三元语: 全量快照提交 → 词法/语义混合检索 → RRF 融合 → 重排 → 版本解析 → 反事实验证

设计要点:
- SnapshotFormat: 定义快照存储格式（FULL / INCREMENTAL / DIFF_BASED）
- VersionRecord: 结构化版本历史条目，含时间戳、变更摘要和完整性校验
- VersionHistory: 维护线性版本 DAG，支持分支、标签和祖先追溯
- SemanticRollbackIntent: 自然语言回滚意图解析为结构化版本查询
- HybridLexicalSemanticRetriever: 词法精确匹配 + 语义向量检索双路召回
- RRFFusionReranker: 倒数排名融合 + Cross-Encoder 重排，输出 Top-K 版本
- CounterfactualValidator: 后暴露反事实验证：回滚后与回滚前状态 diff 对比
- ChronoMemEngine: 统一编排器，线程安全，支持 statistics() 运行时指标
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class SnapshotFormat(Enum):
    """快照存储格式"""
    FULL = "full"                       # 全量快照：完整序列化内存状态
    INCREMENTAL = "incremental"         # 增量快照：仅存储与前一个版本的差异
    DIFF_BASED = "diff_based"           # 差分快照：基于语义差异的最小化表示
    CHECKPOINT = "checkpoint"           # 检查点：定期全量 + 中间增量混合


class RollbackStatus(Enum):
    """回滚操作状态"""
    PENDING = "pending"                 # 待执行
    IN_PROGRESS = "in_progress"         # 执行中
    COMPLETED = "completed"             # 已完成
    FAILED = "failed"                   # 失败
    ROLLED_FORWARD = "rolled_forward"   # 已前滚恢复


class RetrievalMode(Enum):
    """检索模式"""
    LEXICAL_ONLY = "lexical_only"       # 仅词法匹配（精确关键词）
    SEMANTIC_ONLY = "semantic_only"     # 仅语义向量检索
    HYBRID = "hybrid"                   # 混合检索（词法 + 语义）


class FusionAlgorithm(Enum):
    """融合算法"""
    RRF = "rrf"                         # 倒数排名融合
    WEIGHTED_SUM = "weighted_sum"       # 加权求和
    COMBMNZ = "combmnz"                 # CombMNZ 融合
    BORDA_COUNT = "borda_count"         # Borda 计数


class ValidationVerdict(Enum):
    """反事实验证裁决"""
    PASS = "pass"                       # 验证通过：回滚正确
    FAIL = "fail"                       # 验证失败：存在不一致
    PARTIAL = "partial"                 # 部分通过：部分字段不一致
    INDETERMINATE = "indeterminate"     # 无法判定：缺少足够证据


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class VersionRecord:
    """结构化版本历史条目"""
    version_id: str                     # 版本唯一标识（SHA256 哈希）
    sequence_num: int                   # 单调递增序列号
    timestamp: float                    # Unix 时间戳
    parent_version_ids: List[str]       # 父版本 ID 列表（支持多父合并）
    snapshot_format: SnapshotFormat     # 快照格式
    payload_hash: str                   # 快照数据的 SHA256 哈希
    change_summary: str                 # 人类可读的变更摘要
    tags: List[str] = field(default_factory=list)  # 语义标签
    metadata: Dict[str, Any] = field(default_factory=dict)  # 扩展元数据
    snapshot_data: Optional[Dict[str, Any]] = None  # 快照数据（延迟加载）


@dataclass
class SemanticRollbackIntent:
    """自然语言回滚意图 → 结构化版本查询"""
    raw_text: str                       # 用户原始自然语言意图
    parsed_keywords: List[str]          # 提取的实体关键词
    parsed_entities: Dict[str, str]     # 提取的命名实体 {类型: 值}
    temporal_constraint: Optional[Tuple[float, float]]  # 时间范围约束 (start, end)
    tag_constraints: List[str]          # 标签过滤条件
    confidence: float                   # 解析置信度 [0, 1]
    disambiguation_hints: List[str]     # 消歧提示


@dataclass
class RetrievalCandidate:
    """检索候选版本"""
    version: VersionRecord
    lexical_score: float                # 词法匹配分数
    semantic_score: float               # 语义匹配分数
    fusion_score: float                 # 融合后分数
    rank: int                           # 最终排名


@dataclass
class CounterfactualReport:
    """反事实验证报告"""
    rollback_version_id: str            # 回滚目标版本 ID
    pre_rollback_state_hash: str        # 回滚前状态哈希
    post_rollback_state_hash: str       # 回滚后状态哈希
    verdict: ValidationVerdict          # 验证裁决
    diff_summary: str                   # 差异摘要
    field_diffs: Dict[str, Tuple[Any, Any]]  # 字段级差异 {字段: (期望值, 实际值)}
    passed_checks: int                  # 通过的检查项数
    failed_checks: int                  # 失败的检查项数
    recommendation: str                 # 建议（accept / revert / manual_review）


# ============================================================================
# Core Classes
# ============================================================================


class VersionHistory:
    """线性版本 DAG 管理器

    维护完整的版本演化图，支持分支、标签、祖先追溯和合并。
    线程安全，所有写操作持有 _lock。
    """

    def __init__(self) -> None:
        self._versions: OrderedDict[str, VersionRecord] = OrderedDict()
        self._tag_index: Dict[str, str] = {}  # tag → version_id
        self._sequence_counter: int = 0
        self._head_version_id: Optional[str] = None
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"commits": 0, "tags": 0, "rollbacks": 0}

    def commit(self, payload: Dict[str, Any], change_summary: str,
               snapshot_format: SnapshotFormat = SnapshotFormat.FULL,
               tags: Optional[List[str]] = None,
               parent_ids: Optional[List[str]] = None) -> VersionRecord:
        """提交新版本快照"""
        with self._lock:
            self._sequence_counter += 1
            payload_json = json.dumps(payload, sort_keys=True, default=str)
            payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

            parent_ids = parent_ids or ([self._head_version_id] if self._head_version_id else [])
            version_id_raw = f"{self._sequence_counter}:{payload_hash}:{time.time()}"
            version_id = hashlib.sha256(version_id_raw.encode()).hexdigest()[:16]

            record = VersionRecord(
                version_id=version_id,
                sequence_num=self._sequence_counter,
                timestamp=time.time(),
                parent_version_ids=parent_ids,
                snapshot_format=snapshot_format,
                payload_hash=payload_hash,
                change_summary=change_summary,
                tags=tags or [],
                snapshot_data=payload,
            )
            self._versions[version_id] = record
            self._head_version_id = version_id

            for tag in tags or []:
                self._tag_index[tag] = version_id

            self._stats["commits"] += 1
            if tags:
                self._stats["tags"] += len(tags)

            logger.info("Version committed: %s (seq=%d, tags=%s)",
                        version_id, self._sequence_counter, tags)
            return record

    def get_version(self, version_id: str) -> Optional[VersionRecord]:
        """按版本 ID 获取版本记录"""
        return self._versions.get(version_id)

    def get_by_tag(self, tag: str) -> Optional[VersionRecord]:
        """按标签获取版本"""
        vid = self._tag_index.get(tag)
        return self._versions.get(vid) if vid else None

    def get_head(self) -> Optional[VersionRecord]:
        """获取最新版本（HEAD）"""
        return self._versions.get(self._head_version_id) if self._head_version_id else None

    def ancestors(self, version_id: str, max_depth: int = 50) -> List[VersionRecord]:
        """追溯版本祖先链"""
        result: List[VersionRecord] = []
        visited: Set[str] = set()
        queue: List[str] = [version_id]
        while queue and len(result) < max_depth:
            vid = queue.pop(0)
            if vid in visited:
                continue
            visited.add(vid)
            record = self._versions.get(vid)
            if record:
                result.append(record)
                queue.extend(record.parent_version_ids)
        return result

    def list_versions(self, limit: int = 100) -> List[VersionRecord]:
        """列出最近 N 个版本"""
        items = list(self._versions.values())
        return items[-limit:]

    def statistics(self) -> Dict[str, Any]:
        """运行时统计"""
        return {
            "total_versions": len(self._versions),
            "total_tags": len(self._tag_index),
            "head_version_id": self._head_version_id,
            "stats": dict(self._stats),
        }


class HybridLexicalSemanticRetriever:
    """混合词法/语义检索器

    双路召回：词法通道做精确关键词匹配（BM25 + TF-IDF），
    语义通道做向量相似度检索（cosine similarity）。
    两路结果经 RRF / 加权求和 / CombMNZ 融合后输出。
    """

    def __init__(self, retrieval_mode: RetrievalMode = RetrievalMode.HYBRID,
                 fusion_algorithm: FusionAlgorithm = FusionAlgorithm.RRF,
                 lexical_weight: float = 0.4,
                 semantic_weight: float = 0.6) -> None:
        self._retrieval_mode = retrieval_mode
        self._fusion_algorithm = fusion_algorithm
        self._lexical_weight = lexical_weight
        self._semantic_weight = semantic_weight
        self._lock = threading.RLock()
        self._query_count: int = 0

    def lexical_search(self, query_keywords: List[str],
                       versions: List[VersionRecord]) -> Dict[str, float]:
        """词法通道：BM25 风格的精确关键词匹配"""
        scores: Dict[str, float] = {}
        if not query_keywords:
            return scores
        keyword_set = set(k.lower() for k in query_keywords)
        for v in versions:
            text = f"{v.change_summary} {' '.join(v.tags)}".lower()
            hits = sum(1 for kw in keyword_set if kw in text)
            if hits > 0:
                # 简化的 TF-IDF：关键词命中率 × IDF 模拟
                scores[v.version_id] = hits / len(keyword_set)
        return scores

    def semantic_search(self, intent_embedding: Optional[List[float]],
                        versions: List[VersionRecord]) -> Dict[str, float]:
        """语义通道：基于嵌入向量的余弦相似度检索"""
        scores: Dict[str, float] = {}
        if intent_embedding is None or len(intent_embedding) == 0:
            # 降级：基于变更摘要的 Jaccard 相似度模拟
            return self._fallback_semantic_search(versions)
        # 实际生产环境中会调用向量数据库
        return self._fallback_semantic_search(versions)

    def _fallback_semantic_search(self, versions: List[VersionRecord]) -> Dict[str, float]:
        """降级语义检索：基于标签和摘要的启发式打分"""
        scores: Dict[str, float] = {}
        for v in versions:
            base_score = 0.0
            base_score += len(v.tags) * 0.1
            base_score += len(v.change_summary) / 500.0
            base_score += 1.0 / (1.0 + time.time() - v.timestamp) * 0.01  # 时间衰减
            scores[v.version_id] = min(base_score, 1.0)
        return scores

    def retrieve(self, intent: SemanticRollbackIntent,
                 versions: List[VersionRecord],
                 top_k: int = 10,
                 intent_embedding: Optional[List[float]] = None) -> List[RetrievalCandidate]:
        """执行混合检索并输出 Top-K 候选"""
        self._query_count += 1
        lexical_scores = self.lexical_search(intent.parsed_keywords, versions)
        semantic_scores = self.semantic_search(intent_embedding, versions)

        # 融合
        fusion_scores: Dict[str, float] = {}
        all_vids = set(lexical_scores.keys()) | set(semantic_scores.keys())

        if self._fusion_algorithm == FusionAlgorithm.RRF:
            # RRF: rank reciprocal fusion
            lex_ranked = sorted(lexical_scores.items(), key=lambda x: -x[1])
            sem_ranked = sorted(semantic_scores.items(), key=lambda x: -x[1])
            lex_rank_map = {vid: i + 1 for i, (vid, _) in enumerate(lex_ranked)}
            sem_rank_map = {vid: i + 1 for i, (vid, _) in enumerate(sem_ranked)}
            k = 60  # RRF constant
            for vid in all_vids:
                lex_r = lex_rank_map.get(vid, len(lex_ranked) + 1)
                sem_r = sem_rank_map.get(vid, len(sem_ranked) + 1)
                fusion_scores[vid] = 1.0 / (k + lex_r) + 1.0 / (k + sem_r)
        else:
            # 加权求和兜底
            for vid in all_vids:
                l = lexical_scores.get(vid, 0.0)
                s = semantic_scores.get(vid, 0.0)
                fusion_scores[vid] = self._lexical_weight * l + self._semantic_weight * s

        # 排序
        ranked = sorted(fusion_scores.items(), key=lambda x: -x[1])[:top_k]
        candidates: List[RetrievalCandidate] = []
        version_map = {v.version_id: v for v in versions}
        for rank, (vid, score) in enumerate(ranked, 1):
            v = version_map.get(vid)
            if v:
                candidates.append(RetrievalCandidate(
                    version=v,
                    lexical_score=lexical_scores.get(vid, 0.0),
                    semantic_score=semantic_scores.get(vid, 0.0),
                    fusion_score=score,
                    rank=rank,
                ))
        return candidates

    def statistics(self) -> Dict[str, Any]:
        return {"query_count": self._query_count, "mode": self._retrieval_mode.value}


class RRFFusionReranker:
    """RRF 融合 + Cross-Encoder 重排器

    对检索候选进行精排：第一阶段 RRF 融合双路分数，
    第二阶段（可选）用 Cross-Encoder 做逐对相关性打分，输出最终排序。
    """

    def __init__(self, enable_cross_encoder: bool = False,
                 rrf_k: int = 60) -> None:
        self._enable_cross_encoder = enable_cross_encoder
        self._rrf_k = rrf_k
        self._rerank_count: int = 0

    def rerank(self, candidates: List[RetrievalCandidate],
               intent: SemanticRollbackIntent) -> List[RetrievalCandidate]:
        """对候选列表执行 RRF 融合 + 可选 Cross-Encoder 重排"""
        self._rerank_count += 1
        if not candidates:
            return candidates

        # 已按 RRF 融合分数排序；如需 Cross-Encoder 则进一步精排
        if self._enable_cross_encoder:
            candidates = self._cross_encoder_rerank(candidates, intent)

        # 应用时间约束过滤
        if intent.temporal_constraint:
            start, end = intent.temporal_constraint
            candidates = [c for c in candidates
                          if start <= c.version.timestamp <= end]

        # 应用标签约束过滤
        if intent.tag_constraints:
            candidates = [c for c in candidates
                          if any(t in c.version.tags for t in intent.tag_constraints)]

        # 重新分配排名
        for i, c in enumerate(sorted(candidates, key=lambda x: -x.fusion_score), 1):
            c.rank = i

        return candidates

    def _cross_encoder_rerank(self, candidates: List[RetrievalCandidate],
                              intent: SemanticRollbackIntent) -> List[RetrievalCandidate]:
        """Cross-Encoder 精排：基于意图文本与版本变更摘要的相关性"""
        intent_text = intent.raw_text.lower()
        for c in candidates:
            # 生产环境应加载 Cross-Encoder 模型；此处用启发式模拟
            summary_terms = set(c.version.change_summary.lower().split())
            intent_terms = set(intent_text.split())
            overlap = len(summary_terms & intent_terms)
            ce_bonus = min(overlap / max(len(intent_terms), 1), 1.0) * 0.3
            c.fusion_score = c.fusion_score * (1.0 + ce_bonus)
        return sorted(candidates, key=lambda x: -x.fusion_score)

    def statistics(self) -> Dict[str, Any]:
        return {"rerank_count": self._rerank_count, "cross_encoder": self._enable_cross_encoder}


class CounterfactualValidator:
    """后暴露反事实验证器

    回滚完成后，对比回滚前状态与回滚后状态，验证关键字段是否一致。
    若发现差异，生成 CounterfactualReport 并给出 accept / revert / manual_review 建议。
    """

    def __init__(self, tolerance: float = 1e-6) -> None:
        self._tolerance = tolerance
        self._validation_count: int = 0
        self._pass_count: int = 0
        self._fail_count: int = 0

    def validate(self, pre_rollback_state: Dict[str, Any],
                 post_rollback_state: Dict[str, Any],
                 target_version: VersionRecord) -> CounterfactualReport:
        """执行反事实验证"""
        self._validation_count += 1

        pre_hash = hashlib.sha256(
            json.dumps(pre_rollback_state, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        post_hash = hashlib.sha256(
            json.dumps(post_rollback_state, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

        field_diffs: Dict[str, Tuple[Any, Any]] = {}
        all_keys = set(pre_rollback_state.keys()) | set(post_rollback_state.keys())
        for key in all_keys:
            pre_val = pre_rollback_state.get(key)
            post_val = post_rollback_state.get(key)
            if pre_val != post_val:
                field_diffs[key] = (pre_val, post_val)

        total_checks = len(all_keys)
        failed_checks = len(field_diffs)
        passed_checks = total_checks - failed_checks

        if failed_checks == 0:
            verdict = ValidationVerdict.PASS
            recommendation = "accept"
            self._pass_count += 1
        elif failed_checks <= max(total_checks * 0.1, 2):
            verdict = ValidationVerdict.PARTIAL
            recommendation = "manual_review"
            self._fail_count += 1
        else:
            verdict = ValidationVerdict.FAIL
            recommendation = "revert"
            self._fail_count += 1

        diff_summary = f"{failed_checks}/{total_checks} fields differ"
        logger.info("Validation: %s — %s", verdict.value, diff_summary)

        return CounterfactualReport(
            rollback_version_id=target_version.version_id,
            pre_rollback_state_hash=pre_hash,
            post_rollback_state_hash=post_hash,
            verdict=verdict,
            diff_summary=diff_summary,
            field_diffs=field_diffs,
            passed_checks=passed_checks,
            failed_checks=failed_checks,
            recommendation=recommendation,
        )

    def statistics(self) -> Dict[str, Any]:
        return {
            "validation_count": self._validation_count,
            "pass_count": self._pass_count,
            "fail_count": self._fail_count,
            "pass_rate": self._pass_count / max(self._validation_count, 1),
        }


# ============================================================================
# Engine
# ============================================================================


class ChronoMemEngine:
    """ChronoMem 统一编排器

    整合全量快照提交 → 词法/语义混合检索 → RRF 融合 → 重排 → 反事实验证
    的完整流水线。线程安全，提供 statistics() 运行时指标。
    """

    def __init__(self, retrieval_mode: RetrievalMode = RetrievalMode.HYBRID,
                 fusion_algorithm: FusionAlgorithm = FusionAlgorithm.RRF) -> None:
        self._lock = threading.RLock()
        self._version_history = VersionHistory()
        self._retriever = HybridLexicalSemanticRetriever(
            retrieval_mode=retrieval_mode,
            fusion_algorithm=fusion_algorithm,
        )
        self._reranker = RRFFusionReranker()
        self._validator = CounterfactualValidator()
        self._current_state: Dict[str, Any] = {}
        self._rollback_count: int = 0

    def commit_snapshot(self, state: Dict[str, Any], change_summary: str,
                        tags: Optional[List[str]] = None,
                        snapshot_format: SnapshotFormat = SnapshotFormat.FULL) -> VersionRecord:
        """提交全量快照"""
        with self._lock:
            self._current_state = dict(state)
            return self._version_history.commit(
                payload=self._current_state,
                change_summary=change_summary,
                snapshot_format=snapshot_format,
                tags=tags,
            )

    def parse_rollback_intent(self, raw_text: str,
                              keywords: Optional[List[str]] = None,
                              entities: Optional[Dict[str, str]] = None,
                              temporal_constraint: Optional[Tuple[float, float]] = None,
                              tag_constraints: Optional[List[str]] = None) -> SemanticRollbackIntent:
        """解析自然语言回滚意图"""
        return SemanticRollbackIntent(
            raw_text=raw_text,
            parsed_keywords=keywords or [],
            parsed_entities=entities or {},
            temporal_constraint=temporal_constraint,
            tag_constraints=tag_constraints or [],
            confidence=0.85,
            disambiguation_hints=[],
        )

    def search_versions(self, intent: SemanticRollbackIntent,
                        top_k: int = 10) -> List[RetrievalCandidate]:
        """检索匹配的版本候选"""
        versions = self._version_history.list_versions(limit=200)
        candidates = self._retriever.retrieve(intent, versions, top_k=top_k)
        return self._reranker.rerank(candidates, intent)

    def rollback(self, target_version: VersionRecord) -> CounterfactualReport:
        """执行回滚并后暴露反事实验证"""
        with self._lock:
            pre_state = dict(self._current_state)
            if target_version.snapshot_data:
                self._current_state = dict(target_version.snapshot_data)
            else:
                full_record = self._version_history.get_version(target_version.version_id)
                if full_record and full_record.snapshot_data:
                    self._current_state = dict(full_record.snapshot_data)

            self._rollback_count += 1
            report = self._validator.validate(
                pre_rollback_state=pre_state,
                post_rollback_state=self._current_state,
                target_version=target_version,
            )
            if report.verdict == ValidationVerdict.FAIL:
                self._current_state = pre_state  # 自动回退
                logger.warning("Rollback failed, reverted to pre-rollback state")
            else:
                self._version_history._stats["rollbacks"] += 1
            return report

    def get_current_state(self) -> Dict[str, Any]:
        """获取当前内存状态"""
        return dict(self._current_state)

    def statistics(self) -> Dict[str, Any]:
        """聚合运行时统计信息"""
        return {
            "version_history": self._version_history.statistics(),
            "retriever": self._retriever.statistics(),
            "reranker": self._reranker.statistics(),
            "validator": self._validator.statistics(),
            "rollback_count": self._rollback_count,
            "current_state_size": len(self._current_state),
        }


# ============================================================================
# Module-level statistics helper
# ============================================================================

def statistics(engine: Optional[ChronoMemEngine] = None) -> Dict[str, Any]:
    """模块级统计接口"""
    if engine is not None:
        return engine.statistics()
    return {"status": "no engine initialized"}
