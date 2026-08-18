"""
# status: orphan (2026-08-15 audit, not in runtime path)
P9-2: GDPR Compliance Governance Framework (对标 AEPD 2026)
============================================================

核心设计（基于 AEPD 2026 监管指南）：
  - 同意管理（ConsentManager）：按用户+数据类型+用途的三维授权矩阵
  - Article 17 擦除请求处理（ErasureEngine）：跨向量/图谱/元数据的全链路删除，含擦除证明
  - 数据主权控制（DataSovereignty）：按地域的数据驻留策略路由
  - 合规审计日志（ComplianceAuditor）：每次数据访问/删除/修改的可追溯记录
  - 与已有的 memory_unlearning.py 接口兼容（StorageSubstrate, ErasureStatus, ErasureProof）

设计要点：
  - 三维授权矩阵：(user_id × data_type × purpose) → 授权状态
  - Article 17 全链路擦除：向量索引 → 知识图谱 → 摘要缓存 → 备份快照
  - 地域驻留：EU/CN/US/BR 四大区域路由策略
  - 合规审计：不可变日志存储，每次操作的时间戳/操作者/数据/目的

Reference:
  - AEPD Regulatory Guidance on Agentic AI and GDPR (February 2026)
  - GDPR Articles 6, 7, 17, 30, 35
  - "AI Agent Memory and GDPR: How to Handle Persistent Context Without a Compliance Timebomb" (onlypiece.org, 2026)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

from trinity.modules.second_brain.memory_unlearning import (
    MemoryUnlearningManager,
    StorageSubstrate,
    ErasureStatus,
    ErasureProof,
    UnlearningResult,
)

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class ConsentStatus(Enum):
    """同意状态。"""
    GRANTED = "granted"           # 已授权
    DENIED = "denied"             # 已拒绝
    WITHDRAWN = "withdrawn"       # 已撤回
    EXPIRED = "expired"           # 已过期
    PENDING = "pending"           # 待确认
    NOT_APPLICABLE = "not_applicable"  # 不适用


class DataType(Enum):
    """数据类型分类（GDPR 语境）。"""
    PERSONAL_IDENTIFIER = "personal_identifier"     # 个人标识
    CONVERSATION_HISTORY = "conversation_history"   # 对话历史
    BEHAVIORAL_PROFILE = "behavioral_profile"       # 行为画像
    PREFERENCE_DATA = "preference_data"             # 偏好数据
    KNOWLEDGE_EXTRACTION = "knowledge_extraction"   # 知识提取
    VECTOR_EMBEDDING = "vector_embedding"           # 向量嵌入
    METADATA = "metadata"                           # 元数据
    ANALYTICS = "analytics"                         # 分析数据


class ProcessingPurpose(Enum):
    """处理目的（GDPR Article 6 法律基础）。"""
    SERVICE_DELIVERY = "service_delivery"           # 服务交付（合同履行）
    PERSONALIZATION = "personalization"             # 个性化（同意）
    ANALYTICS_IMPROVEMENT = "analytics_improvement"  # 分析改进（合法利益）
    RESEARCH = "research"                           # 研究（匿名化后）
    LEGAL_COMPLIANCE = "legal_compliance"           # 法律合规（法定义务）
    MARKETING = "marketing"                         # 营销（同意）
    VITAL_INTEREST = "vital_interest"               # 重大利益


class DataRegion(Enum):
    """数据驻留区域。"""
    EU_EEA = "eu_eea"               # 欧盟/欧洲经济区（GDPR）
    CHINA_MAINLAND = "china_mainland"  # 中国大陆（PIPL）
    UNITED_STATES = "united_states"    # 美国（CCPA/CPRA）
    BRAZIL = "brazil"                  # 巴西（LGPD）
    GLOBAL = "global"                  # 全球（无特殊限制）


class AuditActionType(Enum):
    """审计操作类型。"""
    DATA_ACCESS = "data_access"               # 数据访问
    DATA_DELETION = "data_deletion"           # 数据删除
    DATA_MODIFICATION = "data_modification"   # 数据修改
    CONSENT_CHANGE = "consent_change"         # 同意变更
    ERASURE_REQUEST = "erasure_request"       # 擦除请求
    DATA_EXPORT = "data_export"               # 数据导出
    ACCESS_REQUEST = "access_request"         # 访问请求（Article 15）
    RECTIFICATION_REQUEST = "rectification_request"  # 修正请求（Article 16）
    RESTRICTION_REQUEST = "restriction_request"      # 限制请求（Article 18）
    PORTABILITY_REQUEST = "portability_request"      # 可移植请求（Article 20）


# ── 数据结构 ────────────────────────────────────────────────────────


@dataclass
class ConsentRecord:
    """同意记录 — 三维授权矩阵的单条记录。

    Args:
        record_id: 记录唯一标识
        user_id: 用户编号
        data_type: 数据类型
        purpose: 处理目的
        status: 同意状态
        granted_at: 授权时间
        expires_at: 过期时间
        withdrawn_at: 撤回时间
        proof_hash: 授权证明哈希
    """
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    data_type: DataType = DataType.PERSONAL_IDENTIFIER
    purpose: ProcessingPurpose = ProcessingPurpose.SERVICE_DELIVERY
    status: ConsentStatus = ConsentStatus.PENDING
    granted_at: float = 0.0
    expires_at: float = 0.0
    withdrawn_at: float = 0.0
    proof_hash: str = ""

    def to_matrix_key(self) -> Tuple[str, str, str]:
        """生成三维矩阵索引键 (user_id, data_type, purpose)。"""
        return (self.user_id, self.data_type.value, self.purpose.value)


@dataclass
class ErasureRequest:
    """Article 17 擦除请求。

    Args:
        request_id: 请求唯一标识
        user_id: 请求用户编号
        requested_at: 请求时间
        erasure_scope: 擦除范围（空=全链路）
        reason: 擦除原因
        status: 处理状态
        proof: 擦除证明
    """
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    requested_at: float = field(default_factory=time.time)
    erasure_scope: List[StorageSubstrate] = field(default_factory=list)
    reason: str = ""
    status: ErasureStatus = ErasureStatus.PENDING
    proof: Optional[ErasureProof] = None


@dataclass
class DataSovereigntyRule:
    """数据驻留路由规则。

    Args:
        rule_id: 规则唯一标识
        region: 目标区域
        data_types: 适用的数据类型
        storage_paths: 存储路径模板
        retention_days: 数据保留天数
        encryption_required: 是否要求加密
        cross_border_allowed: 是否允许跨境传输
    """
    rule_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    region: DataRegion = DataRegion.GLOBAL
    data_types: List[DataType] = field(default_factory=list)
    storage_paths: List[str] = field(default_factory=list)
    retention_days: int = 365
    encryption_required: bool = True
    cross_border_allowed: bool = False


@dataclass
class AuditEntry:
    """合规审计条目。

    Args:
        entry_id: 条目唯一标识
        timestamp: 操作时间戳
        user_id: 关联用户编号
        action: 操作类型
        data_type: 涉及数据类型
        purpose: 处理目的
        resource: 操作资源标识
        operator: 操作者标识
        result: 操作结果
        metadata: 扩展元数据
        hash_chain: 审计链哈希（防篡改）
    """
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    user_id: str = ""
    action: AuditActionType = AuditActionType.DATA_ACCESS
    data_type: DataType = DataType.PERSONAL_IDENTIFIER
    purpose: ProcessingPurpose = ProcessingPurpose.SERVICE_DELIVERY
    resource: str = ""
    operator: str = "system"
    result: str = "success"
    metadata: Dict[str, Any] = field(default_factory=dict)
    hash_chain: str = ""


# ── 同意管理器 ─────────────────────────────────────────────────────


class ConsentManager:
    """三维授权矩阵管理器。

    管理 (user_id × DataType × ProcessingPurpose) → ConsentStatus 的三维授权矩阵。
    支持同意授予、撤回、过期管理、合法性检查。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._matrix: Dict[Tuple[str, str, str], ConsentRecord] = {}
        self._user_consents: Dict[str, List[ConsentRecord]] = defaultdict(list)
        self._grant_count: int = 0
        self._denial_count: int = 0

    def grant_consent(
        self,
        user_id: str,
        data_type: DataType,
        purpose: ProcessingPurpose,
        duration_days: int = 365,
    ) -> ConsentRecord:
        """授予同意。

        Args:
            user_id: 用户编号
            data_type: 数据类型
            purpose: 处理目的
            duration_days: 授权有效期（天）

        Returns:
            ConsentRecord: 创建的同意记录
        """
        with self._lock:
            key = (user_id, data_type.value, purpose.value)
            now = time.time()
            record = ConsentRecord(
                user_id=user_id,
                data_type=data_type,
                purpose=purpose,
                status=ConsentStatus.GRANTED,
                granted_at=now,
                expires_at=now + duration_days * 86400,
            )
            # 生成授权证明哈希
            proof_str = f"{user_id}|{data_type.value}|{purpose.value}|{now}"
            record.proof_hash = hashlib.sha256(proof_str.encode()).hexdigest()[:16]
            self._matrix[key] = record
            self._user_consents[user_id].append(record)
            self._grant_count += 1
            logger.info(f"Consent granted: user={user_id}, type={data_type.value}, purpose={purpose.value}")
            return record

    def withdraw_consent(
        self,
        user_id: str,
        data_type: Optional[DataType] = None,
        purpose: Optional[ProcessingPurpose] = None,
    ) -> int:
        """撤回同意。

        Args:
            user_id: 用户编号
            data_type: 数据类型（None = 全部类型）
            purpose: 处理目的（None = 全部目的）

        Returns:
            int: 撤回的记录数
        """
        with self._lock:
            count = 0
            now = time.time()
            if data_type is None and purpose is None:
                # 撤回该用户全部同意
                for key, record in list(self._matrix.items()):
                    if record.user_id == user_id and record.status == ConsentStatus.GRANTED:
                        record.status = ConsentStatus.WITHDRAWN
                        record.withdrawn_at = now
                        count += 1
            else:
                for key, record in list(self._matrix.items()):
                    if record.user_id != user_id:
                        continue
                    if data_type and record.data_type != data_type:
                        continue
                    if purpose and record.purpose != purpose:
                        continue
                    if record.status == ConsentStatus.GRANTED:
                        record.status = ConsentStatus.WITHDRAWN
                        record.withdrawn_at = now
                        count += 1
            logger.info(f"Consent withdrawn: user={user_id}, count={count}")
            return count

    def check_consent(
        self,
        user_id: str,
        data_type: DataType,
        purpose: ProcessingPurpose,
    ) -> bool:
        """检查是否已授权。

        Args:
            user_id: 用户编号
            data_type: 数据类型
            purpose: 处理目的

        Returns:
            bool: True=已授权可处理
        """
        with self._lock:
            key = (user_id, data_type.value, purpose.value)
            record = self._matrix.get(key)
            if record is None:
                return False
            if record.status == ConsentStatus.WITHDRAWN:
                return False
            if record.status == ConsentStatus.EXPIRED:
                return False
            if record.expires_at > 0 and record.expires_at < time.time():
                record.status = ConsentStatus.EXPIRED
                return False
            return record.status == ConsentStatus.GRANTED

    def get_user_consents(self, user_id: str) -> List[ConsentRecord]:
        """获取用户所有同意记录。"""
        with self._lock:
            return list(self._user_consents.get(user_id, []))

    def get_consent_matrix(self, user_id: str) -> Dict[str, Dict[str, str]]:
        """获取用户二维同意矩阵 (data_type → purpose → status)。"""
        with self._lock:
            matrix: Dict[str, Dict[str, str]] = {}
            for key, record in self._matrix.items():
                if record.user_id != user_id:
                    continue
                dt = record.data_type.value
                pp = record.purpose.value
                if dt not in matrix:
                    matrix[dt] = {}
                matrix[dt][pp] = record.status.value
            return matrix

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            total = len(self._matrix)
            active = sum(1 for r in self._matrix.values() if r.status == ConsentStatus.GRANTED)
            unique_users = len(self._user_consents)
            return {
                "total_records": total,
                "active_grants": active,
                "unique_users": unique_users,
                "grants": self._grant_count,
                "denials": self._denial_count,
                "status_distribution": {
                    s.value: sum(1 for r in self._matrix.values() if r.status == s)
                    for s in ConsentStatus
                },
            }


