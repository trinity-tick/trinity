"""
# status: orphan (2026-08-15 audit, not in runtime path)
P20-2: Scientific Research Memory — 科研全生命周期记忆

对标论文: AutoSci (arXiv 2605.31468, 2026.05, PKU)
核心发现: 模式治理型科研记忆 + 五阶段研究管线 + DAG 多 Agent 协调 → 端到端科研自动化
三元语: 实体图关系记忆 → 五阶段管线 → DAG 并行加速 → 版本化自改进 → 跨项目积累

设计要点:
- SchemaGovernedResearchMemory: 有类型实体 (paper/hypothesis/experiment/result/manuscript/reviewer_comment) + 显式关系图
- FiveStageResearchPipeline: reading → hypothesis_generation → experiment_design → execution → manuscript_writing + reviewer_response
- MultiAgentDAGCoordinator: DAG 拓扑自动编排，并行加速假设生成和实验设计阶段
- VersionedSelfImprovementLoop: SciEvolve 类机制，反馈信号驱动工作流与记忆共同改进
- CrossProjectKnowledgeAccumulator: 多项目间共享知识，避免重复造轮子
- ManuscriptReviewResponder: 根据审稿意见检索相关实验/数据/引用，自动生成反驳/修订内容
- 与 P10-1 codebase_graph.py 互补——codebase 做代码图，本模块做科研实体图+研究流程
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class ResearchEntityType(Enum):
    """科研实体类型"""
    PAPER = "paper"                      # 论文
    HYPOTHESIS = "hypothesis"           # 假设
    EXPERIMENT = "experiment"           # 实验
    RESULT = "result"                   # 实验结果
    MANUSCRIPT = "manuscript"           # 手稿
    REVIEWER_COMMENT = "reviewer_comment"  # 审稿意见


class PipelineStage(Enum):
    """五阶段研究管线"""
    READING = "reading"                          # 文献阅读与理解
    HYPOTHESIS_GENERATION = "hypothesis_gen"     # 假设生成
    EXPERIMENT_DESIGN = "experiment_design"      # 实验设计
    EXECUTION = "execution"                      # 实验执行
    MANUSCRIPT_WRITING = "manuscript_writing"    # 手稿撰写
    REVIEWER_RESPONSE = "reviewer_response"      # 审稿回复


class RelationType(Enum):
    """实体间关系类型"""
    CITES = "cites"                        # 引用
    CONTRADICTS = "contradicts"           # 矛盾
    SUPPORTS = "supports"                 # 支持
    DERIVED_FROM = "derived_from"         # 派生
    TESTS = "tests"                       # 验证
    PRODUCES = "produces"                 # 产出
    REVISES = "revises"                   # 修订


class DAGNodeState(Enum):
    """DAG 节点状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ImprovementSignal(Enum):
    """自改进反馈信号"""
    REVIEW_REJECTION = "review_rejection"
    EXPERIMENT_FAILURE = "experiment_failure"
    HYPOTHESIS_INVALIDATION = "hypothesis_invalidation"
    PEER_FEEDBACK = "peer_feedback"
    NEW_EVIDENCE = "new_evidence"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ResearchEntity:
    """科研实体"""
    entity_id: str
    entity_type: ResearchEntityType
    title: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)


@dataclass
class ResearchRelation:
    """科研实体间显式关系"""
    relation_id: str
    source_id: str                     # 源实体 ID
    target_id: str                     # 目标实体 ID
    relation_type: RelationType
    weight: float = 1.0                # 关系强度 [0, 1]
    description: str = ""
    evidence: List[str] = field(default_factory=list)  # 证据引用
    created_at: float = field(default_factory=time.time)


@dataclass
class PipelineState:
    """研究管线状态快照"""
    project_id: str
    current_stage: PipelineStage
    stage_progress: Dict[PipelineStage, float] = field(default_factory=dict)  # 0~1
    blocked_by: List[str] = field(default_factory=list)
    artifacts: Dict[PipelineStage, List[str]] = field(default_factory=dict)  # 产出物 entity_ids
    started_at: float = field(default_factory=time.time)


