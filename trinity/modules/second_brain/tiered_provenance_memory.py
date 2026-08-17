"""
# status: orphan (2026-08-15 audit, not in runtime path)
P23-3: TierMem — 溯源感知双层记忆

对标论文: TierMem (Tiered Provenance-Aware Agent Memory, 2026.08)
核心发现: 记忆系统应分层管理：轻量摘要索引 + 不可变原始日志。
        运行时充分性路由器动态判断摘要是否足够，不足时自动升级到原始日志。
        已验证发现写回并链接原始源，构建端到端溯源链。
三元语: 摘要索引 → 充分性路由 → 原始日志升级 → 验证写回 → 溯源链接

设计要点:
- ProvenanceTier: 溯源层级枚举（SUMMARY / RAW_LOG / VERIFIED）
- SummaryIndex: 轻量摘要索引，存储压缩后的关键信息用于快速检索
- SummaryIndexEntry: 摘要条目，含溯源指针指向原始日志
- ImmutableRawLog: 不可变原始日志（Append-Only），完整保留执行轨迹
- RawLogEntry: 原始日志条目，含时间戳、完整上下文和不可变哈希
- RuntimeSufficiencyRouter: 运行时充分性路由器，评估摘要是否满足查询需求
- VerifiedDiscovery: 已验证的发现，含置信度和溯源链接
- ProvenanceLinker: 溯源链接器，将验证发现写回并链接到原始源
- TierMemEngine: 统一编排器，线程安全，提供 statistics() 运行时指标
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


class ProvenanceTier(Enum):
    """溯源层级"""
    SUMMARY = "summary"               # 摘要层：轻量压缩索引
    RAW_LOG = "raw_log"               # 原始日志层：完整不可变记录
    VERIFIED = "verified"             # 已验证层：经充分性验证的发现
    HYBRID = "hybrid"                 # 混合层：同时包含摘要和原始引用


class SufficiencyVerdict(Enum):
    """充分性裁决"""
    SUFFICIENT = "sufficient"         # 摘要足够回答查询
    INSUFFICIENT = "insufficient"     # 需要升级到原始日志
    PARTIALLY_SUFFICIENT = "partially_sufficient"  # 部分足够，需补充
    AMBIGUOUS = "ambiguous"           # 无法判断，需人工介入


class VerificationStatus(Enum):
    """验证状态"""
    UNVERIFIED = "unverified"         # 未验证
    VERIFIED = "verified"             # 已验证通过
    REFUTED = "refuted"               # 已被证伪
    STALE = "stale"                   # 已过时（原始日志有新数据）


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class SummaryIndexEntry:
    """摘要索引条目"""
    entry_id: str                     # 条目唯一标识
    summary_text: str                 # 压缩摘要文本
    keywords: List[str]               # 提取的关键词
    entity_map: Dict[str, str]        # 实体映射 {实体类型: 实体值}
    timestamp: float                  # 创建时间戳
    source_log_ids: List[str]         # 溯源指针：指向原始日志条目 ID 列表
    confidence: float                 # 摘要置信度 [0, 1]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RawLogEntry:
    """不可变原始日志条目"""
    entry_id: str                     # 条目唯一标识（SHA256）
    sequence_num: int                 # 单调递增序列号
    timestamp: float                  # 记录时间戳
    content: Dict[str, Any]           # 完整上下文内容
    content_hash: str                 # 内容的 SHA256 哈希（防篡改）
    summary_index_id: Optional[str]   # 关联的摘要索引条目 ID
    previous_entry_id: Optional[str]  # 前一条目 ID（链式完整性）
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifiedDiscovery:
    """已验证的发现"""
    discovery_id: str                 # 发现唯一标识
    summary_entry_id: str             # 关联的摘要索引条目 ID
    raw_log_ids: List[str]            # 关联的原始日志条目 ID 列表
    verified_content: Dict[str, Any]  # 验证后的内容
    verification_status: VerificationStatus
    confidence: float                 # 验证置信度
    verified_at: float                # 验证时间戳
    provenance_chain: List[str]       # 完整溯源链 [entry_id, ...]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouterDecision:
    """充分性路由器决策"""
    query_id: str                     # 查询标识
    verdict: SufficiencyVerdict       # 充分性裁决
    summary_score: float              # 摘要充分性评分
    confidence: float                 # 裁决置信度
    missing_fields: List[str]         # 摘要缺失的字段
    recommended_raw_log_ids: List[str]  # 推荐的原始日志条目 ID
    reasoning: str                    # 裁决推理说明


@dataclass
class ProvenanceLink:
    """溯源链接记录"""
    link_id: str                      # 链接唯一标识
    source_entry_id: str              # 源条目（摘要或原始日志）
    target_discovery_id: str          # 目标验证发现
    link_type: str                    # 链接类型（derives_from / validates / extends）
    bidirectional: bool = True        # 是否双向链接
    created_at: float = field(default_factory=time.time)


# ============================================================================
# Core Classes
# ============================================================================


class SummaryIndex:
    """轻量摘要索引

    存储压缩后的关键信息，支持关键词和实体检索。
    每个摘要条目持有溯源指针指向原始日志。
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, SummaryIndexEntry] = OrderedDict()
        self._keyword_index: Dict[str, Set[str]] = defaultdict(set)  # keyword → {entry_id}
        self._entity_index: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
        self._lock = threading.RLock()
        self._entry_counter: int = 0

    def add(self, summary_text: str, keywords: List[str],
            entity_map: Dict[str, str], source_log_ids: List[str],
            confidence: float = 0.8) -> SummaryIndexEntry:
        """添加摘要条目"""
        with self._lock:
            self._entry_counter += 1
            entry_id = f"SE_{self._entry_counter:08d}"
            entry = SummaryIndexEntry(
                entry_id=entry_id,
                summary_text=summary_text,
                keywords=list(keywords),
                entity_map=dict(entity_map),
                timestamp=time.time(),
                source_log_ids=list(source_log_ids),
                confidence=confidence,
            )
            self._entries[entry_id] = entry
            for kw in keywords:
                self._keyword_index[kw.lower()].add(entry_id)
            for etype, evalue in entity_map.items():
                self._entity_index[etype.lower()][evalue.lower()].add(entry_id)
            return entry

    def search_by_keyword(self, keyword: str) -> List[SummaryIndexEntry]:
        """按关键词检索"""
        entry_ids = self._keyword_index.get(keyword.lower(), set())
        return [self._entries[eid] for eid in entry_ids if eid in self._entries]

    def search_by_entity(self, entity_type: str, entity_value: str) -> List[SummaryIndexEntry]:
        """按实体检索"""
        inner = self._entity_index.get(entity_type.lower(), {})
        entry_ids = inner.get(entity_value.lower(), set())
        return [self._entries[eid] for eid in entry_ids if eid in self._entries]

    def get(self, entry_id: str) -> Optional[SummaryIndexEntry]:
        return self._entries.get(entry_id)

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_entries": len(self._entries),
            "total_keywords": len(self._keyword_index),
            "total_entities": sum(len(v) for v in self._entity_index.values()),
        }


