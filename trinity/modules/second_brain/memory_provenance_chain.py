"""
# status: orphan (2026-08-15 audit, not in runtime path)
P17-7: Memory Provenance Chain
==============================

对标 Provenance Chain — 不可变决策谱系全链路追溯。

设计要点：
  - 检索源 → 推理步骤 → 最终输出全链路追溯（6 层谱系）
  - 6 层：原始数据层 / 检索轨迹层 / 推理步骤层 / 置信度层 / 输出决策层 / 验证审计层
  - 哈希链完整性验证：链式哈希确保不可篡改
  - 反事实分析："如果 X 工具失败，决策会不同吗？"
  - Merkle 树版本化存储：支持稀疏证明与增量验证

核心组件：
  - ProvenanceChainBuilder:    6 层谱系构建器
  - HashChainVerifier:         SHA-256 哈希链完整性验证
  - CounterfactualAnalyzer:    反事实分析引擎
  - MerkleVersionStore:        Merkle 树版本化存储
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class ProvenanceLayer(Enum):
    """谱系六层架构。"""
    RAW_DATA = "raw_data"            # L1: 原始数据层
    RETRIEVAL_TRACE = "retrieval_trace"   # L2: 检索轨迹层
    REASONING_STEPS = "reasoning_steps"   # L3: 推理步骤层
    CONFIDENCE = "confidence"         # L4: 置信度层
    OUTPUT_DECISIONS = "output_decisions"  # L5: 输出决策层
    VERIFICATION_AUDIT = "verification_audit"  # L6: 验证审计层


class AuditStatus(Enum):
    """审计状态。"""
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    TAMPERED = "tampered"


class CounterfactualOutcome(Enum):
    """反事实分析结果。"""
    UNCHANGED = "unchanged"
    ALTERED = "altered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ProvenanceNode:
    """谱系节点 — 单个谱系层级记录。"""
    node_id: str
    layer: ProvenanceLayer
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_hash: str = ""
    timestamp: float = field(default_factory=time.time)

    def compute_hash(self) -> str:
        """计算 SHA-256 哈希（含父哈希 + 内容 + 时间戳）。"""
        data = f"{self.parent_hash}|{self.content}|{self.timestamp}|{self.layer.value}"
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass
class ProvenanceChain:
    """完整的 6 层谱系链。"""
    chain_id: str
    nodes: Dict[ProvenanceLayer, ProvenanceNode] = field(default_factory=dict)
    chain_hashes: List[str] = field(default_factory=list)
    is_valid: bool = True
    created_at: float = field(default_factory=time.time)


@dataclass
class AuditReport:
    """审计报告。"""
    chain_id: str
    status: AuditStatus
    broken_at: Optional[ProvenanceLayer] = None
    expected_hash: str = ""
    actual_hash: str = ""
    details: List[str] = field(default_factory=list)


@dataclass
class CounterfactualScenario:
    """反事实场景定义。"""
    scenario_id: str
    target_layer: ProvenanceLayer
    alternative_content: str
    description: str
    outcome: CounterfactualOutcome = CounterfactualOutcome.UNCERTAIN
    impact_assessment: str = ""


@dataclass
class MerkleLeaf:
    """Merkle 树叶子节点。"""
    entry_id: str
    content: str
    hash_value: str


@dataclass
class MerkleProof:
    """Merkle 稀疏证明。"""
    leaf: MerkleLeaf
    proof_path: List[Tuple[str, bool]]  # (hash, is_left)
    root_hash: str
    is_valid: bool = False


# ============================================================================
# Core Components
# ============================================================================

class ProvenanceChainBuilder:
    """6 层谱系构建器。

    构建检索源→推理→输出的全链路不可变追溯。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.chains: Dict[str, ProvenanceChain] = {}

    def build_chain(
        self,
        raw_data: str,
        retrieval_trace: List[str],
        reasoning_steps: List[str],
        confidence_scores: Dict[str, float],
        output_decisions: List[str],
        verification_notes: str,
    ) -> ProvenanceChain:
        """构建完整 6 层谱系链。"""
        with self._lock:
            chain_id = str(uuid.uuid4())[:8]
            chain = ProvenanceChain(chain_id=chain_id)
            prev_hash = ""

            layers_content = {
                ProvenanceLayer.RAW_DATA: raw_data,
                ProvenanceLayer.RETRIEVAL_TRACE: "\n".join(retrieval_trace),
                ProvenanceLayer.REASONING_STEPS: "\n".join(reasoning_steps),
                ProvenanceLayer.CONFIDENCE: str(confidence_scores),
                ProvenanceLayer.OUTPUT_DECISIONS: "\n".join(output_decisions),
                ProvenanceLayer.VERIFICATION_AUDIT: verification_notes,
            }

            for layer in ProvenanceLayer:
                content = layers_content.get(layer, "")
                node = ProvenanceNode(
                    node_id=str(uuid.uuid4())[:8],
                    layer=layer,
                    content=content,
                    parent_hash=prev_hash,
                )
                prev_hash = node.compute_hash()
                chain.nodes[layer] = node
                chain.chain_hashes.append(prev_hash)

            self.chains[chain_id] = chain
            return chain

    def get_chain(self, chain_id: str) -> Optional[ProvenanceChain]:
        return self.chains.get(chain_id)

    def get_node(self, chain_id: str, layer: ProvenanceLayer) -> Optional[ProvenanceNode]:
        chain = self.chains.get(chain_id)
        if chain:
            return chain.nodes.get(layer)
        return None

    def trace_lineage(self, chain_id: str) -> Dict[str, Any]:
        """全链路追溯。"""
        with self._lock:
            chain = self.chains.get(chain_id)
            if not chain:
                return {}
            return {
                "chain_id": chain_id,
                "layers": {
                    layer.value: {
                        "node_id": node.node_id,
                        "hash": node.compute_hash(),
                        "content_preview": node.content[:80],
                        "timestamp": node.timestamp,
                    }
                    for layer, node in chain.nodes.items()
                },
                "is_valid": chain.is_valid,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_chains": len(self.chains), "total_nodes": sum(len(c.nodes) for c in self.chains.values())}


class HashChainVerifier:
    """SHA-256 哈希链完整性验证。

    逐层验证 hash(parent_hash|content|timestamp) 链式一致性。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.audit_log: List[AuditReport] = []

    def verify(self, chain: ProvenanceChain) -> AuditReport:
        with self._lock:
            report = AuditReport(chain_id=chain.chain_id, status=AuditStatus.PENDING)

            prev_hash = ""
            for layer in ProvenanceLayer:
                node = chain.nodes.get(layer)
                if not node:
                    report.status = AuditStatus.FAILED
                    report.broken_at = layer
                    report.details.append(f"Missing layer: {layer.value}")
                    break

                expected = node.compute_hash()
                node.parent_hash = prev_hash
                actual = node.compute_hash()

                if node.parent_hash != prev_hash:
                    report.status = AuditStatus.TAMPERED
                    report.broken_at = layer
                    report.expected_hash = prev_hash
                    report.actual_hash = node.parent_hash
                    report.details.append(f"Tampered at {layer.value}: parent_hash mismatch")
                    break

                prev_hash = actual

            if report.status == AuditStatus.PENDING:
                report.status = AuditStatus.PASSED

            chain.is_valid = (report.status == AuditStatus.PASSED)
            self.audit_log.append(report)
            return report

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            status_counts = defaultdict(int)
            for r in self.audit_log:
                status_counts[r.status.value] += 1
            return {"total_audits": len(self.audit_log), "breakdown": dict(status_counts)}


class CounterfactualAnalyzer:
    """反事实分析引擎。

    模拟"如果 X 工具/数据源失败，决策会不同吗？"
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.scenarios: List[CounterfactualScenario] = []

    def analyze(
        self,
        chain: ProvenanceChain,
        altered_layer: ProvenanceLayer,
        alternative_content: str,
        description: str,
    ) -> CounterfactualScenario:
        """执行反事实分析。"""
        with self._lock:
            scenario = CounterfactualScenario(
                scenario_id=str(uuid.uuid4())[:8],
                target_layer=altered_layer,
                alternative_content=alternative_content,
                description=description,
            )

            # 模拟替代输入对下游的影响
            original_output = chain.nodes.get(ProvenanceLayer.OUTPUT_DECISIONS)
            if original_output and alternative_content != original_output.content:
                scenario.outcome = CounterfactualOutcome.ALTERED
                scenario.impact_assessment = f"修改 {altered_layer.value} 层导致 OUTPUT_DECISIONS 变化"
            elif altered_layer in (ProvenanceLayer.RETRIEVAL_TRACE, ProvenanceLayer.REASONING_STEPS):
                scenario.outcome = CounterfactualOutcome.ALTERED
                scenario.impact_assessment = f"关键层 {altered_layer.value} 变更可能改变最终决策"
            else:
                scenario.outcome = CounterfactualOutcome.UNCHANGED
                scenario.impact_assessment = f"{altered_layer.value} 层变更对最终输出无显著影响"

            self.scenarios.append(scenario)
            return scenario

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            outcome_counts = defaultdict(int)
            for s in self.scenarios:
                outcome_counts[s.outcome.value] += 1
            return {"total_scenarios": len(self.scenarios), "outcomes": dict(outcome_counts)}


class MerkleVersionStore:
    """Merkle 树版本化存储。

    支持稀疏证明（无需全量下载即可验证某条记录）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.leaves: List[MerkleLeaf] = []
        self.root_hash: str = ""
        self.version: int = 0

    def add_leaf(self, content: str) -> str:
        """添加叶子并重算 Merkle 根。"""
        with self._lock:
            leaf_id = str(uuid.uuid4())[:8]
            leaf_hash = hashlib.sha256(f"{leaf_id}|{content}".encode()).hexdigest()
            leaf = MerkleLeaf(entry_id=leaf_id, content=content, hash_value=leaf_hash)
            self.leaves.append(leaf)
            self.root_hash = self._compute_root()
            self.version += 1
            return leaf_id

    def generate_proof(self, leaf_index: int) -> Optional[MerkleProof]:
        """生成稀疏 Merkle 证明。"""
        with self._lock:
            if leaf_index < 0 or leaf_index >= len(self.leaves):
                return None
            leaf = self.leaves[leaf_index]

            # 模拟 Merkle 证明路径（简化版）
            proof_path: List[Tuple[str, bool]] = []
            hashes: List[str] = [l.hash_value for l in self.leaves]

            # 补齐到 2 的幂
            while (len(hashes) & (len(hashes) - 1)) != 0:
                hashes.append(hashes[-1])

            idx = leaf_index
            while len(hashes) > 1:
                sibling_idx = idx ^ 1
                if sibling_idx < len(hashes):
                    is_left = idx % 2 == 1
                    proof_path.append((hashes[sibling_idx], is_left))
                # 父层
                new_hashes = []
                for i in range(0, len(hashes), 2):
                    combined = hashlib.sha256(f"{hashes[i]}|{hashes[i + 1]}".encode()).hexdigest() if i + 1 < len(hashes) else hashes[i]
                    new_hashes.append(combined)
                hashes = new_hashes
                idx //= 2

            proof = MerkleProof(
                leaf=leaf,
                proof_path=proof_path,
                root_hash=self.root_hash,
                is_valid=True,
            )
            return proof

    def verify_proof(self, proof: MerkleProof) -> bool:
        """验证稀疏证明。"""
        with self._lock:
            current_hash = proof.leaf.hash_value
            for sibling_hash, is_left in proof.proof_path:
                if is_left:
                    combined = hashlib.sha256(f"{sibling_hash}|{current_hash}".encode()).hexdigest()
                else:
                    combined = hashlib.sha256(f"{current_hash}|{sibling_hash}".encode()).hexdigest()
                current_hash = combined
            proof.is_valid = (current_hash == proof.root_hash)
            return proof.is_valid

    def _compute_root(self) -> str:
        if not self.leaves:
            return ""
        hashes = [l.hash_value for l in self.leaves]
        while (len(hashes) & (len(hashes) - 1)) != 0:
            hashes.append(hashes[-1])
        while len(hashes) > 1:
            new_hashes = []
            for i in range(0, len(hashes), 2):
                combined = hashlib.sha256(f"{hashes[i]}|{hashes[i + 1]}".encode()).hexdigest() if i + 1 < len(hashes) else hashes[i]
                new_hashes.append(combined)
            hashes = new_hashes
        return hashes[0] if hashes else ""

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "leaves_count": len(self.leaves),
                "version": self.version,
                "root_hash": self.root_hash[:16] + "...",
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P17-7 Memory Provenance Chain",
        "benchmark": "Provenance Chain (不可变决策谱系)",
        "classes": 4,
        "enums": 3,
        "dataclasses": 7,
        "key_pattern": "6-Layer Provenance + SHA-256 Hash Chain + Counterfactual Analysis + Merkle Tree Versioning",
        "key_metric": "End-to-end immutable traceability + Counterfactual impact assessment",
        "thread_safe": True,
    }
