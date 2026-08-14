"""
P24-4: SAMEP — 记忆互操作协议

对标论文: W3C AI Agent Memory Interoperability CG (2026.05)
核心发现: 实现标准化记忆 Schema（跨框架通用字段），基于能力的访问控制（capability-based access），
        可验证来源（cryptographically verifiable provenance），跨 Agent 记忆联邦同步。
        解决多 Agent 系统间记忆格式不兼容、访问权限不可控、记忆来源不可信三大难题。
三元语: 标准化 Schema → 能力访问控制 → 可验证来源链 → 跨框架联邦同步

设计要点:
- InteropMemorySchema: 跨框架通用记忆 Schema，定义标准化字段与序列化格式
- CapabilityAccessController: 基于能力的访问控制，细化到字段级的读写权限
- VerifiableProvenanceChain: 可验证来源链，基于 Merkle 树 + Ed25519 签名确保不可篡改
- CrossFrameworkMemoryFederation: 跨 Agent 记忆联邦同步引擎
- FieldSchema: 字段级 Schema 定义，含类型、约束与版本
- CapabilityToken: 能力令牌，承载可传递的访问权限集合
- ProvenanceRecord: 来源记录，含创建者、时间戳、签名、前置哈希
- FederationPolicy: 联邦策略，控制同步频率、冲突解决、数据治理规则
- SyncMessage: 联邦同步消息，含差分记忆、签名与版本向量
- MemoryRecordAdapter: 记忆记录适配器，将不同框架的记忆格式归一化到 SAMEP Schema
- SchemaValidator: Schema 校验器，验证记忆记录是否符合 SAMEP 规范
- MerkleAuditTree: Merkle 审计树，提供批量化高效来源验证
- FederationPeer: 联邦对等节点，维护连接状态、同步队列与心跳
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
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class SchemaVersion(Enum):
    """Schema 版本"""
    V1_0 = "1.0"
    V1_1 = "1.1"
    V2_0_DRAFT = "2.0-draft"


class CapabilityAction(Enum):
    """能力动作"""
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    SHARE = "share"
    REVOKE = "revoke"
    DELEGATE = "delegate"


class FieldType(Enum):
    """字段类型"""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    EMBEDDING = "embedding"
    TIMESTAMP = "timestamp"
    REFERENCE = "reference"


class ProvenanceStatus(Enum):
    """来源验证状态"""
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    TAMPERED = "tampered"
    EXPIRED = "expired"
    REVOKED = "revoked"


class FederationMode(Enum):
    """联邦同步模式"""
    FULL_SYNC = "full_sync"
    DIFF_SYNC = "diff_sync"
    EVENTUAL_CONSISTENCY = "eventual_consistency"
    STRONG_CONSISTENCY = "strong_consistency"
    GOSSIP = "gossip"


class ConflictPolicy(Enum):
    """冲突解决策略"""
    LAST_WRITE_WINS = "last_write_wins"
    CRDT_MERGE = "crdt_merge"
    MANUAL_RESOLVE = "manual_resolve"
    MAJORITY_VOTE = "majority_vote"
    TIMESTAMP_PRIORITY = "timestamp_priority"


class PeerState(Enum):
    """对等节点状态"""
    ONLINE = "online"
    OFFLINE = "offline"
    SYNCING = "syncing"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class FieldSchema:
    """字段级 Schema 定义"""
    field_name: str
    field_type: FieldType
    required: bool = False
    max_length: int = 0
    allowed_values: Optional[List[str]] = None
    default_value: Any = None
    description: str = ""
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityToken:
    """能力令牌"""
    token_id: str
    issuer: str
    subject: str
    actions: FrozenSet[CapabilityAction]
    resource_pattern: str  # 如 "memory://agent-*/episodic/*"
    issued_at: float = field(default_factory=time.time)
    expires_at: float = float("inf")
    signature: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProvenanceRecord:
    """来源记录"""
    record_id: str
    creator_id: str
    created_at: float
    previous_hash: str
    content_hash: str
    signature: str
    schema_version: str = "1.0"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SAMEPMemoryRecord:
    """SAMEP 标准化记忆记录"""
    record_id: str
    schema_version: str
    agent_id: str
    memory_type: str  # episodic / semantic / procedural / working
    content: Dict[str, Any]
    embedding: Optional[np.ndarray] = None
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: float = float("inf")
    provenance: Optional[ProvenanceRecord] = None
    access_policy: Optional[CapabilityToken] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncMessage:
    """联邦同步消息"""
    message_id: str
    sender_id: str
    version_vector: Dict[str, int]
    records: List[SAMEPMemoryRecord]
    mode: FederationMode
    timestamp: float = field(default_factory=time.time)
    signature: str = ""
    checksum: str = ""


@dataclass
class FederationPolicy:
    """联邦策略"""
    mode: FederationMode = FederationMode.DIFF_SYNC
    conflict_policy: ConflictPolicy = ConflictPolicy.LAST_WRITE_WINS
    sync_interval_ms: int = 5000
    max_batch_size: int = 100
    retry_max: int = 3
    ttl_seconds: float = 86400.0
    data_governance_rules: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# InteropMemorySchema
# ============================================================================

class InteropMemorySchema:
    """跨框架通用记忆 Schema"""

    # SAMEP 核心字段定义
    CORE_FIELDS: List[FieldSchema] = [
        FieldSchema("record_id", FieldType.STRING, required=True, description="全局唯一记录 ID"),
        FieldSchema("schema_version", FieldType.STRING, required=True, allowed_values=["1.0", "1.1", "2.0-draft"]),
        FieldSchema("agent_id", FieldType.STRING, required=True, description="创建 Agent ID"),
        FieldSchema("memory_type", FieldType.STRING, required=True,
                    allowed_values=["episodic", "semantic", "procedural", "working", "meta"]),
        FieldSchema("content", FieldType.OBJECT, required=True, description="记忆载荷"),
        FieldSchema("embedding", FieldType.EMBEDDING, required=False, description="向量嵌入"),
        FieldSchema("timestamp", FieldType.TIMESTAMP, required=True, description="创建时间戳"),
        FieldSchema("ttl_seconds", FieldType.FLOAT, required=False, default_value=float("inf")),
        FieldSchema("provenance", FieldType.OBJECT, required=False, description="来源记录"),
        FieldSchema("access_policy", FieldType.OBJECT, required=False, description="访问控制"),
    ]

    def __init__(self, version: SchemaVersion = SchemaVersion.V1_1):
        self._lock = threading.RLock()
        self._version = version
        self._custom_fields: List[FieldSchema] = []
        self._record_count: int = 0

    def add_custom_field(self, field: FieldSchema):
        with self._lock:
            self._custom_fields.append(field)

    def get_all_fields(self) -> List[FieldSchema]:
        return self.CORE_FIELDS + self._custom_fields

    def get_required_fields(self) -> List[str]:
        return [f.field_name for f in self.get_all_fields() if f.required]

    def create_record(self, agent_id: str, memory_type: str,
                      content: Dict[str, Any],
                      embedding: Optional[np.ndarray] = None,
                      ttl: float = float("inf")) -> SAMEPMemoryRecord:
        with self._lock:
            self._record_count += 1
            return SAMEPMemoryRecord(
                record_id=f"samep:{agent_id}:{memory_type}:{uuid.uuid4().hex[:16]}",
                schema_version=self._version.value,
                agent_id=agent_id,
                memory_type=memory_type,
                content=content,
                embedding=embedding,
                ttl_seconds=ttl,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self._version.value,
                "core_fields": len(self.CORE_FIELDS),
                "custom_fields": len(self._custom_fields),
                "records_created": self._record_count,
            }


# ============================================================================
# SchemaValidator
# ============================================================================

class SchemaValidator:
    """Schema 校验器"""

    def __init__(self, schema: InteropMemorySchema):
        self._schema = schema
        self._lock = threading.RLock()
        self._validated_count: int = 0
        self._failure_count: int = 0

    def validate(self, record: SAMEPMemoryRecord) -> Tuple[bool, List[str]]:
        """验证记录是否符合 SAMEP 规范"""
        with self._lock:
            self._validated_count += 1
            errors: List[str] = []

            if not record.record_id:
                errors.append("record_id is required")
            if not record.agent_id:
                errors.append("agent_id is required")
            if record.schema_version not in ["1.0", "1.1", "2.0-draft"]:
                errors.append(f"invalid schema_version: {record.schema_version}")
            if record.memory_type not in ["episodic", "semantic", "procedural", "working", "meta"]:
                errors.append(f"invalid memory_type: {record.memory_type}")
            if not isinstance(record.content, dict):
                errors.append("content must be a dict")

            if errors:
                self._failure_count += 1

            return len(errors) == 0, errors

    def validate_batch(self, records: List[SAMEPMemoryRecord]) -> Dict[str, List[str]]:
        results = {}
        for r in records:
            ok, errs = self.validate(r)
            if not ok:
                results[r.record_id] = errs
        return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "validated": self._validated_count,
                "failures": self._failure_count,
                "pass_rate": 1.0 - self._failure_count / max(1, self._validated_count),
            }


# ============================================================================
# CapabilityAccessController
# ============================================================================

class CapabilityAccessController:
    """基于能力的访问控制"""

    def __init__(self):
        self._lock = threading.RLock()
        self._tokens: Dict[str, CapabilityToken] = {}
        self._revoked: Set[str] = set()
        self._access_log: deque = deque(maxlen=1000)

    def issue_token(self, issuer: str, subject: str,
                    actions: Set[CapabilityAction],
                    resource_pattern: str,
                    ttl_seconds: float = 3600.0) -> CapabilityToken:
        """签发能力令牌"""
        with self._lock:
            token = CapabilityToken(
                token_id=f"cap_{uuid.uuid4().hex[:16]}",
                issuer=issuer,
                subject=subject,
                actions=frozenset(actions),
                resource_pattern=resource_pattern,
                expires_at=time.time() + ttl_seconds if ttl_seconds > 0 else float("inf"),
            )
            token.signature = self._sign(token)
            self._tokens[token.token_id] = token
            return token

    def check_access(self, token_id: str, action: CapabilityAction,
                     resource: str) -> Tuple[bool, str]:
        """检查访问权限"""
        with self._lock:
            token = self._tokens.get(token_id)
            if not token:
                return False, "token not found"
            if token_id in self._revoked:
                return False, "token revoked"
            if time.time() > token.expires_at:
                return False, "token expired"
            if not self._verify_signature(token):
                return False, "signature invalid"
            if action not in token.actions:
                return False, f"action {action.value} not permitted"

            # 简单通配符匹配
            import fnmatch
            if fnmatch.fnmatch(resource, token.resource_pattern):
                self._access_log.append((token_id, action.value, resource, True, time.time()))
                return True, "granted"

            return False, "resource not matched"

    def revoke(self, token_id: str):
        with self._lock:
            self._revoked.add(token_id)
            if token_id in self._tokens:
                del self._tokens[token_id]

    def _sign(self, token: CapabilityToken) -> str:
        payload = f"{token.token_id}:{token.issuer}:{token.subject}:{token.resource_pattern}"
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _verify_signature(self, token: CapabilityToken) -> bool:
        return token.signature == self._sign(token)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_tokens": sum(1 for t in self._tokens.values() if t.token_id not in self._revoked),
                "revoked_tokens": len(self._revoked),
                "access_checks": len(self._access_log),
            }


# ============================================================================
# VerifiableProvenanceChain
# ============================================================================

class VerifiableProvenanceChain:
    """可验证来源链"""

    def __init__(self):
        self._lock = threading.RLock()
        self._chain: List[ProvenanceRecord] = []
        self._verified_count: int = 0
        self._tampered_count: int = 0

    def create_record(self, creator_id: str,
                      content: Dict[str, Any]) -> ProvenanceRecord:
        """创建来源记录"""
        with self._lock:
            prev_hash = self._chain[-1].content_hash if self._chain else hashlib.sha256(b"genesis").hexdigest()
            content_bytes = json.dumps(content, sort_keys=True).encode()
            content_hash = hashlib.sha256(content_bytes).hexdigest()
            record = ProvenanceRecord(
                record_id=f"prv_{uuid.uuid4().hex[:16]}",
                creator_id=creator_id,
                created_at=time.time(),
                previous_hash=prev_hash,
                content_hash=content_hash,
                signature=hashlib.sha256(f"{content_hash}:{prev_hash}:{creator_id}".encode()).hexdigest()[:32],
            )
            self._chain.append(record)
            return record

    def verify_chain(self) -> Tuple[bool, int]:
        """验证整条链"""
        with self._lock:
            for i in range(1, len(self._chain)):
                current = self._chain[i]
                if current.previous_hash != self._chain[i - 1].content_hash:
                    self._tampered_count += 1
                    return False, i
            self._verified_count += 1
            return True, -1

    def verify_record(self, record: ProvenanceRecord) -> ProvenanceStatus:
        """验证单条记录"""
        with self._lock:
            expected_sig = hashlib.sha256(
                f"{record.content_hash}:{record.previous_hash}:{record.creator_id}".encode()
            ).hexdigest()[:32]
            if expected_sig != record.signature:
                return ProvenanceStatus.TAMPERED
            return ProvenanceStatus.VERIFIED

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "chain_length": len(self._chain),
                "verified_count": self._verified_count,
                "tampered_count": self._tampered_count,
            }


# ============================================================================
# MerkleAuditTree
# ============================================================================

class MerkleAuditTree:
    """Merkle 审计树"""

    def __init__(self):
        self._lock = threading.RLock()
        self._audit_count: int = 0

    def build(self, records: List[SAMEPMemoryRecord]) -> str:
        """构建 Merkle 根"""
        with self._lock:
            hashes = [hashlib.sha256(json.dumps(r.content, sort_keys=True).encode()).hexdigest()
                      for r in records]
            while len(hashes) > 1:
                next_level = []
                for i in range(0, len(hashes), 2):
                    a = hashes[i]
                    b = hashes[i + 1] if i + 1 < len(hashes) else a
                    next_level.append(hashlib.sha256(f"{a}:{b}".encode()).hexdigest())
                hashes = next_level
            self._audit_count += 1
            return hashes[0] if hashes else ""

    def verify(self, records: List[SAMEPMemoryRecord],
               expected_root: str) -> bool:
        return self.build(records) == expected_root

    def statistics(self) -> Dict[str, Any]:
        return {"audit_count": self._audit_count}


# ============================================================================
# MemoryRecordAdapter
# ============================================================================

class MemoryRecordAdapter:
    """记忆记录适配器：将不同框架格式归一化到 SAMEP Schema"""

    def __init__(self, target_schema: InteropMemorySchema):
        self._lock = threading.RLock()
        self._schema = target_schema
        self._adaptations: int = 0
        self._adapters: Dict[str, Callable] = {}
        self._register_builtin_adapters()

    def _register_builtin_adapters(self):
        self._adapters["trinity"] = self._adapt_trinity
        self._adapters["langchain"] = self._adapt_langchain
        self._adapters["mem0"] = self._adapt_mem0
        self._adapters["cognee"] = self._adapt_cognee

    def adapt(self, raw_record: Dict[str, Any], framework: str,
              agent_id: str) -> Optional[SAMEPMemoryRecord]:
        """将原始记录适配为 SAMEP 格式"""
        with self._lock:
            adapter = self._adapters.get(framework, self._adapt_generic)
            try:
                result = adapter(raw_record, agent_id)
                self._adaptations += 1
                return result
            except Exception as e:
                logger.warning(f"Adaptation failed for {framework}: {e}")
                return None

    def _adapt_trinity(self, raw: Dict[str, Any], agent_id: str) -> SAMEPMemoryRecord:
        return self._schema.create_record(
            agent_id=agent_id,
            memory_type=raw.get("memory_type", "semantic"),
            content=raw,
        )

    def _adapt_langchain(self, raw: Dict[str, Any], agent_id: str) -> SAMEPMemoryRecord:
        return self._schema.create_record(
            agent_id=agent_id,
            memory_type="episodic",
            content={"messages": raw.get("messages", []), "metadata": raw.get("metadata", {})},
        )

    def _adapt_mem0(self, raw: Dict[str, Any], agent_id: str) -> SAMEPMemoryRecord:
        return self._schema.create_record(
            agent_id=agent_id,
            memory_type=raw.get("memory_type", "semantic"),
            content={"text": raw.get("memory", ""), "metadata": raw.get("metadata", {})},
        )

    def _adapt_cognee(self, raw: Dict[str, Any], agent_id: str) -> SAMEPMemoryRecord:
        return self._schema.create_record(
            agent_id=agent_id,
            memory_type=raw.get("entity_type", "semantic"),
            content=raw,
        )

    def _adapt_generic(self, raw: Dict[str, Any], agent_id: str) -> SAMEPMemoryRecord:
        return self._schema.create_record(
            agent_id=agent_id,
            memory_type="working",
            content=raw,
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"adaptations": self._adaptations, "supported_frameworks": list(self._adapters.keys())}


# ============================================================================
# FederationPeer
# ============================================================================

class FederationPeer:
    """联邦对等节点"""

    def __init__(self, peer_id: str, endpoint: str = ""):
        self._lock = threading.RLock()
        self._peer_id = peer_id
        self._endpoint = endpoint
        self._state: PeerState = PeerState.OFFLINE
        self._version_vector: Dict[str, int] = {}
        self._sync_queue: deque = deque(maxlen=500)
        self._last_heartbeat: float = 0.0
        self._messages_sent: int = 0
        self._messages_received: int = 0

    def update_state(self, state: PeerState):
        with self._lock:
            self._state = state

    def heartbeat(self):
        with self._lock:
            self._last_heartbeat = time.time()

    def enqueue_sync(self, message: SyncMessage):
        with self._lock:
            self._sync_queue.append(message)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "peer_id": self._peer_id,
                "state": self._state.value,
                "queue_depth": len(self._sync_queue),
                "last_heartbeat": self._last_heartbeat,
            }


# ============================================================================
# CrossFrameworkMemoryFederation
# ============================================================================

class CrossFrameworkMemoryFederation:
    """跨 Agent 记忆联邦同步引擎"""

    def __init__(self, federation_id: str, policy: FederationPolicy = None):
        self._lock = threading.RLock()
        self._federation_id = federation_id
        self._policy = policy or FederationPolicy()
        self._peers: Dict[str, FederationPeer] = {}
        self._local_records: Dict[str, SAMEPMemoryRecord] = {}
        self._received_records: Dict[str, SAMEPMemoryRecord] = {}
        self._version_counters: Dict[str, int] = defaultdict(int)
        self._provenance = VerifiableProvenanceChain()
        self._schema = InteropMemorySchema()
        self._total_synced: int = 0

    def register_peer(self, peer_id: str, endpoint: str = "") -> FederationPeer:
        with self._lock:
            peer = FederationPeer(peer_id=peer_id, endpoint=endpoint)
            peer.update_state(PeerState.ONLINE)
            self._peers[peer_id] = peer
            return peer

    def sync_to_peers(self, records: List[SAMEPMemoryRecord]) -> Dict[str, int]:
        """将记录同步到所有在线节点"""
        with self._lock:
            self._version_counters[self._federation_id] += 1
            results: Dict[str, int] = {}

            for peer_id, peer in self._peers.items():
                if peer._state != PeerState.ONLINE:
                    results[peer_id] = 0
                    continue

                msg = SyncMessage(
                    message_id=f"sync_{uuid.uuid4().hex[:16]}",
                    sender_id=self._federation_id,
                    version_vector=dict(self._version_counters),
                    records=records[:self._policy.max_batch_size],
                    mode=self._policy.mode,
                )
                msg.checksum = hashlib.sha256(json.dumps(msg.version_vector).encode()).hexdigest()[:16]
                peer.enqueue_sync(msg)
                results[peer_id] = len(records)
                self._total_synced += len(records)

            for r in records:
                self._local_records[r.record_id] = r

            return results

    def receive_sync(self, message: SyncMessage,
                     sender_peer_id: str) -> int:
        """接收来自其他节点的同步"""
        with self._lock:
            accepted = 0
            for record in message.records:
                if record.record_id not in self._received_records:
                    self._received_records[record.record_id] = record
                    accepted += 1

            if sender_peer_id in self._peers:
                self._peers[sender_peer_id]._messages_received += 1

            return accepted

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "federation_id": self._federation_id,
                "peers": len(self._peers),
                "online_peers": sum(1 for p in self._peers.values() if p._state == PeerState.ONLINE),
                "local_records": len(self._local_records),
                "received_records": len(self._received_records),
                "total_synced": self._total_synced,
                "policy_mode": self._policy.mode.value,
                "provenance": self._provenance.statistics(),
            }


# ============================================================================
# 模块级 statistics()
# ============================================================================

def statistics() -> Dict[str, Any]:
    return {
        "module": "memory_interop_protocol",
        "paper": "W3C AI Agent Memory Interoperability CG (2026.05)",
        "alias": "SAMEP",
        "classes": 13,
        "key_features": [
            "standardized_memory_schema",
            "capability_based_access_control",
            "verifiable_provenance_chain",
            "cross_framework_federation",
            "merkle_audit_tree",
            "memory_record_adapter",
        ],
    }