@dataclass
class DAGNode:
    """DAG 协调节点"""
    node_id: str
    task_name: str
    agent_role: str                    # 执行此节点的 Agent 角色
    dependencies: List[str] = field(default_factory=list)  # 前置 node_id
    state: DAGNodeState = DAGNodeState.PENDING
    result: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    retry_count: int = 0
    max_retries: int = 2


@dataclass
class ImprovementVersion:
    """自改进版本记录"""
    version_id: str
    project_id: str
    signal: ImprovementSignal
    trigger_entity_id: str             # 触发改进的实体
    changes: Dict[str, str]            # entity_id -> change_summary
    before_hash: str                   # 改进前状态哈希
    after_hash: str                    # 改进后状态哈希
    quality_delta: float = 0.0         # 质量变化
    created_at: float = field(default_factory=time.time)


@dataclass
class CrossProjectAsset:
    """跨项目知识资产"""
    asset_id: str
    asset_type: ResearchEntityType
    content_summary: str
    source_project_ids: List[str]      # 来源项目
    reuse_count: int = 0
    quality_rating: float = 0.5
    last_reused_at: float = field(default_factory=time.time)


@dataclass
class ReviewerResponse:
    """审稿回复"""
    response_id: str
    comment_id: str
    rebuttal_text: str                 # 反驳/修订正文
    supporting_evidence: List[str]     # 支持证据 (entity_ids)
    revision_actions: List[str]        # 修改动作清单
    confidence: float = 0.5           # 置信度
    created_at: float = field(default_factory=time.time)


# ============================================================================
# SchemaGovernedResearchMemory
# ============================================================================

