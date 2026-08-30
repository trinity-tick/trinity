"""
# status: active (2026-09 EXECUTION 172: 大脑方向激活) (2026-09 EXECUTION 163)
P10-3: FlowSearcher Workflow Memory Synthesis (对标 ICLR2026)

实现分层轨迹记忆、结构化工作流图、经验注入、成功/失败双轨记忆、
learning-free 泛化。

Reference: FlowSearcher — Synthesizing Memory-Guided Agentic Workflows
           for Web Information Seeking (ICLR2026)
           https://papernotes.org/ICLR2026/llm_agent/flowsearcher_synthesizing_memory-guided_agentic_workflows_for_web_information_se
"""

import hashlib
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ─── Enums ───────────────────────────────────────────────────────────────────

class NodeType(Enum):
    """工作流节点类型"""
    QUERY_DECOMPOSITION = "query_decomposition"      # 查询分解
    SUB_QUERY = "sub_query"                          # 子问题
    TOOL_CALL = "tool_call"                          # 工具调用
    SYNTHESIS = "synthesis"                          # 结果合成
    VALIDATION = "validation"                        # 验证节点
    BRANCH = "branch"                                # 条件分支


class TrajectoryTag(Enum):
    """轨迹标签"""
    SUCCESS = "success"                  # 成功轨迹
    FAILURE = "failure"                  # 失败轨迹
    PARTIAL = "partial"                  # 部分成功
    AMBIGUOUS = "ambiguous"              # 结果模糊


class WorkflowPattern(Enum):
    """常见工作流模式"""
    SEQUENTIAL = "sequential"            # 串行工具链
    PARALLEL = "parallel"                # 并行多工具
    ITERATIVE = "iterative"              # 迭代深化
    BRANCH_AND_MERGE = "branch_merge"    # 分支合并
    DECOMPOSE_SOLVE = "decompose_solve"  # 分解求解
    VALIDATE_REFINE = "validate_refine"  # 验证精炼


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class SubProblem:
    """子问题 μ_i"""
    id: str
    description: str                    # 子问题自然语言描述
    dependencies: list[str] = field(default_factory=list)  # 依赖的前置子问题 ID
    priority: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class WorkflowNode:
    """工作流图节点"""
    id: str
    node_type: NodeType
    label: str
    inputs: list[str] = field(default_factory=list)     # 输入边源节点 ID
    outputs: list[str] = field(default_factory=list)    # 输出边目标节点 ID
    tool_name: str = ""                                  # 工具名称（TOOL_CALL 节点）
    parameters: dict = field(default_factory=dict)
    result_preview: str = ""                             # 结果摘要
    execution_time: float = 0.0
    success: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class WorkflowGraph:
    """工作流图 G_i

    描述求解一个子问题 or 总查询的完整 DAG。
    """
    id: str
    name: str
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from_id, to_id)
    pattern: WorkflowPattern = WorkflowPattern.SEQUENTIAL
    entry_node: str = ""
    exit_node: str = ""
    metadata: dict = field(default_factory=dict)

    def add_node(self, node: WorkflowNode):
        self.nodes[node.id] = node
        if not self.entry_node:
            self.entry_node = node.id

    def add_edge(self, from_id: str, to_id: str):
        self.edges.append((from_id, to_id))
        if from_id in self.nodes:
            self.nodes[from_id].outputs.append(to_id)
        if to_id in self.nodes:
            self.nodes[to_id].inputs.append(from_id)

    def topological_order(self) -> list[str]:
        """拓扑排序。"""
        indeg = {nid: len(n.inputs) for nid, n in self.nodes.items()}
        order = []
        queue = [nid for nid, d in indeg.items() if d == 0]

        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for out in self.nodes[nid].outputs:
                indeg[out] -= 1
                if indeg[out] == 0:
                    queue.append(out)
        return order


