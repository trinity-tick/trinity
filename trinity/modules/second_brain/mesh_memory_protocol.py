
"""
# status: orphan (2026-08-15 audit, not in runtime path)
P18-1: Mesh Memory Protocol — 网格记忆协议

对标论文: Mesh Memory Protocol (arXiv 2604.19540)
核心发现: 去中心化对等网格记忆融合，语境记忆块 (CMB) 跨节点语义对齐与传播
三元语: CMB 构建 → SVAF 语义对齐融合 → 网格拓扑管理 → 分层存储路由 → 预注册合规审计

设计要点:
- CMBBuilder: 构建标准 Contextual Memory Block，含 key/createdBy/createdAt/fields(focus/issue/details/evidence/decision/status)
- SVAFEngine: Semantic Vector Analysis Fusion，将接收的 CMB 与本地语义空间对齐后融合
- MeshTopologyManager: 网格拓扑管理，对等方注册/发现/健康检查/多角色(execution/quality-review/compliance)
- TieredStorageRouter: 热/温/冷三层存储路由，hot(内存)/warm(SSD)/cold(归档)，基于访问频率自动升降级
- PreRegistrationAuditor: 预注册合规审计，在 CMB 传播前验证 wave-level 通过率和方法论一致性，防止方法论漂移
- MeshPeerRegistry: 对等方注册表，每对等方的持久本地 CMB 存储，发送前本地存储→接收后 SVAF 融合→remix 存储
- 与 P12 multi_agent_topology.py 互补——topology 做协调路由，本模块做协议级网格语义基础设施
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class CMBStatus(Enum):
    """Contextual Memory Block 状态"""
    DRAFT = "draft"
    PRE_REGISTERED = "pre_registered"
    PROPAGATING = "propagating"
    MERGED = "merged"
    CONFLICT = "conflict"
    ARCHIVED = "archived"


class StorageTier(Enum):
    """三层存储层级"""
    HOT = "hot"       # 内存 resident
    WARM = "warm"     # SSD / 本地磁盘
    COLD = "cold"     # 归档 / 对象存储


class PeerRole(Enum):
    """对等方角色"""
    EXECUTION = "execution"
    QUALITY_REVIEW = "quality_review"
    COMPLIANCE = "compliance"
    OBSERVER = "observer"


class PeerHealth(Enum):
    """对等方健康状态"""
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    QUARANTINED = "quarantined"


class FusionStrategy(Enum):
    """SVAF 融合策略"""
    WEIGHTED_AVERAGE = "weighted_average"
    MAX_POOLING = "max_pooling"
    ATTENTION_WEIGHTED = "attention_weighted"
    CONCAT_PROJECT = "concat_project"
    GATE_FUSION = "gate_fusion"


class AuditVerdict(Enum):
    """审计判定"""
    PASS = "pass"
    PASS_WITH_WARNING = "pass_with_warning"
    REJECT = "reject"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class CMBSchema:
    """Contextual Memory Block 模式定义"""
    key: str
    created_by: str                       # 创建者 peer ID
    created_at: float                     # Unix 时间戳
    focus: str                            # 核心关注点
    issue: str                            # 问题描述
    details: Dict[str, Any] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    decision: Optional[str] = None
    status: CMBStatus = CMBStatus.DRAFT
    version: int = 1
    parent_key: Optional[str] = None      # 派生来源 CMB key
    tags: List[str] = field(default_factory=list)
    methodology_version: str = "1.0.0"

    def to_vector(self) -> np.ndarray:
        """将 CMB 转换为初始语义向量 (基于 key+focus+issue+decision 的 hash 投影)"""
        text = f"{self.key}|{self.focus}|{self.issue}|{self.decision or ''}"
        h = hashlib.sha256(text.encode()).digest()
        return np.frombuffer(h[:128], dtype=np.float32).reshape(4, -1).mean(axis=0)


@dataclass
class PeerRecord:
    """对等方记录"""
    peer_id: str
    role: PeerRole
    health: PeerHealth = PeerHealth.ONLINE
    address: str = ""
    last_heartbeat: float = field(default_factory=time.time)
    registered_at: float = field(default_factory=time.time)
    capabilities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    cmb_count: int = 0
    success_rate: float = 1.0


@dataclass
class FusionResult:
    """SVAF 融合结果"""
    cmb_key: str
    pre_fusion_norm: float
    post_fusion_norm: float
    alignment_score: float               # 语义对齐分数 [0,1]
    fusion_strategy: FusionStrategy
    merged_dim: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class AuditWaveReport:
    """单波审计报告"""
    wave_id: str
    total_cmbs: int
    passed: int
    warned: int
    rejected: int
    pass_rate: float
    methodology_drift_score: float        # 方法论漂移分数 [0,1]，越低越好
    drift_warnings: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class StorageRoutingDecision:
    """存储路由决策"""
    cmb_key: str
    target_tier: StorageTier
    reason: str
    estimated_latency_ms: float
    access_frequency: float               # 最近 N 分钟访问次数/分钟
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProtocolStats:
    """网格协议运行时统计"""
    total_cmbs_created: int = 0
    total_cmbs_fused: int = 0
    active_peers: int = 0
    hot_tier_count: int = 0
    warm_tier_count: int = 0
    cold_tier_count: int = 0
    overall_pass_rate: float = 1.0
    avg_alignment_score: float = 1.0
    last_audit_wave: Optional[str] = None


# ============================================================================
# P18-1-1: CMBBuilder — Contextual Memory Block 构建器
# ============================================================================

class CMBBuilder:
    """构建标准 CMB，提取结构化语境记忆"""

    def __init__(self, default_peer_id: str = "trinity-local"):
        self._lock = threading.RLock()
        self._default_peer_id = default_peer_id
        self._cmb_store: Dict[str, CMBSchema] = OrderedDict()
        self._version_counter: int = 0

    def build(
        self,
        focus: str,
        issue: str,
        details: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[str]] = None,
        decision: Optional[str] = None,
        tags: Optional[List[str]] = None,
        parent_key: Optional[str] = None,
    ) -> CMBSchema:
        """构建新的 Contextual Memory Block"""
        with self._lock:
            self._version_counter += 1
            key_base = f"cmb:{focus}:{hashlib.md5(focus.encode()).hexdigest()[:12]}"
            key = f"{key_base}:v{self._version_counter}"
            cmb = CMBSchema(
                key=key,
                created_by=self._default_peer_id,
                created_at=time.time(),
                focus=focus,
                issue=issue,
                details=details or {},
                evidence=evidence or [],
                decision=decision,
                tags=tags or [],
                parent_key=parent_key,
            )
            self._cmb_store[key] = cmb
            logger.info(f"CMB built: {key} (focus={focus[:40]}...)")
            return cmb

    def get(self, key: str) -> Optional[CMBSchema]:
        with self._lock:
            return self._cmb_store.get(key)

    def update_status(self, key: str, status: CMBStatus) -> bool:
        with self._lock:
            cmb = self._cmb_store.get(key)
            if cmb is None:
                return False
            cmb.status = status
            return True

    def list_by_status(self, status: CMBStatus) -> List[CMBSchema]:
        with self._lock:
            return [c for c in self._cmb_store.values() if c.status == status]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cmbs": len(self._cmb_store),
                "by_status": {s.value: len(self.list_by_status(s)) for s in CMBStatus},
                "version_counter": self._version_counter,
            }


# ============================================================================
# P18-1-2: SVAFEngine — Semantic Vector Analysis Fusion
# ============================================================================

class SVAFEngine:
    """语义向量分析融合：将接收的 CMB 与本地语义空间对齐后融合"""

    def __init__(self, local_dim: int = 128, default_strategy: FusionStrategy = FusionStrategy.ATTENTION_WEIGHTED):
        self._lock = threading.RLock()
        self._local_dim = local_dim
        self._default_strategy = default_strategy
        self._local_semantic_space: Dict[str, np.ndarray] = {}  # key -> embedding
        self._fusion_log: List[FusionResult] = []
        self._attention_weights: Dict[str, float] = {}
        np.random.seed(42)
        # 模拟本地语义投影矩阵
        self._projection_matrix = np.random.randn(local_dim, local_dim) * 0.02

    def register_local_semantic(self, key: str, embedding: np.ndarray):
        """注册本地语义向量"""
        with self._lock:
            if embedding.shape[0] != self._local_dim:
                embedding = self._align_dim(embedding)
            self._local_semantic_space[key] = embedding

    def fuse(
        self,
        incoming_cmbs: List[CMBSchema],
        strategy: Optional[FusionStrategy] = None,
    ) -> List[FusionResult]:
        """融合接收到的 CMB 到本地语义空间"""
        strategy = strategy or self._default_strategy
        results: List[FusionResult] = []
        with self._lock:
            for cmb in incoming_cmbs:
                incoming_vec = cmb.to_vector()
                if incoming_vec.shape[0] != self._local_dim:
                    incoming_vec = self._align_dim(incoming_vec)

                # 与本地空间对齐
                aligned_vec = self._projection_matrix @ incoming_vec
                pre_norm = float(np.linalg.norm(aligned_vec))

                # 根据策略融合
                if cmb.key in self._local_semantic_space:
                    local_vec = self._local_semantic_space[cmb.key]
                    if strategy == FusionStrategy.WEIGHTED_AVERAGE:
                        fused = 0.5 * local_vec + 0.5 * aligned_vec
                    elif strategy == FusionStrategy.MAX_POOLING:
                        fused = np.maximum(local_vec, aligned_vec)
                    elif strategy == FusionStrategy.ATTENTION_WEIGHTED:
                        attn_key = f"{cmb.key}:{cmb.created_by}"
                        weight = self._attention_weights.get(attn_key, 0.5)
                        fused = weight * aligned_vec + (1 - weight) * local_vec
                    elif strategy == FusionStrategy.CONCAT_PROJECT:
                        concat = np.concatenate([local_vec, aligned_vec])
                        proj = np.random.randn(self._local_dim, self._local_dim * 2) * 0.01
                        fused = proj @ concat
                    else:  # GATE_FUSION
                        gate = 1.0 / (1.0 + np.exp(-np.dot(local_vec, aligned_vec) / self._local_dim))
                        fused = gate * aligned_vec + (1 - gate) * local_vec
                else:
                    fused = aligned_vec

                post_norm = float(np.linalg.norm(fused))
                alignment_score = float(np.dot(aligned_vec, fused) / (pre_norm * post_norm + 1e-8))

                self._local_semantic_space[cmb.key] = fused
                result = FusionResult(
                    cmb_key=cmb.key,
                    pre_fusion_norm=pre_norm,
                    post_fusion_norm=post_norm,
                    alignment_score=max(0.0, min(1.0, alignment_score)),
                    fusion_strategy=strategy,
                    merged_dim=self._local_dim,
                )
                self._fusion_log.append(result)
                results.append(result)

            logger.info(f"SVAF fused {len(incoming_cmbs)} CMBs via {strategy.value}")
        return results

    def get_semantic_space(self) -> Dict[str, np.ndarray]:
        with self._lock:
            return dict(self._local_semantic_space)

    def set_attention_weight(self, peer_id: str, cmb_key: str, weight: float):
        with self._lock:
            self._attention_weights[f"{cmb_key}:{peer_id}"] = max(0.0, min(1.0, weight))

    def _align_dim(self, vec: np.ndarray) -> np.ndarray:
        """对齐向量维度到 local_dim"""
        if vec.shape[0] > self._local_dim:
            return vec[:self._local_dim]
        else:
            padded = np.zeros(self._local_dim, dtype=np.float32)
            padded[:vec.shape[0]] = vec
            return padded

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            scores = [r.alignment_score for r in self._fusion_log] if self._fusion_log else [1.0]
            return {
                "local_entries": len(self._local_semantic_space),
                "total_fusions": len(self._fusion_log),
                "avg_alignment_score": float(np.mean(scores)),
                "min_alignment_score": float(np.min(scores)),
                "dimension": self._local_dim,
            }


# ============================================================================
# P18-1-3: MeshTopologyManager — 网格拓扑管理
# ============================================================================

class MeshTopologyManager:
    """对等方注册/发现/健康检查/多角色管理"""

    HEARTBEAT_TIMEOUT = 30.0              # 心跳超时秒数
    HEALTH_CHECK_INTERVAL = 10.0          # 健康检查间隔

    def __init__(self, local_peer_id: str = "trinity-local-001"):
        self._lock = threading.RLock()
        self._local_peer_id = local_peer_id
        self._peers: Dict[str, PeerRecord] = {}
        self._health_check_timer: Optional[threading.Thread] = None
        self._stop_health_check = threading.Event()

    def register_peer(
        self,
        peer_id: str,
        role: PeerRole,
        address: str = "",
        capabilities: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PeerRecord:
        """注册对等方"""
        with self._lock:
            if peer_id in self._peers:
                # 更新已存在记录
                peer = self._peers[peer_id]
                peer.health = PeerHealth.ONLINE
                peer.last_heartbeat = time.time()
                if role != PeerRole.OBSERVER:
                    peer.role = role
                return peer

            peer = PeerRecord(
                peer_id=peer_id,
                role=role,
                address=address,
                capabilities=capabilities or [],
                metadata=metadata or {},
            )
            self._peers[peer_id] = peer
            logger.info(f"Peer registered: {peer_id} (role={role.value})")
            return peer

    def discover_peers(self, role: Optional[PeerRole] = None) -> List[PeerRecord]:
        """发现对等方"""
        with self._lock:
            if role:
                return [p for p in self._peers.values() if p.role == role]
            return list(self._peers.values())

    def heartbeat(self, peer_id: str) -> bool:
        """接收心跳"""
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None:
                return False
            peer.last_heartbeat = time.time()
            if peer.health == PeerHealth.OFFLINE:
                peer.health = PeerHealth.DEGRADED
            return True

    def check_health(self) -> Dict[str, PeerHealth]:
        """全量健康检查"""
        with self._lock:
            now = time.time()
            changes: Dict[str, PeerHealth] = {}
            for pid, peer in list(self._peers.items()):
                elapsed = now - peer.last_heartbeat
                old_health = peer.health
                if elapsed > self.HEARTBEAT_TIMEOUT * 3:
                    peer.health = PeerHealth.OFFLINE
                elif elapsed > self.HEARTBEAT_TIMEOUT * 1.5:
                    peer.health = PeerHealth.DEGRADED
                elif elapsed > self.HEARTBEAT_TIMEOUT and peer.health == PeerHealth.OFFLINE:
                    peer.health = PeerHealth.DEGRADED
                if peer.health != old_health:
                    changes[pid] = peer.health
            return changes

    def quarantine_peer(self, peer_id: str, reason: str = "") -> bool:
        """隔离异常对等方"""
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None:
                return False
            peer.health = PeerHealth.QUARANTINED
            peer.metadata["quarantine_reason"] = reason
            logger.warning(f"Peer quarantined: {peer_id} — {reason}")
            return True

    def start_health_check(self):
        """启动后台健康检查"""
        if self._health_check_timer and self._health_check_timer.is_alive():
            return

        def _loop():
            while not self._stop_health_check.wait(self.HEALTH_CHECK_INTERVAL):
                self.check_health()

        self._stop_health_check.clear()
        self._health_check_timer = threading.Thread(target=_loop, daemon=True)
        self._health_check_timer.start()

    def stop_health_check(self):
        self._stop_health_check.set()

    def get_role_peers(self, role: PeerRole) -> List[PeerRecord]:
        with self._lock:
            return [p for p in self._peers.values()
                    if p.role == role and p.health == PeerHealth.ONLINE]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_peers": len(self._peers),
                "online": sum(1 for p in self._peers.values() if p.health == PeerHealth.ONLINE),
                "degraded": sum(1 for p in self._peers.values() if p.health == PeerHealth.DEGRADED),
                "offline": sum(1 for p in self._peers.values() if p.health == PeerHealth.OFFLINE),
                "quarantined": sum(1 for p in self._peers.values() if p.health == PeerHealth.QUARANTINED),
                "by_role": {r.value: len(self.get_role_peers(r)) for r in PeerRole},
                "local_peer_id": self._local_peer_id,
            }


# ============================================================================
# P18-1-4: TieredStorageRouter — 热/温/冷三层存储路由
# ============================================================================

class TieredStorageRouter:
    """基于访问频率的自动升降级三层存储路由"""

    FREQ_WINDOW_SECONDS = 300.0           # 访问频率统计窗口 (5分钟)

    def __init__(self):
        self._lock = threading.RLock()
        # cmb_key -> access timestamps deque
        self._access_log: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        # cmb_key -> current tier
        self._tier_map: Dict[str, StorageTier] = {}
        # cmb_key -> CMBSchema
        self._cmb_data: Dict[str, CMBSchema] = {}
        # 升级阈值 (次/分钟)
        self._promote_threshold: float = 2.0   # 超过此值升级到 hot
        self._demote_threshold: float = 0.1    # 低于此值降级到 cold
        self._route_log: List[StorageRoutingDecision] = []

    def store(self, cmb: CMBSchema):
        """存储 CMB，初始放 warm"""
        with self._lock:
            self._cmb_data[cmb.key] = cmb
            self._tier_map[cmb.key] = StorageTier.WARM
            self._access_log[cmb.key].append(time.time())

    def access(self, key: str) -> Optional[CMBSchema]:
        """访问 CMB 并记录"""
        with self._lock:
            cmb = self._cmb_data.get(key)
            if cmb is None:
                return None
            self._access_log[key].append(time.time())
            self._maybe_rebalance(key)
            return cmb

    def _get_access_frequency(self, key: str) -> float:
        """计算每分钟访问频率"""
        now = time.time()
        window_start = now - self.FREQ_WINDOW_SECONDS
        recent = [t for t in self._access_log.get(key, deque()) if t >= window_start]
        return len(recent) / (self.FREQ_WINDOW_SECONDS / 60.0)

    def _maybe_rebalance(self, key: str):
        """基于访问频率自动升降级"""
        freq = self._get_access_frequency(key)
        current = self._tier_map.get(key, StorageTier.WARM)

        if freq >= self._promote_threshold and current != StorageTier.HOT:
            self._tier_map[key] = StorageTier.HOT
            decision = StorageRoutingDecision(
                cmb_key=key, target_tier=StorageTier.HOT,
                reason=f"Promoted: freq={freq:.2f}/min >= threshold={self._promote_threshold}",
                estimated_latency_ms=1.0, access_frequency=freq,
            )
            self._route_log.append(decision)
            logger.debug(f"Storage tier promoted: {key} → HOT (freq={freq:.2f})")
        elif freq <= self._demote_threshold and current == StorageTier.WARM:
            self._tier_map[key] = StorageTier.COLD
            decision = StorageRoutingDecision(
                cmb_key=key, target_tier=StorageTier.COLD,
                reason=f"Demoted: freq={freq:.2f}/min <= threshold={self._demote_threshold}",
                estimated_latency_ms=100.0, access_frequency=freq,
            )
            self._route_log.append(decision)
            logger.debug(f"Storage tier demoted: {key} → COLD (freq={freq:.2f})")
        elif self._demote_threshold < freq < self._promote_threshold and current != StorageTier.WARM:
            self._tier_map[key] = StorageTier.WARM
            decision = StorageRoutingDecision(
                cmb_key=key, target_tier=StorageTier.WARM,
                reason=f"Reverted to WARM: freq={freq:.2f}/min",
                estimated_latency_ms=10.0, access_frequency=freq,
            )
            self._route_log.append(decision)

    def get_tier(self, key: str) -> StorageTier:
        with self._lock:
            return self._tier_map.get(key, StorageTier.WARM)

    def rebalance_all(self):
        """全量重平衡"""
        with self._lock:
            for key in list(self._cmb_data.keys()):
                self._maybe_rebalance(key)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cmbs": len(self._cmb_data),
                "hot_count": sum(1 for t in self._tier_map.values() if t == StorageTier.HOT),
                "warm_count": sum(1 for t in self._tier_map.values() if t == StorageTier.WARM),
                "cold_count": sum(1 for t in self._tier_map.values() if t == StorageTier.COLD),
                "route_decisions": len(self._route_log),
                "promote_threshold": self._promote_threshold,
                "demote_threshold": self._demote_threshold,
            }


# ============================================================================
# P18-1-5: PreRegistrationAuditor — 预注册合规审计
# ============================================================================

class PreRegistrationAuditor:
    """CMB 传播前的 wave-level 合规审计，防止方法论漂移"""

    MIN_PASS_RATE = 0.80                  # 最小通过率阈值

    def __init__(self, methodology_version: str = "1.0.0"):
        self._lock = threading.RLock()
        self._methodology_version = methodology_version
        self._allowed_methodologies: Set[str] = {methodology_version}
        self._audit_log: List[AuditWaveReport] = []

    def audit_wave(self, cmbs: List[CMBSchema], wave_id: Optional[str] = None) -> AuditWaveReport:
        """对一批 CMB 做 wave-level 审计"""
        wave_id = wave_id or f"wave-{uuid.uuid4().hex[:8]}"
        passed, warned, rejected = 0, 0, 0
        drift_warnings: List[str] = []

        for cmb in cmbs:
            if cmb.methodology_version not in self._allowed_methodologies:
                rejected += 1
                drift_warnings.append(
                    f"CMB {cmb.key[:40]}: methodology {cmb.methodology_version} not in allowed set"
                )
                continue

            # 检查 CMB 字段完整性
            if not cmb.focus or not cmb.issue:
                rejected += 1
                drift_warnings.append(f"CMB {cmb.key[:40]}: missing focus or issue")
                continue

            # 检查 evidence 至少有一条或是合理标记
            if not cmb.evidence and cmb.decision:
                warned += 1
                drift_warnings.append(f"CMB {cmb.key[:40]}: decision without evidence")
                continue

            passed += 1

        total = len(cmbs)
        pass_rate = passed / total if total > 0 else 0.0
        drift_score = (warned + rejected) / total if total > 0 else 0.0

        report = AuditWaveReport(
            wave_id=wave_id,
            total_cmbs=total,
            passed=passed,
            warned=warned,
            rejected=rejected,
            pass_rate=pass_rate,
            methodology_drift_score=drift_score,
            drift_warnings=drift_warnings,
        )

        with self._lock:
            self._audit_log.append(report)

        logger.info(f"Audit wave {wave_id}: pass_rate={pass_rate:.2%}, drift={drift_score:.2%}")
        return report

    def is_wave_approved(self, report: AuditWaveReport) -> bool:
        """判断 wave 是否通过审批"""
        return report.pass_rate >= self.MIN_PASS_RATE

    def add_methodology(self, version: str):
        """注册允许的方法论版本"""
        with self._lock:
            self._allowed_methodologies.add(version)

    def get_audit_history(self) -> List[AuditWaveReport]:
        with self._lock:
            return list(self._audit_log)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = sum(r.total_cmbs for r in self._audit_log)
            passed = sum(r.passed for r in self._audit_log)
            return {
                "total_waves": len(self._audit_log),
                "total_cmbs_audited": total,
                "cumulative_pass_rate": passed / total if total > 0 else 0.0,
                "allowed_methodologies": list(self._allowed_methodologies),
                "min_pass_rate": self.MIN_PASS_RATE,
            }


# ============================================================================
# P18-1-6: MeshPeerRegistry — 对等方注册表
# ============================================================================

class MeshPeerRegistry:
    """每对等方的持久本地 CMB 存储，发送前本地存储→接收后 SVAF 融合→remix 存储"""

    def __init__(self, peer_id: str, svaf_engine: Optional[SVAFEngine] = None):
        self._lock = threading.RLock()
        self._peer_id = peer_id
        self._svaf = svaf_engine or SVAFEngine()
        self._local_store: Dict[str, CMBSchema] = {}        # 发送前本地存储
        self._remix_store: Dict[str, CMBSchema] = {}         # 融合后 remix 存储
        self._send_log: List[Tuple[str, float]] = []         # (cmb_key, timestamp)
        self._receive_log: List[Tuple[str, float, str]] = [] # (cmb_key, timestamp, from_peer)

    def store_before_send(self, cmb: CMBSchema):
        """发送前本地持久化存储"""
        with self._lock:
            self._local_store[cmb.key] = cmb
            self._send_log.append((cmb.key, time.time()))
            logger.debug(f"[{self._peer_id}] Local stored before send: {cmb.key}")

    def receive_and_fuse(self, cmbs: List[CMBSchema], from_peer: str) -> List[FusionResult]:
        """接收 CMB → SVAF 融合 → remix 存储"""
        with self._lock:
            fusion_results = self._svaf.fuse(cmbs)
            for cmb in cmbs:
                self._remix_store[cmb.key] = cmb
                self._receive_log.append((cmb.key, time.time(), from_peer))
            logger.info(f"[{self._peer_id}] Received & fused {len(cmbs)} CMBs from {from_peer}")
            return fusion_results

    def get_local_cmb(self, key: str) -> Optional[CMBSchema]:
        with self._lock:
            return self._local_store.get(key)

    def get_remix_cmb(self, key: str) -> Optional[CMBSchema]:
        with self._lock:
            return self._remix_store.get(key)

    def get_sent_keys(self) -> List[str]:
        with self._lock:
            return [k for k, _ in self._send_log]

    def get_received_from(self, peer_id: str) -> List[str]:
        with self._lock:
            return [k for k, _, p in self._receive_log if p == peer_id]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "peer_id": self._peer_id,
                "local_store_size": len(self._local_store),
                "remix_store_size": len(self._remix_store),
                "total_sent": len(self._send_log),
                "total_received": len(self._receive_log),
                "svaf_stats": self._svaf.statistics(),
            }