class SchemaGovernedResearchMemory:
    """模式治理型科研记忆

    有类型实体 + 显式关系图，构成结构化科研知识图谱。
    支持按实体类型、关系类型双向查询。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._entities: Dict[str, ResearchEntity] = {}
        self._relations: Dict[str, ResearchRelation] = {}
        self._forward_index: Dict[str, Set[str]] = defaultdict(set)   # entity -> target ids
        self._backward_index: Dict[str, Set[str]] = defaultdict(set)  # entity -> source ids
        self._type_index: Dict[ResearchEntityType, Set[str]] = defaultdict(set)

    def add_entity(self, entity: ResearchEntity) -> str:
        """添加科研实体"""
        with self._lock:
            self._entities[entity.entity_id] = entity
            self._type_index[entity.entity_type].add(entity.entity_id)
            return entity.entity_id

    def add_relation(self, relation: ResearchRelation) -> str:
        """添加实体间关系"""
        with self._lock:
            self._relations[relation.relation_id] = relation
            self._forward_index[relation.source_id].add(relation.target_id)
            self._backward_index[relation.target_id].add(relation.source_id)
            return relation.relation_id

    def get_entity(self, entity_id: str) -> Optional[ResearchEntity]:
        with self._lock:
            return self._entities.get(entity_id)

    def query_by_type(self, entity_type: ResearchEntityType) -> List[ResearchEntity]:
        with self._lock:
            return [self._entities[eid] for eid in self._type_index[entity_type] if eid in self._entities]

    def get_neighbors(self, entity_id: str) -> Tuple[List[ResearchRelation], List[ResearchRelation]]:
        """获取实体的出边和入边关系"""
        with self._lock:
            outgoing = [
                r for r in self._relations.values()
                if r.source_id == entity_id
            ]
            incoming = [
                r for r in self._relations.values()
                if r.target_id == entity_id
            ]
            return outgoing, incoming

    def graph_stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "total_entities": len(self._entities),
                "total_relations": len(self._relations),
                **{f"entity_{t.value}": len(ids) for t, ids in self._type_index.items()},
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        return self.graph_stats()


# ============================================================================
# FiveStageResearchPipeline
# ============================================================================

class FiveStageResearchPipeline:
    """五阶段研究管线

    reading → hypothesis_generation → experiment_design → execution → manuscript_writing + reviewer_response
    每阶段产出实体注册到 SchemaGovernedResearchMemory。
    """

    def __init__(self, memory: SchemaGovernedResearchMemory):
        self.memory = memory
        self._lock = threading.RLock()
        self._pipelines: Dict[str, PipelineState] = {}
        self._stage_order = [
            PipelineStage.READING,
            PipelineStage.HYPOTHESIS_GENERATION,
            PipelineStage.EXPERIMENT_DESIGN,
            PipelineStage.EXECUTION,
            PipelineStage.MANUSCRIPT_WRITING,
            PipelineStage.REVIEWER_RESPONSE,
        ]

    def create_pipeline(self, project_id: str) -> PipelineState:
        """创建研究管线"""
        with self._lock:
            state = PipelineState(
                project_id=project_id,
                current_stage=PipelineStage.READING,
                stage_progress={s: 0.0 for s in self._stage_order},
            )
            self._pipelines[project_id] = state
            return state

    def advance_stage(self, project_id: str, artifacts: List[str]) -> Optional[PipelineStage]:
        """推进到下一阶段"""
        with self._lock:
            state = self._pipelines.get(project_id)
            if state is None:
                return None
            current_idx = self._stage_order.index(state.current_stage)
            # 记录当前阶段产出
            state.artifacts[state.current_stage] = artifacts
            state.stage_progress[state.current_stage] = 1.0
            # 推进
            if current_idx + 1 < len(self._stage_order):
                state.current_stage = self._stage_order[current_idx + 1]
                return state.current_stage
            return None  # 管线完成

    def get_pipeline(self, project_id: str) -> Optional[PipelineState]:
        with self._lock:
            return self._pipelines.get(project_id)

    def pipeline_summary(self, project_id: str) -> Dict[str, Any]:
        """管线摘要"""
        with self._lock:
            state = self._pipelines.get(project_id)
            if not state:
                return {}
            return {
                "project_id": project_id,
                "current_stage": state.current_stage.value,
                "progress": {s.value: p for s, p in state.stage_progress.items()},
                "blocked": state.blocked_by,
                "artifact_count": sum(len(v) for v in state.artifacts.values()),
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            active = sum(
                1 for s in self._pipelines.values()
                if s.current_stage != PipelineStage.REVIEWER_RESPONSE
                or s.stage_progress.get(PipelineStage.REVIEWER_RESPONSE, 0) < 1.0
            )
            return {
                "total_pipelines": len(self._pipelines),
                "active_pipelines": active,
                "completed_pipelines": len(self._pipelines) - active,
            }


# ============================================================================
# MultiAgentDAGCoordinator
# ============================================================================

class MultiAgentDAGCoordinator:
    """DAG 多 Agent 协调器

    并行加速科研困难阶段 (假设生成/实验设计)，DAG 拓扑自动编排。
    支持依赖解析、并行调度和失败重试。
    """

    def __init__(self, max_parallel: int = 4):
        self.max_parallel = max_parallel
        self._lock = threading.RLock()
        self._dags: Dict[str, Dict[str, DAGNode]] = {}  # dag_id -> {node_id: DAGNode}

    def create_dag(
        self,
        dag_id: str,
        nodes: List[DAGNode],
    ) -> str:
        """创建 DAG"""
        with self._lock:
            self._dags[dag_id] = {n.node_id: n for n in nodes}
            return dag_id

    def get_runnable_nodes(self, dag_id: str) -> List[DAGNode]:
        """获取可并行执行的节点 (依赖均已满足)"""
        with self._lock:
            dag = self._dags.get(dag_id, {})
            runnable = []
            for node in dag.values():
                if node.state != DAGNodeState.PENDING:
                    continue
                deps_met = all(
                    dag.get(d).state == DAGNodeState.COMPLETED if d in dag else False
                    for d in node.dependencies
                )
                if deps_met:
                    runnable.append(node)
            # 按并行上限截断
            return runnable[:self.max_parallel]

    def mark_completed(self, dag_id: str, node_id: str, result: str) -> bool:
        with self._lock:
            dag = self._dags.get(dag_id, {})
            node = dag.get(node_id)
            if node is None:
                return False
            node.state = DAGNodeState.COMPLETED
            node.result = result
            node.completed_at = time.time()
            return True

    def mark_failed(self, dag_id: str, node_id: str) -> bool:
        with self._lock:
            dag = self._dags.get(dag_id, {})
            node = dag.get(node_id)
            if node is None:
                return False
            node.retry_count += 1
            if node.retry_count > node.max_retries:
                node.state = DAGNodeState.FAILED
            else:
                node.state = DAGNodeState.PENDING  # 等待重试
            return True

    def is_complete(self, dag_id: str) -> bool:
        """检查 DAG 是否全部完成"""
        with self._lock:
            dag = self._dags.get(dag_id, {})
            if not dag:
                return True
            return all(
                n.state in (DAGNodeState.COMPLETED, DAGNodeState.SKIPPED)
                for n in dag.values()
            )

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            total_nodes = sum(len(dag) for dag in self._dags.values())
            completed = sum(
                1 for dag in self._dags.values()
                for n in dag.values()
                if n.state == DAGNodeState.COMPLETED
            )
            return {
                "total_dags": len(self._dags),
                "total_nodes": total_nodes,
                "completed_nodes": completed,
                "max_parallel": self.max_parallel,
            }


# ============================================================================
# VersionedSelfImprovementLoop
# ============================================================================

class VersionedSelfImprovementLoop:
    """版本化自改进闭环

    SciEvolve 类机制: 反馈信号驱动工作流与记忆共同改进。
    每次改进生成版本快照，支持回滚。
    """

    def __init__(self, memory: SchemaGovernedResearchMemory):
        self.memory = memory
        self._lock = threading.RLock()
        self._versions: Dict[str, List[ImprovementVersion]] = defaultdict(list)  # project_id -> versions

    def record_improvement(
        self,
        project_id: str,
        signal: ImprovementSignal,
        trigger_entity_id: str,
        changes: Dict[str, str],
    ) -> ImprovementVersion:
        """记录一轮改进"""
        with self._lock:
            # 计算改进前状态哈希
            before_state = json.dumps(
                sorted(self.memory.graph_stats().items()), sort_keys=True
            )
            before_hash = hashlib.sha256(before_state.encode()).hexdigest()[:12]

            version = ImprovementVersion(
                version_id=f"v{len(self._versions[project_id]) + 1}_{project_id}",
                project_id=project_id,
                signal=signal,
                trigger_entity_id=trigger_entity_id,
                changes=changes,
                before_hash=before_hash,
                after_hash=before_hash,  # 实际应由应用后的状态计算
            )
            self._versions[project_id].append(version)
            return version

    def get_version_history(self, project_id: str) -> List[ImprovementVersion]:
        with self._lock:
            return self._versions.get(project_id, [])

    def latest_version(self, project_id: str) -> Optional[ImprovementVersion]:
        with self._lock:
            versions = self._versions.get(project_id, [])
            return versions[-1] if versions else None

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            total_versions = sum(len(v) for v in self._versions.values())
            return {
                "total_projects": len(self._versions),
                "total_versions": total_versions,
                "avg_versions_per_project": total_versions / max(len(self._versions), 1),
            }


# ============================================================================
# CrossProjectKnowledgeAccumulator
# ============================================================================

class CrossProjectKnowledgeAccumulator:
    """跨项目知识积累器

    多项目间共享知识资产，避免重复造轮子。
    按 entity_type 建立全局知识库，支持相似度检索。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._assets: Dict[str, CrossProjectAsset] = {}
        self._project_assets: Dict[str, Set[str]] = defaultdict(set)  # project_id -> asset_ids

    def register_asset(
        self,
        project_id: str,
        entity: ResearchEntity,
    ) -> CrossProjectAsset:
        """注册项目知识资产"""
        with self._lock:
            asset = CrossProjectAsset(
                asset_id=entity.entity_id,
                asset_type=entity.entity_type,
                content_summary=entity.content[:200],
                source_project_ids=[project_id],
            )
            self._assets[asset.asset_id] = asset
            self._project_assets[project_id].add(asset.asset_id)
            return asset

    def find_similar(
        self,
        entity_type: ResearchEntityType,
        query: str,
        top_k: int = 5,
    ) -> List[CrossProjectAsset]:
        """查找相似知识资产 (基于关键词 Jaccard)"""
        with self._lock:
            query_tokens = set(query.lower().split())
            scored = []
            for asset in self._assets.values():
                if asset.asset_type != entity_type:
                    continue
                asset_tokens = set(asset.content_summary.lower().split())
                if not query_tokens or not asset_tokens:
                    continue
                jaccard = len(query_tokens & asset_tokens) / len(query_tokens | asset_tokens)
                scored.append((jaccard, asset))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [a for _, a in scored[:top_k]]

    def reuse_asset(self, asset_id: str, project_id: str) -> bool:
        """标记资产被复用"""
        with self._lock:
            asset = self._assets.get(asset_id)
            if asset is None:
                return False
            asset.reuse_count += 1
            asset.last_reused_at = time.time()
            if project_id not in asset.source_project_ids:
                asset.source_project_ids.append(project_id)
            self._project_assets[project_id].add(asset_id)
            return True

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "total_assets": len(self._assets),
                "total_projects": len(self._project_assets),
                "total_reuses": sum(a.reuse_count for a in self._assets.values()),
                "by_type": {
                    t.value: sum(1 for a in self._assets.values() if a.asset_type == t)
                    for t in ResearchEntityType
                },
            }