@dataclass
class HierarchicalTrajectory:
    """分层轨迹 Γ = {μ_i, G_i}

    高层：查询分解 + 工作流合成
    低层：工作流执行
    """
    id: str
    query: str                         # 原始查询 Q
    predicted_answer: str = ""         # 预测答案 y^
    tag: TrajectoryTag = TrajectoryTag.SUCCESS
    sub_problems: list[SubProblem] = field(default_factory=list)
    workflow_graphs: dict[str, WorkflowGraph] = field(default_factory=dict)
    # 高层合成轨迹
    decomposition_strategy: str = ""    # 分解策略描述
    synthesis_strategy: str = ""        # 合成策略描述
    # 低层执行日志
    execution_log: list[dict] = field(default_factory=list)
    total_time: float = 0.0
    created_at: float = field(default_factory=time.time)
    embedding: Optional[list[float]] = None   # 查询嵌入（用于相似度检索）


# ─── Structured Execution Memory ─────────────────────────────────────────────

class StructuredExecutionMemory:
    """结构化执行记忆 M

    维护历史轨迹索引、支持相似轨迹检索、成功/失败模式挖掘。
    """

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path is None:
            import os as _os
            storage_path = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..",
                "data", "workflow_memory.jsonl"
            )
        self.storage_path = storage_path
        self.trajectories: dict[str, HierarchicalTrajectory] = {}

        # 索引
        self._success_trajectories: list[str] = []   # 成功轨迹 ID 列表
        self._failure_trajectories: list[str] = []    # 失败轨迹 ID 列表
        self._pattern_index: dict[str, list[str]] = defaultdict(list)  # pattern → trajectory IDs
        self._keyword_index: dict[str, set[str]] = defaultdict(set)    # keyword → trajectory IDs

        self._load()

    def add_trajectory(self, trajectory: HierarchicalTrajectory):
        """记录一条轨迹。"""
        self.trajectories[trajectory.id] = trajectory

        if trajectory.tag == TrajectoryTag.SUCCESS:
            self._success_trajectories.append(trajectory.id)
        elif trajectory.tag == TrajectoryTag.FAILURE:
            self._failure_trajectories.append(trajectory.id)

        # 索引模式
        for gid, g in trajectory.workflow_graphs.items():
            self._pattern_index[g.pattern.value].append(trajectory.id)

        # 索引关键词
        keywords = self._extract_keywords(trajectory.query)
        for kw in keywords:
            self._keyword_index[kw.lower()].add(trajectory.id)

    def retrieve_similar(self, query: str, top_k: int = 5,
                          tag_filter: Optional[TrajectoryTag] = None) -> list[HierarchicalTrajectory]:
        """检索与查询相似的历史轨迹。

        用于经验注入：新查询 → 检索相似历史轨迹 → 注入工作流合成。
        """
        q_keywords = set(self._extract_keywords(query))
        scored: list[tuple[float, HierarchicalTrajectory]] = []

        for tid, traj in self.trajectories.items():
            if tag_filter and traj.tag != tag_filter:
                continue

            t_keywords = self._keyword_index_for_traj(traj.id)
            overlap = len(q_keywords & t_keywords)

            # 模式匹配加分
            pattern_bonus = 0.0
            for gid, g in traj.workflow_graphs.items():
                if g.pattern.value in query.lower():
                    pattern_bonus += 0.2

            score = overlap / max(len(q_keywords), 1) + pattern_bonus
            if score > 0:
                scored.append((score, traj))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:top_k]]

    def get_success_patterns(self) -> dict[str, int]:
        """统计成功轨迹中最常见的工作流模式。"""
        counts = defaultdict(int)
        for tid in self._success_trajectories:
            traj = self.trajectories.get(tid)
            if traj:
                for gid, g in traj.workflow_graphs.items():
                    counts[g.pattern.value] += 1
        return dict(counts)

    def get_failure_modes(self) -> list[dict]:
        """提取失败模式（failure modes）供修订。"""
        failures = []
        for tid in self._failure_trajectories:
            traj = self.trajectories.get(tid)
            if not traj:
                continue
            failed_nodes = [
                nid for nid, node in traj.workflow_graphs.get("main", WorkflowGraph("", "")).nodes.items()
                if not node.success
            ]
            failures.append({
                "trajectory_id": tid,
                "query": traj.query,
                "failed_nodes": failed_nodes,
                "decomposition_strategy": traj.decomposition_strategy,
                "failure_count": len(failed_nodes),
            })
        return sorted(failures, key=lambda f: f["failure_count"], reverse=True)

    def stats(self) -> dict:
        return {
            "total_trajectories": len(self.trajectories),
            "success_count": len(self._success_trajectories),
            "failure_count": len(self._failure_trajectories),
            "patterns": {
                p.value: len(ids)
                for p, ids in self._pattern_index.items()
            },
            "total_keywords": len(self._keyword_index),
        }

    def _extract_keywords(self, text: str) -> list[str]:
        """从查询中提取关键词。"""
        stopwords = {"the", "a", "an", "is", "of", "to", "in", "for", "on", "and", "or",
                     "how", "what", "why", "when", "where", "who", "?", ".", ",", "!"}
        words = text.lower().split()
        return [w for w in words if w not in stopwords and len(w) > 2]

    def _keyword_index_for_traj(self, tid: str) -> set[str]:
        result = set()
        for kw, tids in self._keyword_index.items():
            if tid in tids:
                result.add(kw)
        return result

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
                    record = json.loads(line)
                    traj = HierarchicalTrajectory(
                        id=record["id"],
                        query=record["query"],
                        predicted_answer=record.get("predicted_answer", ""),
                        tag=TrajectoryTag(record.get("tag", "success")),
                        decomposition_strategy=record.get("decomposition_strategy", ""),
                        synthesis_strategy=record.get("synthesis_strategy", ""),
                        total_time=record.get("total_time", 0.0),
                        created_at=record.get("created_at", time.time()),
                    )
                    for sp in record.get("sub_problems", []):
                        traj.sub_problems.append(SubProblem(**sp))
                    for gid, gdata in record.get("workflow_graphs", {}).items():
                        wg = WorkflowGraph(id=gdata["id"], name=gdata["name"],
                                           pattern=WorkflowPattern(gdata.get("pattern", "sequential")))
                        for nid, ndata in gdata.get("nodes", {}).items():
                            wg.add_node(WorkflowNode(
                                id=ndata["id"],
                                node_type=NodeType(ndata["node_type"]),
                                label=ndata["label"],
                                success=ndata.get("success", True),
                            ))
                        for e in gdata.get("edges", []):
                            wg.add_edge(e[0], e[1])
                        traj.workflow_graphs[gid] = wg
                    self.add_trajectory(traj)
        except Exception:
            pass

    def save(self):
        import os as _os
        _os.makedirs(_os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            for tid, traj in self.trajectories.items():
                record = {
                    "id": traj.id,
                    "query": traj.query,
                    "predicted_answer": traj.predicted_answer,
                    "tag": traj.tag.value,
                    "decomposition_strategy": traj.decomposition_strategy,
                    "synthesis_strategy": traj.synthesis_strategy,
                    "total_time": traj.total_time,
                    "created_at": traj.created_at,
                    "sub_problems": [
                        {"id": sp.id, "description": sp.description,
                         "dependencies": sp.dependencies, "priority": sp.priority}
                        for sp in traj.sub_problems
                    ],
                    "workflow_graphs": {
                        gid: {
                            "id": g.id, "name": g.name,
                            "pattern": g.pattern.value,
                            "nodes": {
                                nid: {"id": node.id, "node_type": node.node_type.value,
                                      "label": node.label, "success": node.success}
                                for nid, node in g.nodes.items()
                            },
                            "edges": [[e[0], e[1]] for e in g.edges],
                        }
                        for gid, g in traj.workflow_graphs.items()
                    },
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Workflow Synthesizer ────────────────────────────────────────────────────

class WorkflowSynthesizer:
    """工作流合成器：将新查询与历史经验结合，生成工作流图。"""

    def __init__(self, memory: StructuredExecutionMemory):
        self.memory = memory

    def synthesize(self, query: str, top_k_history: int = 3) -> HierarchicalTrajectory:
        """合成工作流轨迹。

        核心流程：
        1. 查询分解 → 子问题 {μ_i}
        2. 检索相似历史轨迹 → 经验注入
        3. 为每个子问题合成工作流图 {G_i}
        4. 生成分层轨迹 Γ
        """
        traj = HierarchicalTrajectory(
            id=str(uuid.uuid4())[:12],
            query=query,
        )

        # Step 1: 查询分解
        traj.sub_problems = self._decompose_query(query)
        traj.decomposition_strategy = self._describe_decomposition(traj.sub_problems)

        # Step 2: 经验注入 — 检索相似轨迹
        similar = self.memory.retrieve_similar(query, top_k=top_k_history)

        # Step 3: 为每个子问题合成工作流图
        for sp in traj.sub_problems:
            wg = self._synthesize_workflow(sp, similar)
            traj.workflow_graphs[sp.id] = wg

        # Step 4: 主工作流图（连接各子问题）
        main_wg = self._synthesize_main_workflow(traj.sub_problems)
        traj.workflow_graphs["main"] = main_wg

        traj.synthesis_strategy = self._describe_synthesis(traj.workflow_graphs)
        return traj

    def inject_experience(self, traj: HierarchicalTrajectory,
                           similar_trajectories: list[HierarchicalTrajectory]) -> HierarchicalTrajectory:
        """将历史轨迹经验注入到当前工作流合成中。

        成功轨迹 → 强化正模式
        失败轨迹 → 暴露 failure mode 供修订
        """
        for hist in similar_trajectories:
            if hist.tag == TrajectoryTag.SUCCESS:
                # 复用成功模式
                for gid, g in hist.workflow_graphs.items():
                    if g.pattern in (WorkflowPattern.PARALLEL, WorkflowPattern.DECOMPOSE_SOLVE):
                        traj.workflow_graphs[f"inherited_{gid}"] = g
                        traj.decomposition_strategy += f" | reusing pattern {g.pattern.value} from {hist.id[:6]}"

            elif hist.tag == TrajectoryTag.FAILURE:
                # 暴露失败节点以避免
                for gid, g in hist.workflow_graphs.items():
                    failed_nodes = [nid for nid, n in g.nodes.items() if not n.success]
                    if failed_nodes:
                        traj.execution_log.append({
                            "type": "failure_avoidance",
                            "source_trajectory": hist.id,
                            "failed_nodes": failed_nodes,
                            "avoidance": f"Avoiding nodes: {', '.join(failed_nodes)}",
                        })

        return traj

    def _decompose_query(self, query: str) -> list[SubProblem]:
        """启发式查询分解。

        规则：
        - "compare" / "vs" → 分解为多个单项分析子问题
        - "and" → 按并列分解
        - 复杂的 what/which → 分解为范围探索 + 排序 + 选择
        """
        result = []
        ql = query.lower()

        if "compare" in ql or " vs " in ql or "versus" in ql:
            # 对比类：拆为两个独立分析 + 合成
            parts = re.split(r'\b(?:compare|vs|versus|and)\b', query, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
            for i, part in enumerate(parts):
                result.append(SubProblem(
                    id=f"sp_{i}",
                    description=f"Analyze: {part}",
                    priority=i,
                ))
            if len(result) >= 2:
                result.append(SubProblem(
                    id="sp_synthesis",
                    description="Synthesize comparative analysis",
                    dependencies=[sp.id for sp in result],
                    priority=len(result),
                ))
        elif " and " in ql and "what" not in ql:
            # 并列任务：各自独立执行
            parts = re.split(r'\band\b', query, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
            for i, part in enumerate(parts):
                result.append(SubProblem(id=f"sp_{i}", description=part, priority=i))
        else:
            # 默认：单一子问题
            result.append(SubProblem(id="sp_0", description=query, priority=0))

        return result

    def _synthesize_workflow(self, sp: SubProblem,
                              similar: list[HierarchicalTrajectory]) -> WorkflowGraph:
        """为子问题合成工作流图 G_i。"""
        wg = WorkflowGraph(id=f"wf_{sp.id}", name=sp.description[:30])

        # 尝试从相似轨迹中复制最佳工作流图
        best_pattern = WorkflowPattern.SEQUENTIAL
        pattern_counts = defaultdict(int)
        for hist in similar:
            for gid, g in hist.workflow_graphs.items():
                pattern_counts[g.pattern] += 1
        if pattern_counts:
            best_pattern = max(pattern_counts, key=pattern_counts.get)

        wg.pattern = best_pattern

        if best_pattern == WorkflowPattern.PARALLEL:
            # 并行模式：多个工具同时调用
            for i in range(3):
                node = WorkflowNode(
                    id=f"tool_{i}", node_type=NodeType.TOOL_CALL,
                    label=f"Tool Call {i}", tool_name=f"tool_{i}",
                )
                wg.add_node(node)
            wg.entry_node = "tool_0"
            wg.exit_node = "tool_2"
        elif best_pattern == WorkflowPattern.DECOMPOSE_SOLVE:
            # 分解求解模式
            decomp = WorkflowNode(id="decomp", node_type=NodeType.QUERY_DECOMPOSITION,
                                  label="Decompose Query")
            solve = WorkflowNode(id="solve", node_type=NodeType.TOOL_CALL,
                                 label="Solve Sub-problems")
            synth = WorkflowNode(id="synth", node_type=NodeType.SYNTHESIS,
                                 label="Synthesize Results")
            wg.add_node(decomp)
            wg.add_node(solve)
            wg.add_node(synth)
            wg.add_edge("decomp", "solve")
            wg.add_edge("solve", "synth")
            wg.entry_node = "decomp"
            wg.exit_node = "synth"
        else:
            # 默认串行模式
            node1 = WorkflowNode(id="step1", node_type=NodeType.TOOL_CALL,
                                 label="Search/Retrieve", tool_name="search")
            node2 = WorkflowNode(id="step2", node_type=NodeType.VALIDATION,
                                 label="Validate Results")
            node3 = WorkflowNode(id="step3", node_type=NodeType.SYNTHESIS,
                                 label="Format Output")
            wg.add_node(node1)
            wg.add_node(node2)
            wg.add_node(node3)
            wg.add_edge("step1", "step2")
            wg.add_edge("step2", "step3")
            wg.entry_node = "step1"
            wg.exit_node = "step3"

        return wg

    def _synthesize_main_workflow(self, sub_problems: list[SubProblem]) -> WorkflowGraph:
        """合成主工作流图，连接所有子问题。"""
        wg = WorkflowGraph(id="main", name="Main Workflow",
                           pattern=WorkflowPattern.DECOMPOSE_SOLVE)

        for i, sp in enumerate(sub_problems):
            node = WorkflowNode(
                id=f"sp_{i}_exec",
                node_type=NodeType.SUB_QUERY,
                label=sp.description[:40],
            )
            wg.add_node(node)

        if len(sub_problems) > 1:
            synth = WorkflowNode(id="final_synthesis", node_type=NodeType.SYNTHESIS,
                                 label="Final Synthesis")
            wg.add_node(synth)
            for i in range(len(sub_problems)):
                wg.add_edge(f"sp_{i}_exec", "final_synthesis")
            wg.entry_node = "sp_0_exec"
            wg.exit_node = "final_synthesis"
        else:
            wg.entry_node = "sp_0_exec"
            wg.exit_node = "sp_0_exec"

        return wg

    def _describe_decomposition(self, sub_problems: list[SubProblem]) -> str:
        return f"Decomposed into {len(sub_problems)} sub-problems: " + \
               ", ".join(sp.description[:30] for sp in sub_problems)

    def _describe_synthesis(self, workflow_graphs: dict[str, WorkflowGraph]) -> str:
        return "Synthesized " + ", ".join(
            f"{gid} ({g.pattern.value})" for gid, g in workflow_graphs.items()
        )


# ─── Learning-Free Generalizer ───────────────────────────────────────────────

class LearningFreeGeneralizer:
    """Learning-Free 泛化器。

    不重新训练，通过模式匹配和轨迹迁移实现跨任务知识传递。
    """

    def __init__(self, memory: StructuredExecutionMemory):
        self.memory = memory

    def generalize(self, new_query: str, top_k: int = 5) -> HierarchicalTrajectory:
        """将旧轨迹知识泛化到新任务。

        过程：
        1. 检索最相似的 k 条历史轨迹
        2. 提取可复用的工作流模式
        3. 过滤失败模式
        4. 生成新轨迹
        """
        similar = self.memory.retrieve_similar(new_query, top_k=top_k)

        # 提取成功模式
        success_patterns: dict[str, int] = defaultdict(int)
        failure_patterns: set[str] = set()

        for hist in similar:
            if hist.tag == TrajectoryTag.SUCCESS:
                for gid, g in hist.workflow_graphs.items():
                    success_patterns[g.pattern.value] += 1
            elif hist.tag == TrajectoryTag.FAILURE:
                for gid, g in hist.workflow_graphs.items():
                    failure_patterns.add(g.pattern.value)

        # 过滤：只保留成功率高的模式
        viable_patterns = [
            p for p, count in success_patterns.items()
            if p not in failure_patterns or success_patterns[p] > len(failure_patterns)
        ]

        # 生成新轨迹（基于最优模式）
        synthesizer = WorkflowSynthesizer(self.memory)
        traj = synthesizer.synthesize(new_query, top_k_history=top_k)

        # 注入经验
        traj = synthesizer.inject_experience(traj, similar)

        # 标记泛化模式
        traj.metadata = {
            "generalized_from": len(similar),
            "viable_patterns": viable_patterns,
            "avoided_failures": list(failure_patterns),
            "generalization_method": "learning_free_pattern_migration",
        }

        return traj

    def transfer_workflow(self, source_query: str, target_query: str) -> Optional[HierarchicalTrajectory]:
        """显式工作流迁移：将源查询的成功模式迁移到目标查询。"""
        similar = self.memory.retrieve_similar(source_query, top_k=1,
                                                tag_filter=TrajectoryTag.SUCCESS)
        if not similar:
            return None

        source_traj = similar[0]
        new_traj = HierarchicalTrajectory(
            id=str(uuid.uuid4())[:12],
            query=target_query,
            tag=TrajectoryTag.PARTIAL,
        )

        # 复制子问题结构
        for sp in source_traj.sub_problems:
            new_sp = SubProblem(
                id=f"transferred_{sp.id}",
                description=sp.description.replace(source_query, target_query),
                dependencies=[f"transferred_{d}" for d in sp.dependencies],
                priority=sp.priority,
            )
            new_traj.sub_problems.append(new_sp)

        # 复制工作流图结构
        for gid, g in source_traj.workflow_graphs.items():
            new_wg = WorkflowGraph(
                id=f"transferred_{gid}",
                name=g.name,
                pattern=g.pattern,
            )
            for nid, node in g.nodes.items():
                new_node = WorkflowNode(
                    id=f"transferred_{nid}",
                    node_type=node.node_type,
                    label=node.label,
                    tool_name=node.tool_name,
                )
                new_wg.add_node(new_node)
            for e in g.edges:
                new_wg.add_edge(f"transferred_{e[0]}", f"transferred_{e[1]}")
            new_traj.workflow_graphs[gid] = new_wg

        new_traj.decomposition_strategy = f"Transferred from {source_query}"
        new_traj.synthesis_strategy = source_traj.synthesis_strategy

        return new_traj


# ─── Convenience API ─────────────────────────────────────────────────────────

import re


def create_workflow_memory(storage_path: Optional[str] = None) -> tuple[
    StructuredExecutionMemory, WorkflowSynthesizer, LearningFreeGeneralizer
]:
    """创建工作流记忆系统完整组件。"""
    mem = StructuredExecutionMemory(storage_path)
    synthesizer = WorkflowSynthesizer(mem)
    generalizer = LearningFreeGeneralizer(mem)
    return mem, synthesizer, generalizer
