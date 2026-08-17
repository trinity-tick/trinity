"""
# status: orphan (2026-08-15 audit, not in runtime path)
P10-4: Graph Memory Workflow Router (对标 GraphPlanner)

实现双维决策图（Agent Role + LLM Backbone 联合选择）、
默认三角色协作图、动态工作流生成、图记忆路由表、
与 P10-3 workflow_memory 联动。

Reference: GraphPlanner — Multi-Agent LLM Graph Memory Workflow Router
           https://new.qq.com/rain/a/20260712A032BE00
"""

import hashlib
import json
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ─── Enums ───────────────────────────────────────────────────────────────────

class AgentRole(Enum):
    """Agent 角色类型（GraphPlanner 默认三角色）"""
    PLANNER = "planner"            # 查询分解、工作流编排
    EXECUTOR = "executor"          # 子任务执行、工具调用
    SUMMARIZER = "summarizer"      # 结果聚合、最终回答生成


class LlmBackbone(Enum):
    """LLM 主干模型"""
    GPT4O = "gpt-4o"
    CLAUDE_SONNET = "claude-sonnet-4"
    GEMINI_PRO = "gemini-2.5-pro"
    DEEPSEEK_V4 = "deepseek-v4"
    QWEN3 = "qwen3-235b"
    LLAMA4 = "llama-4-maverick"


class RoutingPolicy(Enum):
    """路由策略"""
    COST_OPTIMIZED = "cost_optimized"        # 最小化总成本
    LATENCY_OPTIMIZED = "latency_optimized"  # 最小化延迟
    QUALITY_OPTIMIZED = "quality_optimized"  # 最大化质量
    HYBRID = "hybrid"                        # 权衡成本/质量


class QueryComplexity(Enum):
    """查询复杂度分级"""
    SIMPLE = "simple"              # 单步回答（1-hop）
    MODERATE = "moderate"          # 多步推理（2-3 hop）
    COMPLEX = "complex"            # 多子任务 + 合成（4+ hop）
    ANALYTICAL = "analytical"      # 需要深度分析/对比


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    """路由决策：二元动作 (AgentRole, LLMBackbone)"""
    step: int
    role: AgentRole
    backbone: LlmBackbone
    confidence: float = 1.0       # 决策置信度
    reasoning: str = ""           # 决策理由


@dataclass
class CollaborationNode:
    """协作图节点"""
    id: str
    role: AgentRole
    backbone: LlmBackbone
    task_description: str = ""
    predecessors: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)


@dataclass
class CollaborationGraph:
    """Agent 角色协作图

    描述一次查询求解过程中各 Agent Role 的协作拓扑。
    """
    id: str
    query: str
    nodes: dict[str, CollaborationNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from, to)
    total_cost_estimate: float = 0.0
    estimated_latency: float = 0.0
    created_at: float = field(default_factory=time.time)

    def add_node(self, node: CollaborationNode):
        self.nodes[node.id] = node

    def add_edge(self, from_id: str, to_id: str):
        self.edges.append((from_id, to_id))
        self.nodes[from_id].successors.append(to_id)
        self.nodes[to_id].predecessors.append(from_id)

    def execution_order(self) -> list[str]:
        """拓扑排序执行顺序。"""
        indeg = {nid: len(n.predecessors) for nid, n in self.nodes.items()}
        order = []
        queue = [nid for nid, d in indeg.items() if d == 0]
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for succ in self.nodes[nid].successors:
                indeg[succ] -= 1
                if indeg[succ] == 0:
                    queue.append(succ)
        return order


@dataclass
class RouteRecord:
    """路由历史记录"""
    id: str
    query_fingerprint: str         # 查询特征指纹
    complexity: QueryComplexity
    collaboration_graph_id: str
    decisions: list[RoutingDecision]
    success: bool
    actual_latency: float = 0.0
    actual_cost: float = 0.0
    user_feedback: float = 0.0     # 0~1 用户满意度
    created_at: float = field(default_factory=time.time)


# ─── Query Analyzer ──────────────────────────────────────────────────────────