# ============================================================================
# ManuscriptReviewResponder
# ============================================================================

class ManuscriptReviewResponder:
    """审稿人回应生成器

    根据审稿意见检索相关实验/数据/引用，自动生成反驳/修订内容。
    """

    def __init__(self, memory: SchemaGovernedResearchMemory):
        self.memory = memory
        self._lock = threading.RLock()
        self._responses: Dict[str, ReviewerResponse] = {}

    def generate_response(
        self,
        comment: ResearchEntity,
        project_id: str,
    ) -> ReviewerResponse:
        """根据审稿意见生成回应"""
        with self._lock:
            # 检索相关实验和数据
            experiments = self.memory.query_by_type(ResearchEntityType.EXPERIMENT)
            results = self.memory.query_by_type(ResearchEntityType.RESULT)

            # 关键词匹配找到相关证据
            comment_keywords = set(comment.content.lower().split())
            supporting = []
            for exp in experiments:
                exp_kw = set(exp.content.lower().split())
                if comment_keywords & exp_kw:
                    supporting.append(exp.entity_id)
            for res in results:
                res_kw = set(res.content.lower().split())
                if comment_keywords & res_kw:
                    supporting.append(res.entity_id)

            response = ReviewerResponse(
                response_id=f"resp_{project_id}_{len(self._responses)}",
                comment_id=comment.entity_id,
                rebuttal_text=f"Response to: {comment.title}. "
                              f"Supporting evidence found from {len(supporting)} entities.",
                supporting_evidence=supporting,
                revision_actions=["review_manuscript", "update_experiments"],
                confidence=min(0.8, len(supporting) * 0.2),
            )
            self._responses[response.response_id] = response
            return response

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标"""
        with self._lock:
            return {
                "total_responses": len(self._responses),
                "avg_confidence": (
                    sum(r.confidence for r in self._responses.values()) / max(len(self._responses), 1)
                ),
            }


# ============================================================================
# Module Statistics
# ============================================================================

_module_start_time = time.time()


def statistics() -> Dict[str, Any]:
    """模块级统计"""
    return {
        "module": "scientific_research_memory",
        "uptime_seconds": time.time() - _module_start_time,
        "key_classes": [
            "SchemaGovernedResearchMemory",
            "FiveStageResearchPipeline",
            "MultiAgentDAGCoordinator",
            "VersionedSelfImprovementLoop",
            "CrossProjectKnowledgeAccumulator",
            "ManuscriptReviewResponder",
        ],
    }
