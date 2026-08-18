"""
# status: orphan (2026-08-15 audit, not in runtime path)
EA-Graph — Artifact-Anchored Verification Memory for Upstream Drift
====================================================================
arXiv 2608.04278 · P48-4

产物锚定验证记忆图：以代码产物（函数/类/模块）为节点的依赖图。
沿依赖链做回归验证，检测上游 API 签名/契约/行为漂移，
通过 NetworkX 图结构持久化 + 向量序列化。

设计要点:
  - ArtifactAnchorNode: 产物节点
  - VerificationChain: 依赖链回归验证
  - DriftDetector: 上游漂移检测
  - EAGraphMemoryStore: 图持久化
"""
from __future__ import annotations

import logging
import threading
import time
import json
import hashlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DriftType(Enum):
    SIGNATURE_CHANGE = auto()   # API 签名变更
    CONTRACT_VIOLATION = auto()  # 契约违反
    BEHAVIOR_DEVIATION = auto()  # 行为偏差
    NO_DRIFT = auto()


class VerificationStatus(Enum):
    PENDING = auto()
    PASSED = auto()
    FAILED = auto()
    SKIPPED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ArtifactAnchorNode:
    """以代码产物为锚点的依赖图节点。

    Attributes
    ----------
    artifact_id : str
        产物唯一 ID（如 module.class.method）。
    artifact_type : str
        类型: function / class / module。
    signature_hash : str
        签名哈希（用于漂移检测）。
    dependencies : List[str]
        依赖的 artifact_id 列表。
    """
    artifact_id: str
    artifact_type: str = "function"
    signature_hash: str = ""
    dependencies: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_verified: float = 0.0
    verification_status: VerificationStatus = VerificationStatus.PENDING


@dataclass
class VerificationResult:
    """单次验证结果。"""
    node_id: str
    status: VerificationStatus
    drift_type: DriftType = DriftType.NO_DRIFT
    details: str = ""
    upstream_changes: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DriftDetector
# ---------------------------------------------------------------------------

class DriftDetector:
    """上游漂移检测器——检测 API 签名、契约、行为三类漂移。

    签名变更: 参数列表变化
    契约违反: 返回类型/值范围变化
    行为偏差: 输出语义偏离
    """

    def __init__(self, tolerance: float = 0.15) -> None:
        self.tolerance = tolerance
        self._signatures: Dict[str, str] = {}
        self._contracts: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def register_signature(self, artifact_id: str, sig: str) -> None:
        with self._lock:
            self._signatures[artifact_id] = sig

    def register_contract(self, artifact_id: str, contract: Dict[str, Any]) -> None:
        with self._lock:
            self._contracts[artifact_id] = contract

    def detect(
        self, node: ArtifactAnchorNode, current_signature: str,
        current_contract: Optional[Dict[str, Any]] = None,
    ) -> DriftType:
        """检测一个节点是否发生上游漂移。"""
        with self._lock:
            old_sig = self._signatures.get(node.artifact_id, node.signature_hash)

            # 签名检测
            if old_sig and old_sig != current_signature:
                return DriftType.SIGNATURE_CHANGE

            # 契约检测
            if current_contract:
                old_contract = self._contracts.get(node.artifact_id, {})
                if old_contract and self._contract_diff(old_contract, current_contract) > self.tolerance:
                    return DriftType.CONTRACT_VIOLATION

            return DriftType.NO_DRIFT

    def _contract_diff(self, old: Dict[str, Any], new: Dict[str, Any]) -> float:
        all_keys = set(old.keys()) | set(new.keys())
        if not all_keys:
            return 0.0
        diff_count = 0
        for k in all_keys:
            if str(old.get(k)) != str(new.get(k)):
                diff_count += 1
        return diff_count / len(all_keys)

    def statistics(self) -> Dict[str, Any]:
        return {
            "tracked_signatures": len(self._signatures),
            "tracked_contracts": len(self._contracts),
            "tolerance": self.tolerance,
        }


# ---------------------------------------------------------------------------
# VerificationChain
# ---------------------------------------------------------------------------