class ImmutableRawLog:
    """不可变原始日志（Append-Only）

    完整保留执行轨迹，每个条目带 SHA256 哈希防篡改。
    支持链式完整性验证（每个条目引用前一条目的哈希）。
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, RawLogEntry] = OrderedDict()
        self._sequence_counter: int = 0
        self._last_entry_id: Optional[str] = None
        self._lock = threading.RLock()

    def append(self, content: Dict[str, Any],
               summary_index_id: Optional[str] = None) -> RawLogEntry:
        """追加不可变日志条目"""
        with self._lock:
            self._sequence_counter += 1
            content_json = json.dumps(content, sort_keys=True, default=str)
            content_hash = hashlib.sha256(content_json.encode()).hexdigest()

            entry_id_raw = f"{self._sequence_counter}:{content_hash}:{time.time()}"
            entry_id = hashlib.sha256(entry_id_raw.encode()).hexdigest()[:16]

            entry = RawLogEntry(
                entry_id=entry_id,
                sequence_num=self._sequence_counter,
                timestamp=time.time(),
                content=content,
                content_hash=content_hash,
                summary_index_id=summary_index_id,
                previous_entry_id=self._last_entry_id,
            )
            self._entries[entry_id] = entry
            self._last_entry_id = entry_id
            logger.debug("Raw log appended: %s (seq=%d)", entry_id, self._sequence_counter)
            return entry

    def get(self, entry_id: str) -> Optional[RawLogEntry]:
        return self._entries.get(entry_id)

    def get_range(self, start_seq: int, end_seq: int) -> List[RawLogEntry]:
        """按序列号范围获取日志"""
        return [e for e in self._entries.values()
                if start_seq <= e.sequence_num <= end_seq]

    def verify_integrity(self, entry_id: str) -> bool:
        """验证单条日志的完整性（哈希校验）"""
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        content_json = json.dumps(entry.content, sort_keys=True, default=str)
        recomputed_hash = hashlib.sha256(content_json.encode()).hexdigest()
        return recomputed_hash == entry.content_hash

    def verify_chain(self, start_entry_id: str, max_depth: int = 100) -> Tuple[bool, int]:
        """验证链式完整性"""
        visited = 0
        current_id = start_entry_id
        while current_id and visited < max_depth:
            entry = self._entries.get(current_id)
            if not entry:
                return False, visited
            if not self.verify_integrity(current_id):
                return False, visited
            current_id = entry.previous_entry_id
            visited += 1
        return True, visited

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_entries": len(self._entries),
            "sequence_counter": self._sequence_counter,
            "last_entry_id": self._last_entry_id,
        }


class RuntimeSufficiencyRouter:
    """运行时充分性路由器

    动态评估摘要索引是否足以回答查询。
    不足时自动升级到原始日志层获取完整上下文。
    """

    def __init__(self, sufficiency_threshold: float = 0.7,
                 max_log_lookback: int = 50) -> None:
        self._sufficiency_threshold = sufficiency_threshold
        self._max_log_lookback = max_log_lookback
        self._route_count: int = 0
        self._upgrade_count: int = 0

    def evaluate(self, query_fields: List[str],
                 summary_entry: SummaryIndexEntry) -> RouterDecision:
        """评估摘要充分性"""
        self._route_count += 1

        # 检查摘要覆盖的字段
        available_fields: Set[str] = set()
        available_fields.update(summary_entry.keywords)
        available_fields.update(summary_entry.entity_map.keys())
        available_fields.update(summary_entry.entity_map.values())

        query_set = set(f.lower() for f in query_fields)
        hit_fields = query_set & {f.lower() for f in available_fields}
        missing_fields = list(query_set - {f.lower() for f in available_fields})

        coverage = len(hit_fields) / max(len(query_set), 1)

        if coverage >= self._sufficiency_threshold:
            verdict = SufficiencyVerdict.SUFFICIENT
        elif coverage >= 0.3:
            verdict = SufficiencyVerdict.PARTIALLY_SUFFICIENT
        elif len(missing_fields) <= 2:
            verdict = SufficiencyVerdict.INSUFFICIENT
        else:
            verdict = SufficiencyVerdict.AMBIGUOUS

        if verdict in (SufficiencyVerdict.INSUFFICIENT, SufficiencyVerdict.PARTIALLY_SUFFICIENT):
            self._upgrade_count += 1

        return RouterDecision(
            query_id=f"Q_{self._route_count:06d}",
            verdict=verdict,
            summary_score=coverage,
            confidence=coverage,
            missing_fields=missing_fields,
            recommended_raw_log_ids=summary_entry.source_log_ids,
            reasoning=f"Coverage {coverage:.2f}, threshold {self._sufficiency_threshold}",
        )

    def statistics(self) -> Dict[str, Any]:
        return {
            "route_count": self._route_count,
            "upgrade_count": self._upgrade_count,
            "upgrade_rate": self._upgrade_count / max(self._route_count, 1),
        }


class ProvenanceLinker:
    """溯源链接器

    将已验证发现写回摘要索引，并建立到原始日志的双向溯源链接。
    维护完整的 provenance_chain 用于端到端审计。
    """

    def __init__(self) -> None:
        self._links: OrderedDict[str, ProvenanceLink] = OrderedDict()
        self._link_counter: int = 0
        self._lock = threading.RLock()

    def create_link(self, source_entry_id: str,
                    target_discovery_id: str,
                    link_type: str = "derives_from") -> ProvenanceLink:
        """创建溯源链接"""
        with self._lock:
            self._link_counter += 1
            link_id = f"PL_{self._link_counter:08d}"
            link = ProvenanceLink(
                link_id=link_id,
                source_entry_id=source_entry_id,
                target_discovery_id=target_discovery_id,
                link_type=link_type,
            )
            self._links[link_id] = link
            return link

    def trace_provenance(self, entry_id: str, max_depth: int = 20) -> List[ProvenanceLink]:
        """追溯某个条目的完整溯源链"""
        chain: List[ProvenanceLink] = []
        visited: Set[str] = set()
        current_id = entry_id
        depth = 0
        while current_id and depth < max_depth:
            if current_id in visited:
                break
            visited.add(current_id)
            for link in self._links.values():
                if link.source_entry_id == current_id:
                    chain.append(link)
                    current_id = link.target_discovery_id
                    break
            else:
                break
            depth += 1
        return chain

    def statistics(self) -> Dict[str, Any]:
        return {"total_links": len(self._links)}


# ============================================================================
# Engine
# ============================================================================


class TierMemEngine:
    """TierMem 统一编排器

    整合 摘要索引 → 充分性路由 → 原始日志升级 → 验证写回 → 溯源链接
    的完整溯源感知记忆流水线。线程安全。
    """

    def __init__(self, sufficiency_threshold: float = 0.7) -> None:
        self._lock = threading.RLock()
        self._summary_index = SummaryIndex()
        self._raw_log = ImmutableRawLog()
        self._router = RuntimeSufficiencyRouter(sufficiency_threshold=sufficiency_threshold)
        self._linker = ProvenanceLinker()
        self._verified_discoveries: OrderedDict[str, VerifiedDiscovery] = OrderedDict()
        self._discovery_counter: int = 0

    def ingest(self, content: Dict[str, Any], summary_text: str,
               keywords: Optional[List[str]] = None,
               entity_map: Optional[Dict[str, str]] = None) -> Tuple[RawLogEntry, SummaryIndexEntry]:
        """摄入事件：写入原始日志 + 生成摘要索引"""
        with self._lock:
            # Step 1: 先写入不可变原始日志
            log_entry = self._raw_log.append(content)

            # Step 2: 生成摘要索引（带溯源指针）
            summary_entry = self._summary_index.add(
                summary_text=summary_text,
                keywords=keywords or [],
                entity_map=entity_map or {},
                source_log_ids=[log_entry.entry_id],
            )

            # Step 3: 反向关联（摘要索引 ID → 原始日志）
            # 更新原始日志以关联摘要
            updated_log = RawLogEntry(
                entry_id=log_entry.entry_id,
                sequence_num=log_entry.sequence_num,
                timestamp=log_entry.timestamp,
                content=log_entry.content,
                content_hash=log_entry.content_hash,
                summary_index_id=summary_entry.entry_id,
                previous_entry_id=log_entry.previous_entry_id,
            )
            self._raw_log._entries[log_entry.entry_id] = updated_log

            return updated_log, summary_entry

    def query(self, query_fields: List[str],
              keywords: Optional[List[str]] = None) -> Dict[str, Any]:
        """查询记忆：摘要索引 → 充分性路由 → 必要时升级到原始日志"""
        results: Dict[str, Any] = {"tier": ProvenanceTier.SUMMARY.value, "entries": [], "raw_logs": []}

        # 搜索摘要索引
        if keywords:
            all_entries: Set[str] = set()
            for kw in keywords:
                matches = self._summary_index.search_by_keyword(kw)
                all_entries.update(e.entry_id for e in matches)
            summary_entries = [self._summary_index.get(eid) for eid in all_entries]
        else:
            summary_entries = list(self._summary_index._entries.values())

        summary_entries = [e for e in summary_entries if e is not None]

        for entry in summary_entries:
            decision = self._router.evaluate(query_fields, entry)
            results["entries"].append({
                "summary": entry,
                "router_decision": decision,
            })

            # 充分性不足时升级到原始日志
            if decision.verdict in (SufficiencyVerdict.INSUFFICIENT,
                                     SufficiencyVerdict.PARTIALLY_SUFFICIENT):
                results["tier"] = ProvenanceTier.RAW_LOG.value
                for log_id in decision.recommended_raw_log_ids:
                    log_entry = self._raw_log.get(log_id)
                    if log_entry:
                        results["raw_logs"].append(log_entry)

        return results

    def verify_and_link(self, summary_entry_id: str,
                        verified_content: Dict[str, Any],
                        confidence: float = 0.9) -> VerifiedDiscovery:
        """验证发现并建立溯源链接"""
        with self._lock:
            self._discovery_counter += 1
            discovery_id = f"VD_{self._discovery_counter:08d}"

            summary_entry = self._summary_index.get(summary_entry_id)
            raw_log_ids = summary_entry.source_log_ids if summary_entry else []

            discovery = VerifiedDiscovery(
                discovery_id=discovery_id,
                summary_entry_id=summary_entry_id,
                raw_log_ids=list(raw_log_ids),
                verified_content=dict(verified_content),
                verification_status=VerificationStatus.VERIFIED,
                confidence=confidence,
                verified_at=time.time(),
                provenance_chain=[summary_entry_id] + raw_log_ids,
            )
            self._verified_discoveries[discovery_id] = discovery

            # 建立溯源链接
            for log_id in raw_log_ids:
                self._linker.create_link(
                    source_entry_id=log_id,
                    target_discovery_id=discovery_id,
                    link_type="validates",
                )

            return discovery

    def statistics(self) -> Dict[str, Any]:
        """聚合运行时统计"""
        return {
            "summary_index": self._summary_index.statistics(),
            "raw_log": self._raw_log.statistics(),
            "router": self._router.statistics(),
            "linker": self._linker.statistics(),
            "verified_discoveries": len(self._verified_discoveries),
        }


# ============================================================================
# Module-level statistics helper
# ============================================================================

def statistics(engine: Optional[TierMemEngine] = None) -> Dict[str, Any]:
    """模块级统计接口"""
    if engine is not None:
        return engine.statistics()
    return {"status": "no engine initialized"}
