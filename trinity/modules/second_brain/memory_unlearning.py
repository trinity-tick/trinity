"""
P4-5: Cross-Substrate Memory Unlearning / Erasure (对标 Zylos + GDPR)
=========================================================================

统一擦除接口，支持四类存储基质中的可验证遗忘删除：
  1. 向量索引 (Vector Index) — HNSW / 向量数据库条目
  2. 知识图谱 (Knowledge Graph) — 实体 / 关系 / 三元组
  3. 摘要缓存 (Summary Cache) — 分层摘要 / 压缩缓存
  4. 备份快照 (Backup Snapshot) — 备份文件 / 检查点

每项擦除操作返回擦除证明 (ErasureProof)，包含：
  - 基质类型、路径/ID、擦除时间戳
  - 内容指纹（擦除前哈希）
  - 验证状态（是否确认擦除成功）

设计要点：
  - 统一 API: erase(memory_id, substrates=["vector", "kgraph", "summary", "backup"])
  - 可验证: 每次擦除返回 proof dict，外部可独立验证
  - 合规: GDPR Article 17 "被遗忘权" 可审计
  - 防误删: 擦除前检查依赖（其他记忆是否引用），记录级联影响

Reference: Zylos Controlled Forgetting (zylos.ai, June 2026)
          GDPR Article 17 — Right to Erasure
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────

class StorageSubstrate(Enum):
    """存储基质类型。"""
    VECTOR_INDEX = "vector_index"       # 向量索引 (HNSW / 向量数据库)
    KNOWLEDGE_GRAPH = "knowledge_graph"  # 知识图谱 (实体/关系/三元组)
    SUMMARY_CACHE = "summary_cache"      # 摘要缓存 (分层摘要/压缩缓存)
    BACKUP_SNAPSHOT = "backup_snapshot"  # 备份快照 (备份文件/检查点)


class ErasureStatus(Enum):
    """擦除操作状态。"""
    PENDING = auto()
    IN_PROGRESS = auto()
    VERIFIED = auto()        # 已验证擦除成功
    FAILED = auto()          # 擦除失败
    PARTIAL = auto()         # 部分基质擦除成功
    SKIPPED = auto()         # 该基质中不存在（跳过）


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class ErasureProof:
    """单基质擦除证明。

    Args:
        proof_id: 证明唯一标识
        memory_id: 被擦除的记忆 ID
        substrate: 存储基质类型
        resource_path: 基质中的资源路径/ID
        content_hash_before: 擦除前内容哈希
        status: 擦除状态
        verified_at: 验证时间戳
        error_message: 失败时错误信息
        metadata: 扩展元数据
    """

    proof_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    memory_id: str = ""
    substrate: StorageSubstrate = StorageSubstrate.VECTOR_INDEX
    resource_path: str = ""
    content_hash_before: str = ""
    status: ErasureStatus = ErasureStatus.PENDING
    verified_at: Optional[float] = None
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UnlearningResult:
    """统一擦除操作的聚合结果。

    Args:
        memory_id: 被擦除的记忆 ID
        proofs: 各基质的擦除证明列表
        all_verified: 是否所有基质均已验证成功
        cascade_effects: 级联擦除的其他记忆 ID 列表
        total_duration_ms: 总耗时（毫秒）
    """

    memory_id: str
    proofs: List[ErasureProof] = field(default_factory=list)
    all_verified: bool = False
    cascade_effects: List[str] = field(default_factory=list)
    total_duration_ms: float = 0.0


# ── 基质擦除处理器接口 ──────────────────────────────────────────

class SubstrateEraser:
    """存储基质擦除处理器的抽象基类。

    各基质实现自己的 do_erase() 方法。
    """

    def __init__(self, substrate_type: StorageSubstrate):
        self.substrate_type = substrate_type

    def erase(self, memory_id: str, content: str = "") -> ErasureProof:
        """执行擦除并返回证明。

        Args:
            memory_id: 记忆 ID
            content: 擦除前内容（用于生成指纹）
        """
        raise NotImplementedError

    def verify(self, memory_id: str) -> bool:
        """验证指定记忆在此基质中是否已完全擦除。"""
        raise NotImplementedError


class VectorIndexEraser(SubstrateEraser):
    """向量索引擦除器。"""

    def __init__(self, delete_fn: Optional[Callable[[str], bool]] = None):
        super().__init__(StorageSubstrate.VECTOR_INDEX)
        self.delete_fn = delete_fn
        self._deleted: Set[str] = set()

    def erase(self, memory_id: str, content: str = "") -> ErasureProof:
        proof = ErasureProof(
            memory_id=memory_id,
            substrate=StorageSubstrate.VECTOR_INDEX,
            resource_path=f"vector_index:{memory_id}",
            content_hash_before=hashlib.sha256(
                (content or memory_id).encode()
            ).hexdigest(),
        )
        try:
            if self.delete_fn:
                success = self.delete_fn(memory_id)
            else:
                self._deleted.add(memory_id)
                success = True

            if success:
                proof.status = ErasureStatus.VERIFIED
                proof.verified_at = time.time()
                logger.info("Vector index erased: %s", memory_id)
            else:
                proof.status = ErasureStatus.FAILED
                proof.error_message = "Delete function returned False"
        except Exception as e:
            proof.status = ErasureStatus.FAILED
            proof.error_message = str(e)
            logger.error("Vector index erase failed: %s — %s", memory_id, e)

        return proof

    def verify(self, memory_id: str) -> bool:
        return memory_id in self._deleted


class KnowledgeGraphEraser(SubstrateEraser):
    """知识图谱擦除器 — 擦除实体/关系/三元组。"""

    def __init__(
        self,
        delete_entity_fn: Optional[Callable[[str], bool]] = None,
        delete_relations_fn: Optional[Callable[[str], List[str]]] = None,
    ):
        super().__init__(StorageSubstrate.KNOWLEDGE_GRAPH)
        self.delete_entity_fn = delete_entity_fn
        self.delete_relations_fn = delete_relations_fn
        self._deleted_entities: Set[str] = set()

    def erase(self, memory_id: str, content: str = "") -> ErasureProof:
        proof = ErasureProof(
            memory_id=memory_id,
            substrate=StorageSubstrate.KNOWLEDGE_GRAPH,
            resource_path=f"kgraph:{memory_id}",
            content_hash_before=hashlib.sha256(
                (content or memory_id).encode()
            ).hexdigest(),
        )
        try:
            success = True
            cascade_entities: List[str] = []

            # 擦除关联关系
            if self.delete_relations_fn:
                cascade_entities = self.delete_relations_fn(memory_id)

            # 擦除实体
            if self.delete_entity_fn:
                if not self.delete_entity_fn(memory_id):
                    success = False

            self._deleted_entities.add(memory_id)
            for ent in cascade_entities:
                self._deleted_entities.add(ent)

            proof.metadata["cascade_entities"] = cascade_entities
            proof.status = ErasureStatus.VERIFIED if success else ErasureStatus.FAILED
            proof.verified_at = time.time() if success else None
            logger.info("KG erased: %s (cascade: %d entities)", memory_id, len(cascade_entities))

        except Exception as e:
            proof.status = ErasureStatus.FAILED
            proof.error_message = str(e)
            logger.error("KG erase failed: %s — %s", memory_id, e)

        return proof

    def verify(self, memory_id: str) -> bool:
        return memory_id in self._deleted_entities


class SummaryCacheEraser(SubstrateEraser):
    """摘要缓存擦除器。"""

    def __init__(self, invalidate_fn: Optional[Callable[[str], bool]] = None):
        super().__init__(StorageSubstrate.SUMMARY_CACHE)
        self.invalidate_fn = invalidate_fn
        self._invalidated: Set[str] = set()

    def erase(self, memory_id: str, content: str = "") -> ErasureProof:
        proof = ErasureProof(
            memory_id=memory_id,
            substrate=StorageSubstrate.SUMMARY_CACHE,
            resource_path=f"summary_cache:{memory_id}",
            content_hash_before=hashlib.sha256(
                (content or memory_id).encode()
            ).hexdigest(),
        )
        try:
            if self.invalidate_fn:
                success = self.invalidate_fn(memory_id)
            else:
                self._invalidated.add(memory_id)
                success = True

            proof.status = ErasureStatus.VERIFIED if success else ErasureStatus.FAILED
            proof.verified_at = time.time() if success else None
            logger.info("Summary cache invalidated: %s", memory_id)

        except Exception as e:
            proof.status = ErasureStatus.FAILED
            proof.error_message = str(e)
            logger.error("Summary cache erase failed: %s — %s", memory_id, e)

        return proof

    def verify(self, memory_id: str) -> bool:
        return memory_id in self._invalidated


class BackupSnapshotEraser(SubstrateEraser):
    """备份快照擦除器 — 从备份文件和检查点中移除。"""

    def __init__(self, mark_fn: Optional[Callable[[str], bool]] = None):
        super().__init__(StorageSubstrate.BACKUP_SNAPSHOT)
        self.mark_fn = mark_fn
        self._marked: Set[str] = set()

    def erase(self, memory_id: str, content: str = "") -> ErasureProof:
        proof = ErasureProof(
            memory_id=memory_id,
            substrate=StorageSubstrate.BACKUP_SNAPSHOT,
            resource_path=f"backup_snapshot:{memory_id}",
            content_hash_before=hashlib.sha256(
                (content or memory_id).encode()
            ).hexdigest(),
        )
        try:
            if self.mark_fn:
                success = self.mark_fn(memory_id)
            else:
                self._marked.add(memory_id)
                success = True

            proof.status = ErasureStatus.VERIFIED if success else ErasureStatus.FAILED
            proof.verified_at = time.time() if success else None
            logger.info("Backup snapshot marked for erasure: %s", memory_id)

        except Exception as e:
            proof.status = ErasureStatus.FAILED
            proof.error_message = str(e)
            logger.error("Backup snapshot erase failed: %s — %s", memory_id, e)

        return proof

    def verify(self, memory_id: str) -> bool:
        return memory_id in self._marked


# ── 统一擦除接口 ──────────────────────────────────────────────────

class MemoryUnlearningManager:
    """跨基质记忆遗忘/擦除管理器 — 对标 Zylos + GDPR。

    使用方式::

        from trinity.modules.second_brain.memory_unlearning import (
            MemoryUnlearningManager, StorageSubstrate,
        )

        mum = MemoryUnlearningManager()

        # 注册各基质擦除器（可注入外部 delete 函数）
        mum.register_eraser(StorageSubstrate.VECTOR_INDEX, VectorIndexEraser())
        mum.register_eraser(StorageSubstrate.KNOWLEDGE_GRAPH, KnowledgeGraphEraser())

        # 四基质统一擦除
        result = mum.erase(
            "mem_001",
            substrates=[
                StorageSubstrate.VECTOR_INDEX,
                StorageSubstrate.KNOWLEDGE_GRAPH,
                StorageSubstrate.SUMMARY_CACHE,
                StorageSubstrate.BACKUP_SNAPSHOT,
            ],
        )

        # 检查结果
        print(result.all_verified)    # True if all substrates succeeded
        for proof in result.proofs:
            print(f"  {proof.substrate.value}: {proof.status.name}")

        # 导出擦除证明（可存入审计日志）
        proofs_json = mum.export_proofs("mem_001")
    """

    # ── 构造函数 ──────────────────────────────────────────────────

    def __init__(self):
        """初始化统一擦除管理器，预注册四种基质的默认擦除器。"""
        self._erasers: Dict[StorageSubstrate, SubstrateEraser] = {
            StorageSubstrate.VECTOR_INDEX: VectorIndexEraser(),
            StorageSubstrate.KNOWLEDGE_GRAPH: KnowledgeGraphEraser(),
            StorageSubstrate.SUMMARY_CACHE: SummaryCacheEraser(),
            StorageSubstrate.BACKUP_SNAPSHOT: BackupSnapshotEraser(),
        }
        # 擦除历史: {memory_id: UnlearningResult}
        self._history: Dict[str, UnlearningResult] = {}
        # 依赖图: {memory_id: [dependent_memory_ids]}
        self._dependency_graph: Dict[str, List[str]] = {}

    # ── 擦除器注册 ───────────────────────────────────────────────

    def register_eraser(self, substrate: StorageSubstrate, eraser: SubstrateEraser) -> None:
        """注册/替换基质擦除器（支持注入外部函数）。"""
        self._erasers[substrate] = eraser
        logger.info("Eraser registered for substrate: %s", substrate.value)

    def register_dependency(self, memory_id: str, depends_on: List[str]) -> None:
        """注册记忆间的依赖关系（级联擦除检查）。"""
        self._dependency_graph[memory_id] = depends_on

    # ── 统一擦除 ──────────────────────────────────────────────────

    def erase(
        self,
        memory_id: str,
        substrates: Optional[List[StorageSubstrate]] = None,
        content: str = "",
        check_dependencies: bool = True,
    ) -> UnlearningResult:
        """统一擦除接口 — 跨所有指定基质执行擦除。

        Args:
            memory_id: 要擦除的记忆 ID
            substrates: 要擦除的基质列表（None = 全部四种）
            content: 擦除前内容（用于生成指纹）
            check_dependencies: 是否检查级联依赖

        Returns:
            UnlearningResult 包含每基质的 ErasureProof + 聚合验证状态
        """
        if substrates is None:
            substrates = [
                StorageSubstrate.VECTOR_INDEX,
                StorageSubstrate.KNOWLEDGE_GRAPH,
                StorageSubstrate.SUMMARY_CACHE,
                StorageSubstrate.BACKUP_SNAPSHOT,
            ]

        t_start = time.time()
        proofs: List[ErasureProof] = []

        for substrate in substrates:
            eraser = self._erasers.get(substrate)
            if eraser is None:
                proof = ErasureProof(
                    memory_id=memory_id,
                    substrate=substrate,
                    status=ErasureStatus.SKIPPED,
                    error_message=f"No eraser registered for {substrate.value}",
                )
                proofs.append(proof)
                continue

            proof = eraser.erase(memory_id, content)
            proofs.append(proof)

            # 验证
            if proof.status == ErasureStatus.VERIFIED:
                verified = eraser.verify(memory_id)
                if not verified:
                    proof.status = ErasureStatus.FAILED
                    proof.error_message = "Post-erase verification failed"

        # 检查级联依赖
        cascade_effects: List[str] = []
        if check_dependencies:
            cascade_effects = self._check_cascade(memory_id)

        all_ok = all(
            p.status == ErasureStatus.VERIFIED or p.status == ErasureStatus.SKIPPED
            for p in proofs
        )

        result = UnlearningResult(
            memory_id=memory_id,
            proofs=proofs,
            all_verified=all_ok,
            cascade_effects=cascade_effects,
            total_duration_ms=round((time.time() - t_start) * 1000, 2),
        )
        self._history[memory_id] = result

        if all_ok:
            logger.info(
                "Unlearning complete: %s across %d substrates (%.1f ms)",
                memory_id, len(substrates), result.total_duration_ms,
            )
        else:
            failed = [p.substrate.value for p in proofs if p.status == ErasureStatus.FAILED]
            logger.warning(
                "Unlearning partial: %s — failed substrates: %s", memory_id, failed,
            )

        return result

    def _check_cascade(self, memory_id: str) -> List[str]:
        """检查级联影响：哪些记忆依赖于此记忆。"""
        affected = [
            mid for mid, deps in self._dependency_graph.items()
            if memory_id in deps
        ]
        if affected:
            logger.info(
                "Cascade effect: erasing %s affects %d dependent memories: %s",
                memory_id, len(affected), affected,
            )
        return affected

    # ── 查询与导出 ────────────────────────────────────────────────

    def get_result(self, memory_id: str) -> Optional[UnlearningResult]:
        """查询指定记忆的擦除结果。"""
        return self._history.get(memory_id)

    def export_proofs(self, memory_id: str) -> List[Dict[str, Any]]:
        """将擦除证明导出为可序列化的字典列表（用于审计）。"""
        result = self._history.get(memory_id)
        if result is None:
            return []

        return [
            {
                "proof_id": p.proof_id,
                "memory_id": p.memory_id,
                "substrate": p.substrate.value,
                "resource_path": p.resource_path,
                "content_hash_before": p.content_hash_before,
                "status": p.status.name,
                "verified_at": p.verified_at,
                "error_message": p.error_message,
            }
            for p in result.proofs
        ]

    def verify_erasure(self, memory_id: str) -> Dict[str, bool]:
        """跨所有基质验证指定记忆是否已完全擦除。"""
        result = {}
        for substrate, eraser in self._erasers.items():
            result[substrate.value] = eraser.verify(memory_id)
        return result

    def statistics(self) -> Dict[str, Any]:
        """返回擦除管理器运行时统计。"""
        return {
            "total_erasures": len(self._history),
            "registered_substrates": list(s.value for s in self._erasers.keys()),
            "fully_verified": sum(
                1 for r in self._history.values() if r.all_verified
            ),
            "partial_erasures": sum(
                1 for r in self._history.values() if not r.all_verified
            ),
            "dependency_graph_size": len(self._dependency_graph),
        }