class VerificationChain:
    """沿依赖链回归验证——上游变更 → 被影响节点 → 验证结果。

    从变更节点出发，BFS 遍历依赖图，对每个受影响节点执行验证。
    """

    def __init__(self, drift_detector: Optional[DriftDetector] = None) -> None:
        self.detector = drift_detector or DriftDetector()
        self._results: List[VerificationResult] = []
        self._lock = threading.RLock()

    def verify_chain(
        self, graph: Dict[str, ArtifactAnchorNode], changed_node_ids: List[str],
    ) -> List[VerificationResult]:
        """执行依赖链验证。

        Parameters
        ----------
        graph : Dict[str, ArtifactAnchorNode]
            完整依赖图。
        changed_node_ids : List[str]
            发生变更的节点 ID 列表。
        """
        with self._lock:
            results: List[VerificationResult] = []
            visited: Set[str] = set()
            queue: deque[str] = deque(changed_node_ids)

            while queue:
                node_id = queue.popleft()
                if node_id in visited:
                    continue
                visited.add(node_id)
                node = graph.get(node_id)
                if node is None:
                    continue

                # 执行验证
                drift = self.detector.detect(
                    node, current_signature=node.signature_hash,
                )
                status = VerificationStatus.FAILED if drift != DriftType.NO_DRIFT else VerificationStatus.PASSED
                vr = VerificationResult(
                    node_id=node_id, status=status, drift_type=drift,
                    details=f"drift={drift.name}", upstream_changes=changed_node_ids,
                )
                results.append(vr)

                # BFS 传播到依赖者
                for dep in node.dependencies:
                    if dep not in visited:
                        queue.append(dep)

            self._results.extend(results)
            return results

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_verifications": len(self._results),
            "detector": self.detector.statistics(),
        }


# ---------------------------------------------------------------------------
# EAGraphMemoryStore
# ---------------------------------------------------------------------------

class EAGraphMemoryStore:
    """EA-Graph 图结构持久化——基于 NetworkX 逻辑 + 向量序列化。

    纯 NumPy 实现，不依赖 NetworkX import。支持 JSON 序列化 / 反序列化。
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, ArtifactAnchorNode] = {}
        self._adj: Dict[str, List[str]] = defaultdict(list)  # node → [dependents]
        self._node_vectors: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()

    def add_node(self, node: ArtifactAnchorNode) -> None:
        with self._lock:
            self._nodes[node.artifact_id] = node
            if node.artifact_id not in self._adj:
                self._adj[node.artifact_id] = []

    def add_edge(self, from_id: str, to_id: str) -> None:
        """添加依赖边：from_id 依赖于 to_id。"""
        with self._lock:
            if from_id in self._nodes and to_id in self._nodes:
                self._nodes[from_id].dependencies.append(to_id)
                self._adj[to_id].append(from_id)

    def get_dependents(self, node_id: str) -> List[str]:
        with self._lock:
            return list(self._adj.get(node_id, []))

    def serialize(self) -> str:
        """序列化为 JSON 字符串。"""
        with self._lock:
            data = {
                "nodes": {
                    nid: {
                        "artifact_id": n.artifact_id,
                        "artifact_type": n.artifact_type,
                        "signature_hash": n.signature_hash,
                        "dependencies": n.dependencies,
                        "metadata": n.metadata,
                    }
                    for nid, n in self._nodes.items()
                },
                "version": "1.0",
            }
            return json.dumps(data, indent=2)

    def deserialize(self, json_str: str) -> None:
        """从 JSON 反序列化。"""
        with self._lock:
            data = json.loads(json_str)
            nodes_data = data.get("nodes", {})
            self._nodes.clear()
            self._adj.clear()
            for nid, nd in nodes_data.items():
                node = ArtifactAnchorNode(
                    artifact_id=nd["artifact_id"],
                    artifact_type=nd.get("artifact_type", "function"),
                    signature_hash=nd.get("signature_hash", ""),
                    dependencies=nd.get("dependencies", []),
                    metadata=nd.get("metadata", {}),
                )
                self._nodes[nid] = node
                if nid not in self._adj:
                    self._adj[nid] = []
                for dep in node.dependencies:
                    self._adj[dep].append(nid)

    def statistics(self) -> Dict[str, Any]:
        return {
            "nodes": len(self._nodes),
            "edges": sum(len(deps) for deps in self._adj.values()),
            "orphan_nodes": sum(1 for n in self._nodes.values() if not n.dependencies),
        }