# ── Article 17 擦除引擎 ─────────────────────────────────────────────


class ErasureEngine:
    """GDPR Article 17 全链路擦除引擎。

    协调跨向量索引、知识图谱、元数据的全链路删除。
    与已有的 MemoryUnlearningManager 接口兼容。
    """

    def __init__(self, learning_manager: Optional[MemoryUnlearningManager] = None):
        self._lock = threading.RLock()
        self._unlearning = learning_manager or MemoryUnlearningManager()
        self._erasure_requests: List[ErasureRequest] = []
        self._erasure_count: int = 0

    def submit_erasure_request(
        self,
        user_id: str,
        reason: str = "GDPR Article 17 right to erasure",
        scope: Optional[List[StorageSubstrate]] = None,
    ) -> ErasureRequest:
        """提交擦除请求。

        Args:
            user_id: 请求用户编号
            reason: 擦除原因
            scope: 擦除基质范围（None=全链路）

        Returns:
            ErasureRequest: 擦除请求对象
        """
        with self._lock:
            if scope is None:
                scope = list(StorageSubstrate)
            req = ErasureRequest(
                user_id=user_id,
                reason=reason,
                erasure_scope=scope,
                status=ErasureStatus.IN_PROGRESS,
            )

            try:
                # 对每个存储基质执行擦除
                overall_status = ErasureStatus.VERIFIED
                for substrate in scope:
                    result: UnlearningResult = self._unlearning.erase(
                        memory_id=f"user:{user_id}",
                        substrates=[substrate.value],
                    )
                    if result.status == ErasureStatus.FAILED:
                        overall_status = ErasureStatus.FAILED
                    elif result.status == ErasureStatus.PARTIAL and overall_status != ErasureStatus.FAILED:
                        overall_status = ErasureStatus.PARTIAL

                req.status = overall_status
                req.proof = ErasureProof(
                    memory_id=f"user:{user_id}",
                    substrate=scope[0] if scope else StorageSubstrate.VECTOR_INDEX,
                    status=overall_status,
                )
            except Exception as e:
                logger.error(f"Erasure failed for user {user_id}: {e}")
                req.status = ErasureStatus.FAILED

            self._erasure_requests.append(req)
            self._erasure_count += 1
            logger.info(f"Erasure request submitted: {req.request_id} for user {user_id}, status={req.status.value}")
            return req

    def verify_erasure(self, request_id: str) -> bool:
        """验证擦除是否完成。

        Args:
            request_id: 擦除请求编号

        Returns:
            bool: True=擦除已验证完成
        """
        with self._lock:
            for req in self._erasure_requests:
                if req.request_id == request_id:
                    return req.status == ErasureStatus.VERIFIED
            return False

    def get_erasure_proof(self, request_id: str) -> Optional[ErasureProof]:
        """获取擦除证明。

        Args:
            request_id: 擦除请求编号

        Returns:
            Optional[ErasureProof]: 擦除证明对象
        """
        with self._lock:
            for req in self._erasure_requests:
                if req.request_id == request_id:
                    return req.proof
            return None

    def get_user_erasure_history(self, user_id: str) -> List[ErasureRequest]:
        """获取用户的所有擦除请求历史。"""
        with self._lock:
            return [r for r in self._erasure_requests if r.user_id == user_id]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            return {
                "total_erasure_requests": self._erasure_count,
                "pending": sum(1 for r in self._erasure_requests if r.status == ErasureStatus.PENDING),
                "verified": sum(1 for r in self._erasure_requests if r.status == ErasureStatus.VERIFIED),
                "failed": sum(1 for r in self._erasure_requests if r.status == ErasureStatus.FAILED),
                "partial": sum(1 for r in self._erasure_requests if r.status == ErasureStatus.PARTIAL),
            }