class QueryAnalyzer:
    """查询分析器：评估复杂度、提取特征指纹。"""

    COMPLEXITY_INDICATORS = {
        QueryComplexity.SIMPLE: 1,
        QueryComplexity.MODERATE: 5,
        QueryComplexity.COMPLEX: 10,
        QueryComplexity.ANALYTICAL: 15,
    }

    def analyze(self, query: str) -> tuple[QueryComplexity, str]:
        """分析查询返回复杂度和特征指纹。"""
        complexity = self._estimate_complexity(query)
        fingerprint = self._compute_fingerprint(query)
        return complexity, fingerprint

    def _estimate_complexity(self, query: str) -> QueryComplexity:
        ql = query.lower()
        score = 0

        # 多跳指示词
        if any(w in ql for w in ["compare", "vs", "versus", "difference"]):
            score += 4
        if any(w in ql for w in ["analyze", "evaluate", "assess", "review"]):
            score += 5
        if any(w in ql for w in ["summarize", "aggregate", "consolidate"]):
            score += 3
        if any(w in ql for w in ["and", "also", "additionally", "furthermore"]):
            score += 2
        if any(w in ql for w in ["why", "how", "explain"]):
            score += 2
        if any(w in ql for w in ["best", "optimal", "recommend", "suggest"]):
            score += 3

        # 查询长度
        words = query.split()
        if len(words) > 20:
            score += 3
        elif len(words) > 10:
            score += 1

        if score <= 2:
            return QueryComplexity.SIMPLE
        elif score <= 5:
            return QueryComplexity.MODERATE
        elif score <= 9:
            return QueryComplexity.COMPLEX
        else:
            return QueryComplexity.ANALYTICAL

    def _compute_fingerprint(self, query: str) -> str:
        """计算查询的特征指纹（用于路由表匹配）。"""
        normalized = " ".join(sorted(query.lower().split()))
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ─── Cost Model ──────────────────────────────────────────────────────────────

class CostModel:
    """LLM 调用成本与延迟估算模型。"""

    # 粗略成本 (USD per 1K tokens)
    _COST_PER_1K: dict[LlmBackbone, tuple[float, float]] = {
        LlmBackbone.GPT4O: (0.0025, 0.010),      # input, output
        LlmBackbone.CLAUDE_SONNET: (0.003, 0.015),
        LlmBackbone.GEMINI_PRO: (0.00125, 0.005),
        LlmBackbone.DEEPSEEK_V4: (0.00055, 0.00219),
        LlmBackbone.QWEN3: (0.001, 0.004),
        LlmBackbone.LLAMA4: (0.0005, 0.002),
    }

    # 估算延迟 (ms per call)
    _LATENCY_MS: dict[LlmBackbone, float] = {
        LlmBackbone.GPT4O: 2000,
        LlmBackbone.CLAUDE_SONNET: 1500,
        LlmBackbone.GEMINI_PRO: 2500,
        LlmBackbone.DEEPSEEK_V4: 3500,
        LlmBackbone.QWEN3: 1800,
        LlmBackbone.LLAMA4: 1200,
    }

    # 各角色估算 token 用量
    _ROLE_TOKENS: dict[AgentRole, tuple[int, int]] = {
        AgentRole.PLANNER: (2000, 1500),    # (input_tokens, output_tokens)
        AgentRole.EXECUTOR: (3000, 2000),
        AgentRole.SUMMARIZER: (5000, 3000),
    }

    @classmethod
    def estimate_cost(cls, role: AgentRole, backbone: LlmBackbone) -> float:
        """估算单次角色调用的成本（USD）。"""
        in_tok, out_tok = cls._ROLE_TOKENS[role]
        in_cost, out_cost = cls._COST_PER_1K[backbone]
        return (in_tok / 1000) * in_cost + (out_tok / 1000) * out_cost

    @classmethod
    def estimate_latency(cls, backbone: LlmBackbone) -> float:
        """估算单次调用延迟（秒）。"""
        return cls._LATENCY_MS.get(backbone, 2000) / 1000.0

    @classmethod
    def estimate_collaboration_cost(cls, graph: CollaborationGraph) -> tuple[float, float]:
        """估算协作图的总成本和总延迟。"""
        total_cost = 0.0
        total_latency = 0.0
        for nid, node in graph.nodes.items():
            total_cost += cls.estimate_cost(node.role, node.backbone)
            total_latency += cls.estimate_latency(node.backbone)
        return total_cost, total_latency


# ─── Route Table ─────────────────────────────────────────────────────────────

class RouteTable:
    """图记忆路由表。

    存储历史路由决策 → 成功率 → 用于新查询的最优路由。
    """

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path is None:
            import os as _os
            storage_path = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..",
                "data", "route_table.jsonl"
            )
        self.storage_path = storage_path
        self.records: dict[str, RouteRecord] = {}
        # 多维索引
        self._complexity_index: dict[QueryComplexity, list[str]] = defaultdict(list)
        self._role_backbone_index: dict[str, list[str]] = defaultdict(list)
        self._fingerprint_index: dict[str, str] = {}  # fingerprint → record_id

        self._load()

    def record(self, record: RouteRecord):
        """记录一次路由决策。"""
        self.records[record.id] = record
        self._complexity_index[record.complexity].append(record.id)
        self._fingerprint_index[record.query_fingerprint] = record.id

        for dec in record.decisions:
            key = f"{dec.role.value}:{dec.backbone.value}"
            self._role_backbone_index[key].append(record.id)

    def get_best_route(self, complexity: QueryComplexity,
                        policy: RoutingPolicy = RoutingPolicy.HYBRID,
                        max_backbones: int = 2) -> list[tuple[AgentRole, LlmBackbone]]:
        """获取指定复杂度下最优的路由决策。

        基于历史成功率加权推荐最优 (Role, Backbone) 组合。
        """
        candidates: dict[str, list[tuple[float, list[RouteRecord]]]] = defaultdict(list)

        for rid in self._complexity_index.get(complexity, []):
            rec = self.records.get(rid)
            if not rec:
                continue
            score = self._compute_route_score(rec, policy)
            key = self._route_key(rec.decisions)
            candidates[key].append((score, rec))

        # 聚合评分
        ranked: list[tuple[float, str, list[RoutingDecision]]] = []
        for key, entries in candidates.items():
            if not entries:
                continue
            avg_score = sum(s for s, _ in entries) / len(entries)
            decisions = entries[0][1].decisions
            ranked.append((avg_score, key, decisions))

        ranked.sort(key=lambda x: x[0], reverse=True)

        # 返回 top 2 的 backbone 组合
        seen_backbones: set[str] = set()
        result: list[tuple[AgentRole, LlmBackbone]] = []
        for _, key, decisions in ranked:
            for d in decisions:
                bkey = f"{d.role.value}:{d.backbone.value}"
                if bkey not in seen_backbones:
                    seen_backbones.add(bkey)
                    result.append((d.role, d.backbone))
                    if len(seen_backbones) >= max_backbones * 3:
                        break
            if len(seen_backbones) >= max_backbones * 3:
                break

        return result[:max_backbones * 3]

    def find_similar_query_route(self, fingerprint: str) -> Optional[RouteRecord]:
        """查找相似查询的历史路由。"""
        return self.records.get(self._fingerprint_index.get(fingerprint, ""))

    def get_success_rate(self, role: AgentRole, backbone: LlmBackbone,
                          complexity: Optional[QueryComplexity] = None) -> float:
        """查询特定 (Role, Backbone) 组合的成功率。"""
        key = f"{role.value}:{backbone.value}"
        rec_ids = self._role_backbone_index.get(key, [])
        if complexity:
            rec_ids = [rid for rid in rec_ids
                       if self.records.get(rid) and self.records[rid].complexity == complexity]

        if not rec_ids:
            return 0.0

        successes = sum(1 for rid in rec_ids
                        if self.records.get(rid) and self.records[rid].success)
        return successes / len(rec_ids)

    def stats(self) -> dict:
        total = len(self.records)
        successes = sum(1 for r in self.records.values() if r.success)
        return {
            "total_routes": total,
            "success_rate": successes / max(total, 1),
            "by_complexity": {
                c.value: len(ids)
                for c, ids in self._complexity_index.items()
            },
            "avg_latency": sum(r.actual_latency for r in self.records.values()) / max(total, 1),
        }

    def _compute_route_score(self, record: RouteRecord, policy: RoutingPolicy) -> float:
        """计算路由综合评分。"""
        success_bonus = 1.5 if record.success else 0.3
        feedback = record.user_feedback * 2.0  # 用户反馈权重

        if policy == RoutingPolicy.COST_OPTIMIZED:
            cost_penalty = max(0, 1.0 - record.actual_cost)
            return success_bonus + feedback + cost_penalty
        elif policy == RoutingPolicy.LATENCY_OPTIMIZED:
            latency_penalty = max(0, 1.0 - record.actual_latency / 10.0)
            return success_bonus + feedback + latency_penalty
        elif policy == RoutingPolicy.QUALITY_OPTIMIZED:
            return success_bonus * 2 + feedback
        else:  # HYBRID
            cost_score = max(0, 1.0 - record.actual_cost)
            latency_score = max(0, 1.0 - record.actual_latency / 10.0)
            return success_bonus + feedback + cost_score + latency_score

    @staticmethod
    def _route_key(decisions: list[RoutingDecision]) -> str:
        return "|".join(f"{d.role.value}:{d.backbone.value}" for d in decisions)

    def _load(self):
        import os as _os
        if not _os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec_data = json.loads(line)
                    rec = RouteRecord(
                        id=rec_data["id"],
                        query_fingerprint=rec_data["fingerprint"],
                        complexity=QueryComplexity(rec_data["complexity"]),
                        collaboration_graph_id=rec_data.get("cg_id", ""),
                        decisions=[
                            RoutingDecision(
                                step=d["step"],
                                role=AgentRole(d["role"]),
                                backbone=LlmBackbone(d["backbone"]),
                                confidence=d.get("confidence", 1.0),
                            )
                            for d in rec_data.get("decisions", [])
                        ],
                        success=rec_data.get("success", True),
                        actual_latency=rec_data.get("latency", 0.0),
                        actual_cost=rec_data.get("cost", 0.0),
                        user_feedback=rec_data.get("feedback", 0.0),
                        created_at=rec_data.get("created_at", time.time()),
                    )
                    self.record(rec)
        except Exception:
            pass

    def save(self):
        import os as _os
        _os.makedirs(_os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            for rid, rec in self.records.items():
                record = {
                    "id": rid,
                    "fingerprint": rec.query_fingerprint,
                    "complexity": rec.complexity.value,
                    "cg_id": rec.collaboration_graph_id,
                    "decisions": [
                        {"step": d.step, "role": d.role.value,
                         "backbone": d.backbone.value, "confidence": d.confidence}
                        for d in rec.decisions
                    ],
                    "success": rec.success,
                    "latency": rec.actual_latency,
                    "cost": rec.actual_cost,
                    "feedback": rec.user_feedback,
                    "created_at": rec.created_at,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Graph Router ────────────────────────────────────────────────────────────

class GraphRouter:
    """图记忆工作流路由器。

    双维决策：Agent Role + LLM Backbone 联合选择。
    动态生成角色协作图，参考历史路由表优化决策。
    """

    # 默认骨干模型及其质量/成本评级
    BACKBONE_PROFILE: dict[LlmBackbone, dict[str, Any]] = {
        LlmBackbone.GPT4O: {"quality": 0.92, "cost_tier": "high", "latency_tier": "medium"},
        LlmBackbone.CLAUDE_SONNET: {"quality": 0.90, "cost_tier": "high", "latency_tier": "medium"},
        LlmBackbone.GEMINI_PRO: {"quality": 0.88, "cost_tier": "medium", "latency_tier": "low"},
        LlmBackbone.DEEPSEEK_V4: {"quality": 0.87, "cost_tier": "low", "latency_tier": "high"},
        LlmBackbone.QWEN3: {"quality": 0.85, "cost_tier": "low", "latency_tier": "medium"},
        LlmBackbone.LLAMA4: {"quality": 0.80, "cost_tier": "low", "latency_tier": "high"},
    }

    def __init__(self, route_table: Optional[RouteTable] = None,
                 policy: RoutingPolicy = RoutingPolicy.HYBRID,
                 workflow_memory=None):
        """
        Args:
            route_table: 路由历史表
            policy: 路由策略
            workflow_memory: P10-3 StructuredExecutionMemory 实例（联动）
        """
        self.route_table = route_table or RouteTable()
        self.policy = policy
        self.workflow_memory = workflow_memory
        self.analyzer = QueryAnalyzer()
        self.cost_model = CostModel()

    def route(self, query: str, available_backbones: Optional[list[LlmBackbone]] = None
              ) -> tuple[CollaborationGraph, list[RoutingDecision]]:
        """为查询生成路由决策和协作图。

        流程：
        1. 分析查询（复杂度 + 指纹）
        2. 查询路由表获取历史最优路由
        3. 参考 P10-3 工作流记忆（可选联动）
        4. 构建协作图
        5. 返回 (CollaborationGraph, decisions)
        """
        complexity, fingerprint = self.analyzer.analyze(query)
        backbones = available_backbones or list(LlmBackbone)

        # 尝试从路由表获取最佳路由
        best_routes = self.route_table.get_best_route(complexity, self.policy)

        # 生成决策
        decisions = self._generate_decisions(query, complexity, best_routes, backbones)

        # 构建协作图
        cg = self._build_collaboration_graph(query, decisions, complexity, fingerprint)

        return cg, decisions

    def _generate_decisions(self, query: str, complexity: QueryComplexity,
                             best_routes: list[tuple[AgentRole, LlmBackbone]],
                             available_backbones: list[LlmBackbone]) -> list[RoutingDecision]:
        """生成路由决策序列。"""
        decisions = []

        if complexity == QueryComplexity.SIMPLE:
            # 简单查询：Executor 直接回答
            bb = self._pick_backbone(AgentRole.EXECUTOR, best_routes, available_backbones)
            decisions.append(RoutingDecision(step=0, role=AgentRole.EXECUTOR,
                                              backbone=bb, reasoning="Single-step simple query"))

        elif complexity == QueryComplexity.MODERATE:
            # 中等：Planner → Executor 两步
            bb_p = self._pick_backbone(AgentRole.PLANNER, best_routes, available_backbones)
            bb_e = self._pick_backbone(AgentRole.EXECUTOR, best_routes, available_backbones)
            decisions.append(RoutingDecision(step=0, role=AgentRole.PLANNER,
                                              backbone=bb_p, reasoning="Decompose moderate query"))
            decisions.append(RoutingDecision(step=1, role=AgentRole.EXECUTOR,
                                              backbone=bb_e, reasoning="Execute sub-tasks"))

        elif complexity == QueryComplexity.COMPLEX:
            # 复杂：Planner → Executor(s) → Summarizer
            bb_p = self._pick_backbone(AgentRole.PLANNER, best_routes, available_backbones)
            bb_e = self._pick_backbone(AgentRole.EXECUTOR, best_routes, available_backbones)
            bb_s = self._pick_backbone(AgentRole.SUMMARIZER, best_routes, available_backbones)
            decisions.append(RoutingDecision(step=0, role=AgentRole.PLANNER,
                                              backbone=bb_p, reasoning="Decompose complex query"))
            decisions.append(RoutingDecision(step=1, role=AgentRole.EXECUTOR,
                                              backbone=bb_e, reasoning="Execute parallel sub-tasks"))
            decisions.append(RoutingDecision(step=2, role=AgentRole.SUMMARIZER,
                                              backbone=bb_s, reasoning="Synthesize results"))

        elif complexity == QueryComplexity.ANALYTICAL:
            # 分析型：Planner → 多Executor → Summarizer → Executor(精炼)
            bb_p = self._pick_backbone(AgentRole.PLANNER, best_routes, available_backbones)
            bb_e = self._pick_backbone(AgentRole.EXECUTOR, best_routes, available_backbones)
            bb_s = self._pick_backbone(AgentRole.SUMMARIZER, best_routes, available_backbones)
            decisions.append(RoutingDecision(step=0, role=AgentRole.PLANNER,
                                              backbone=bb_p, reasoning="Analyze & decompose"))
            decisions.append(RoutingDecision(step=1, role=AgentRole.EXECUTOR,
                                              backbone=bb_e, reasoning="Execute deep analysis"))
            decisions.append(RoutingDecision(step=2, role=AgentRole.SUMMARIZER,
                                              backbone=bb_s, reasoning="Aggregate findings"))
            decisions.append(RoutingDecision(step=3, role=AgentRole.EXECUTOR,
                                              backbone=bb_e, reasoning="Refine & validate"))

        return decisions

    def _pick_backbone(self, role: AgentRole,
                        best_routes: list[tuple[AgentRole, LlmBackbone]],
                        available: list[LlmBackbone]) -> LlmBackbone:
        """为特定角色选择最优 LLM Backbone。"""
        # 优先使用历史最优路由
        for hist_role, hist_bb in best_routes:
            if hist_role == role and hist_bb in available:
                return hist_bb

        # 兜底：按策略选择
        if self.policy == RoutingPolicy.COST_OPTIMIZED:
            # 选择最便宜的
            sorted_bb = sorted(available, key=lambda bb: CostModel.estimate_cost(role, bb))
            return sorted_bb[0] if sorted_bb else LlmBackbone.DEEPSEEK_V4
        elif self.policy == RoutingPolicy.LATENCY_OPTIMIZED:
            sorted_bb = sorted(available, key=lambda bb: CostModel.estimate_latency(bb))
            return sorted_bb[0] if sorted_bb else LlmBackbone.LLAMA4
        elif self.policy == RoutingPolicy.QUALITY_OPTIMIZED:
            sorted_bb = sorted(available,
                               key=lambda bb: self.BACKBONE_PROFILE.get(bb, {}).get("quality", 0),
                               reverse=True)
            return sorted_bb[0] if sorted_bb else LlmBackbone.GPT4O
        else:  # HYBRID
            # Planner/Summarizer 用高质量，Executor 用低成本
            if role in (AgentRole.PLANNER, AgentRole.SUMMARIZER):
                return LlmBackbone.GPT4O if LlmBackbone.GPT4O in available else available[0]
            else:
                return LlmBackbone.DEEPSEEK_V4 if LlmBackbone.DEEPSEEK_V4 in available else available[0]

    def _build_collaboration_graph(self, query: str, decisions: list[RoutingDecision],
                                    complexity: QueryComplexity,
                                    fingerprint: str) -> CollaborationGraph:
        """构建角色协作图。"""
        cg = CollaborationGraph(
            id=str(uuid.uuid4())[:12],
            query=query,
        )

        for i, dec in enumerate(decisions):
            node = CollaborationNode(
                id=f"{dec.role.value}_{i}",
                role=dec.role,
                backbone=dec.backbone,
                task_description=f"Step {i}: {dec.role.value}",
            )
            cg.add_node(node)

        # 连接边
        for i in range(len(decisions) - 1):
            from_id = f"{decisions[i].role.value}_{i}"
            to_id = f"{decisions[i+1].role.value}_{i+1}"
            cg.add_edge(from_id, to_id)

        # 估算成本
        total_cost, total_latency = CostModel.estimate_collaboration_cost(cg)
        cg.total_cost_estimate = total_cost
        cg.estimated_latency = total_latency

        return cg

    def record_execution(self, cg: CollaborationGraph, decisions: list[RoutingDecision],
                          success: bool, actual_latency: float = 0.0,
                          actual_cost: float = 0.0, user_feedback: float = 0.5):
        """记录一次路由执行结果（反馈学习）。"""
        complexity, fingerprint = self.analyzer.analyze(cg.query)
        record = RouteRecord(
            id=str(uuid.uuid4())[:12],
            query_fingerprint=fingerprint,
            complexity=complexity,
            collaboration_graph_id=cg.id,
            decisions=decisions,
            success=success,
            actual_latency=actual_latency,
            actual_cost=actual_cost,
            user_feedback=user_feedback,
        )
        self.route_table.record(record)

        # 联动 P10-3：如果提供了 workflow_memory
        if self.workflow_memory:
            from trinity.modules.second_brain.workflow_memory import (
                HierarchicalTrajectory, TrajectoryTag, WorkflowGraph, WorkflowNode,
                NodeType, WorkflowPattern,
            )
            tag = TrajectoryTag.SUCCESS if success else TrajectoryTag.FAILURE
            # 将路由决策编码为简化的 WorkflowGraph
            wg = WorkflowGraph(id=cg.id, name=f"Route: {cg.query[:30]}")
            for nid, node in cg.nodes.items():
                wg.add_node(WorkflowNode(
                    id=nid, node_type=NodeType.SYNTHESIS,
                    label=f"{node.role.value}:{node.backbone.value}",
                    success=success,
                ))
            traj = HierarchicalTrajectory(
                id=cg.id, query=cg.query, tag=tag,
            )
            traj.workflow_graphs["route"] = wg
            self.workflow_memory.add_trajectory(traj)

    def query_complexity_distribution(self) -> dict[str, int]:
        """查询复杂度分布统计。"""
        return {
            c.value: len(ids)
            for c, ids in self.route_table._complexity_index.items()
        }


# ─── Convenience API ─────────────────────────────────────────────────────────

def create_graph_router(policy: RoutingPolicy = RoutingPolicy.HYBRID,
                         route_table_path: Optional[str] = None,
                         workflow_memory=None) -> GraphRouter:
    """创建图记忆工作流路由器。"""
    table = RouteTable(storage_path=route_table_path)
    return GraphRouter(route_table=table, policy=policy, workflow_memory=workflow_memory)