# ── 数据主权控制器 ──────────────────────────────────────────────────


class DataSovereignty:
    """地域数据驻留策略路由器。

    管理不同法域的数据存储位置、加密要求、跨境传输策略。
    支持 EU (GDPR) / CN (PIPL) / US (CCPA) / BR (LGPD) 四个区域。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._rules: Dict[DataRegion, List[DataSovereigntyRule]] = defaultdict(list)
        self._init_default_rules()

    def _init_default_rules(self) -> None:
        """初始化默认地域策略。"""
        # EU/EEA: 默认不允许跨境，所有个人数据需加密存储
        self._rules[DataRegion.EU_EEA].append(
            DataSovereigntyRule(
                region=DataRegion.EU_EEA,
                data_types=list(DataType),
                storage_paths=["eu-west-1:/data/trinity/"],
                retention_days=730,
                encryption_required=True,
                cross_border_allowed=False,
            )
        )
        # China Mainland: PIPL 要求
        self._rules[DataRegion.CHINA_MAINLAND].append(
            DataSovereigntyRule(
                region=DataRegion.CHINA_MAINLAND,
                data_types=list(DataType),
                storage_paths=["cn-north-1:/data/trinity/"],
                retention_days=365,
                encryption_required=True,
                cross_border_allowed=False,
            )
        )
        # United States: CCPA/CPRA
        self._rules[DataRegion.UNITED_STATES].append(
            DataSovereigntyRule(
                region=DataRegion.UNITED_STATES,
                data_types=list(DataType),
                storage_paths=["us-east-1:/data/trinity/"],
                retention_days=365,
                encryption_required=False,
                cross_border_allowed=True,
            )
        )
        # Brazil: LGPD
        self._rules[DataRegion.BRAZIL].append(
            DataSovereigntyRule(
                region=DataRegion.BRAZIL,
                data_types=list(DataType),
                storage_paths=["sa-east-1:/data/trinity/"],
                retention_days=365,
                encryption_required=True,
                cross_border_allowed=False,
            )
        )
        # Global fallback
        self._rules[DataRegion.GLOBAL].append(
            DataSovereigntyRule(
                region=DataRegion.GLOBAL,
                data_types=list(DataType),
                storage_paths=["global:/data/trinity/"],
                retention_days=365,
                encryption_required=False,
                cross_border_allowed=True,
            )
        )

    def add_rule(self, rule: DataSovereigntyRule) -> None:
        """添加自定义驻留规则。"""
        with self._lock:
            self._rules[rule.region].append(rule)
            logger.info(f"Added sovereignty rule for {rule.region.value}: {rule.rule_id}")

    def get_storage_path(self, region: DataRegion, data_type: DataType) -> str:
        """根据地域和数据类型获取存储路径。

        Args:
            region: 目标区域
            data_type: 数据类型

        Returns:
            str: 存储路径
        """
        with self._lock:
            rules = self._rules.get(region, self._rules[DataRegion.GLOBAL])
            for rule in rules:
                if data_type in rule.data_types:
                    return rule.storage_paths[0] if rule.storage_paths else ""
            return ""

    def get_retention_policy(self, region: DataRegion, data_type: DataType) -> int:
        """获取数据保留天数。

        Args:
            region: 目标区域
            data_type: 数据类型

        Returns:
            int: 保留天数
        """
        with self._lock:
            rules = self._rules.get(region, self._rules[DataRegion.GLOBAL])
            for rule in rules:
                if data_type in rule.data_types:
                    return rule.retention_days
            return 365

    def is_cross_border_allowed(self, source_region: DataRegion, target_region: DataRegion) -> bool:
        """检查是否允许跨境传输。"""
        with self._lock:
            rules = self._rules.get(source_region, self._rules[DataRegion.GLOBAL])
            for rule in rules:
                if not rule.cross_border_allowed:
                    return False
            return True

    def get_all_rules(self) -> Dict[str, List[Dict[str, Any]]]:
        """返回所有驻留规则（序列化）。"""
        with self._lock:
            return {
                region.value: [
                    {
                        "rule_id": r.rule_id,
                        "data_types": [dt.value for dt in r.data_types],
                        "retention_days": r.retention_days,
                        "encryption_required": r.encryption_required,
                        "cross_border_allowed": r.cross_border_allowed,
                    }
                    for r in rules_list
                ]
                for region, rules_list in self._rules.items()
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            total_rules = sum(len(rules) for rules in self._rules.values())
            return {
                "total_rules": total_rules,
                "regions": list(self._rules.keys()),
                "per_region_rules": {r.value: len(rules) for r, rules in self._rules.items()},
            }


# ── 合规审计器 ────────────────────────────────────────────────────


class ComplianceAuditor:
    """合规审计日志系统。

    记录所有数据访问/删除/修改操作，提供防篡改审计链。
    符合 GDPR Article 30 记录处理活动的要求。
    """

    MAX_ENTRIES_FOR_HASH = 1000

    def __init__(self, max_entries: int = 100000):
        self._lock = threading.RLock()
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)
        self._chain_head: str = ""
        self._entry_count: int = 0
        self._max_entries: int = max_entries

    def log_operation(
        self,
        user_id: str,
        action: AuditActionType,
        data_type: DataType,
        purpose: ProcessingPurpose,
        resource: str = "",
        operator: str = "system",
        result: str = "success",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        """记录一条合规审计日志。

        Args:
            user_id: 关联用户编号
            action: 操作类型
            data_type: 涉及数据类型
            purpose: 处理目的
            resource: 操作资源标识
            operator: 操作者
            result: 操作结果
            metadata: 扩展元数据

        Returns:
            AuditEntry: 创建的审计条目
        """
        with self._lock:
            entry = AuditEntry(
                timestamp=time.time(),
                user_id=user_id,
                action=action,
                data_type=data_type,
                purpose=purpose,
                resource=resource,
                operator=operator,
                result=result,
                metadata=metadata or {},
            )
            # 构建防篡改哈希链
            prev_hash = self._chain_head
            chain_input = (
                f"{entry.entry_id}|{entry.timestamp}|{user_id}|"
                f"{action.value}|{data_type.value}|{purpose.value}|{prev_hash}"
            )
            entry.hash_chain = hashlib.sha256(chain_input.encode()).hexdigest()[:32]
            self._chain_head = entry.hash_chain

            self._entries.append(entry)
            self._entry_count += 1
            logger.debug(f"Audit: {action.value} by {operator} on {resource} for user {user_id}")
            return entry

    def query(
        self,
        user_id: Optional[str] = None,
        action: Optional[AuditActionType] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """查询审计日志。

        Args:
            user_id: 按用户过滤
            action: 按操作类型过滤
            start_time: 开始时间
            end_time: 结束时间
            limit: 返回上限

        Returns:
            List[AuditEntry]: 匹配的审计条目
        """
        with self._lock:
            results = []
            for entry in self._entries:
                if user_id and entry.user_id != user_id:
                    continue
                if action and entry.action != action:
                    continue
                if start_time and entry.timestamp < start_time:
                    continue
                if end_time and entry.timestamp > end_time:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
            return results

    def verify_integrity(self) -> bool:
        """验证审计链完整性（防篡改检测）。"""
        with self._lock:
            entries_list = list(self._entries)
            if not entries_list:
                return True
            prev_hash = ""
            for entry in entries_list:
                chain_input = (
                    f"{entry.entry_id}|{entry.timestamp}|{entry.user_id}|"
                    f"{entry.action.value}|{entry.data_type.value}|{entry.purpose.value}|{prev_hash}"
                )
                expected_hash = hashlib.sha256(chain_input.encode()).hexdigest()[:32]
                if entry.hash_chain != expected_hash:
                    logger.error(f"Audit chain integrity violation at entry {entry.entry_id}")
                    return False
                prev_hash = entry.hash_chain
            return True

    def export_log(self, file_path: str, user_id: Optional[str] = None) -> None:
        """导出审计日志到文件（JSON Lines 格式）。

        Args:
            file_path: 输出文件路径
            user_id: 按用户过滤（None=全部）
        """
        with self._lock:
            entries = [e for e in self._entries if user_id is None or e.user_id == user_id]
            with open(file_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    line = json.dumps({
                        "entry_id": entry.entry_id,
                        "timestamp": entry.timestamp,
                        "user_id": entry.user_id,
                        "action": entry.action.value,
                        "data_type": entry.data_type.value,
                        "purpose": entry.purpose.value,
                        "resource": entry.resource,
                        "operator": entry.operator,
                        "result": entry.result,
                        "hash_chain": entry.hash_chain,
                    }, ensure_ascii=False)
                    f.write(line + "\n")
            logger.info(f"Exported {len(entries)} audit entries to {file_path}")

    def get_gdpr_report(self, user_id: str) -> Dict[str, Any]:
        """生成 GDPR 合规报告（针对特定用户）。

        包含：Article 15 访问记录、Article 17 删除记录、
        Article 16 修正记录、Article 20 可移植记录。
        """
        with self._lock:
            user_entries = [e for e in self._entries if e.user_id == user_id]
            return {
                "user_id": user_id,
                "total_operations": len(user_entries),
                "access_requests": sum(1 for e in user_entries if e.action == AuditActionType.ACCESS_REQUEST),
                "erasure_requests": sum(1 for e in user_entries if e.action == AuditActionType.ERASURE_REQUEST),
                "rectification_requests": sum(1 for e in user_entries if e.action == AuditActionType.RECTIFICATION_REQUEST),
                "portability_requests": sum(1 for e in user_entries if e.action == AuditActionType.PORTABILITY_REQUEST),
                "data_access_logs": sum(1 for e in user_entries if e.action == AuditActionType.DATA_ACCESS),
                "data_modifications": sum(1 for e in user_entries if e.action == AuditActionType.DATA_MODIFICATION),
                "data_deletions": sum(1 for e in user_entries if e.action == AuditActionType.DATA_DELETION),
                "consent_changes": sum(1 for e in user_entries if e.action == AuditActionType.CONSENT_CHANGE),
                "first_operation": min((e.timestamp for e in user_entries), default=0.0),
                "last_operation": max((e.timestamp for e in user_entries), default=0.0),
                "chain_integrity_verified": self.verify_integrity(),
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            action_counts = defaultdict(int)
            user_counts: Dict[str, int] = defaultdict(int)
            for entry in self._entries:
                action_counts[entry.action.value] += 1
                user_counts[entry.user_id] += 1
            return {
                "total_entries": self._entry_count,
                "current_buffer_size": len(self._entries),
                "chain_integrity_verified": self.verify_integrity(),
                "unique_users": len(user_counts),
                "action_distribution": dict(action_counts),
                "max_entries": self._max_entries,
            }
