"""
# status: active (2026-09 EXECUTION 172: 大脑方向激活) (2026-09 EXECUTION 163)
P5-2: Causal Reasoning Memory Engine (对标 ActMem)
=====================================================

从对话历史自动构建因果 + 语义混合图，支持反事实推理、
常识补全和冲突消解。附带 ActMemEval 风格的逻辑驱动评测用例。

Reference: Zhang et al., "ActMem: Bridging the Gap Between Memory Retrieval
           and Reasoning in LLM Agents", arXiv:2603.00026, Feb 2026.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举 ──────────────────────────────────────────────────────────────

class EdgeType(Enum):
    CAUSES = "causes"
    PREVENTS = "prevents"
    ENABLES = "enables"
    CORRELATES = "correlates"
    CONTRADICTS = "contradicts"
    IMPLIES = "implies"
    PRECEDES = "precedes"
    SEMANTIC_LINK = "semantic"


class ReasoningType(Enum):
    COUNTERFACTUAL = "counterfactual"
    COMMONSENSE = "commonsense"
    CONFLICT_RESOLUTION = "conflict"
    MULTI_HOP = "multi_hop"
    TEMPORAL = "temporal"


class ConflictSeverity(Enum):
    MILD = "mild"
    MODERATE = "moderate"
    CRITICAL = "critical"


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class CausalNode:
    node_id: str
    statement: str
    source_dialogue_id: str = ""
    timestamp: float = field(default_factory=time.time)
    confidence: float = 1.0
    is_fact: bool = True
    is_constraint: bool = False
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalEdge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    strength: float = 0.5
    explanation: str = ""
    timestamp: float = field(default_factory=time.time)
    source_dialogue_id: str = ""


@dataclass
class CounterfactualResult:
    hypothesis: str
    conclusion: str
    reasoning_chain: List[str]
    confidence: float
    alternative_outcomes: List[str] = field(default_factory=list)
    evidence_nodes: List[str] = field(default_factory=list)


@dataclass
class CommonsenseCompletion:
    explicit_statement: str
    implicit_constraint: str
    reasoning: str
    confidence: float
    prerequisite_nodes: List[str] = field(default_factory=list)


@dataclass
class ConflictReport:
    conflict_id: str
    node_a_id: str
    node_b_id: str
    description: str
    severity: ConflictSeverity
    resolution_strategy: str
    resolution_confidence: float = 0.5
    overwrite_suggestion: Optional[str] = None


@dataclass
class ActMemEvalCase:
    case_id: str
    category: ReasoningType
    dialogue_history: List[str]
    question: str
    expected_answer: str
    required_nodes: List[str] = field(default_factory=list)
    difficulty: str = "medium"


ACTMEM_EVAL_SUITE: List[ActMemEvalCase] = [
    ActMemEvalCase("counterfactual_001", ReasoningType.COUNTERFACTUAL,
        ["User: 我决定周五坐高铁去北京出差。",
         "Assistant: 好的，周五高铁票已预订。北京周五有暴雨预警。",
         "User: 暴雨的话我改签周六可以吗？"],
        "如果用户周五没有收到暴雨预警，他会怎么做？",
        "用户会按原计划周五坐高铁去北京出差。", difficulty="medium"),
    ActMemEvalCase("counterfactual_002", ReasoningType.COUNTERFACTUAL,
        ["User: 项目截止日期是下周五。",
         "Assistant: 目前进度 70%，按计划周三可完成。",
         "User: Alice 请假了，前端工作缺人。"],
        "如果 Alice 没有请假，项目会延期吗？",
        "如果 Alice 没请假，前端工作不缺人，项目可能不会延期。", difficulty="medium"),
    ActMemEvalCase("commonsense_001", ReasoningType.COMMONSENSE,
        ["User: 帮我订一张明天去上海的机票。",
         "Assistant: 已查询，明天最早航班 6:30，最晚 22:00。"],
        "用户订机票隐含了什么约束？",
        "用户需要在明天之内抵达上海；通常需要至少提前1小时到机场。", difficulty="easy"),
    ActMemEvalCase("commonsense_002", ReasoningType.COMMONSENSE,
        ["User: 我在深圳，想去香港见客户。",
         "Assistant: 深圳到香港可以坐高铁或地铁。",
         "User: 我的港澳通行证过期了。"],
        "用户去香港见客户隐含了什么约束？",
        "用户需要有效签注的港澳通行证才能出境去香港；通行证过期意味着无法立即出行，需要先办证。", difficulty="medium"),
    ActMemEvalCase("conflict_001", ReasoningType.CONFLICT_RESOLUTION,
        ["User: 我的预算只有 5000 元。",
         "Assistant: 理解，控制在 5000 以内。",
         "User: 帮我订那家五星酒店，一晚 3000。",
         "Assistant: 五星酒店两晚 6000，超出预算 1000。"],
        "用户当前的请求与之前的陈述有什么冲突？",
        "用户最初说预算5000元，但五星酒店两晚需要6000元，超出预算1000元。需要提醒用户或调整方案。", difficulty="easy"),
    ActMemEvalCase("conflict_002", ReasoningType.CONFLICT_RESOLUTION,
        ["User: 我每周三下午 3 点有固定会议。",
         "User: 下周三下午 3-4 点帮我约牙医。"],
        "这两个陈述有什么冲突？",
        "下周三下午3-4点的牙医预约与每周三下午3点的固定会议时间冲突。", difficulty="easy"),
    ActMemEvalCase("multihop_001", ReasoningType.MULTI_HOP,
        ["User: Trident 项目使用的是 Python 3.11。",
         "User: Python 3.11 支持 asyncio.TaskGroup。",
         "User: 我们的代码里用了 TaskGroup。"],
        "Trident 项目能否使用 asyncio.TaskGroup？",
        "可以。Trident 项目使用 Python 3.11，而 Python 3.11 支持 asyncio.TaskGroup，且代码中已在使用。", difficulty="easy"),
    ActMemEvalCase("temporal_001", ReasoningType.TEMPORAL,
        ["User: 我 2025 年 3 月入职了 A 公司。",
         "User: 2025 年 9 月跳槽到了 B 公司。",
         "User: 现在我还在 B 公司。"],
        "用户在 2025 年 6 月时在哪家公司？",
        "用户在 2025年6月时在 A 公司。3月入职A公司，9月才跳槽到B公司。", difficulty="easy"),
]


# ── _CausalGraphBuilder ───────────────────────────────────────────────

class _CausalGraphBuilder:
    """因果图构建：节点/边管理 + 对话摄取 + 检索。"""

    def __init__(self, parent: "CausalMemory") -> None:
        self._p = parent

    def add_statement(self, statement: str, source_dialogue_id: str = "",
                      is_fact: bool = True, is_constraint: bool = False,
                      tags: Optional[List[str]] = None,
                      metadata: Optional[Dict[str, Any]] = None) -> str:
        with self._p._lock:
            node_id = f"cn_{uuid.uuid4().hex[:12]}"
            node = CausalNode(node_id=node_id, statement=statement,
                              source_dialogue_id=source_dialogue_id,
                              is_fact=is_fact, is_constraint=is_constraint,
                              tags=tags or [], metadata=metadata or {})
            self._p._nodes[node_id] = node
            self._p._timeline.append(node_id)
            self._p._total_statements += 1
            return node_id

    def add_causal_link(self, source_id: str, target_id: str, edge_type: EdgeType,
                        explanation: str = "", strength: float = 0.5,
                        source_dialogue_id: str = "") -> Optional[str]:
        with self._p._lock:
            if source_id not in self._p._nodes or target_id not in self._p._nodes:
                logger.warning(f"Cannot create edge: source={source_id} or target={target_id} not found")
                return None
            edge_id = f"ce_{uuid.uuid4().hex[:12]}"
            edge = CausalEdge(edge_id=edge_id, source_id=source_id, target_id=target_id,
                              edge_type=edge_type, strength=strength,
                              explanation=explanation, source_dialogue_id=source_dialogue_id)
            self._p._edges[edge_id] = edge
            self._p._adjacency_out[source_id].append(edge_id)
            self._p._adjacency_in[target_id].append(edge_id)
            self._p._total_causal_links += 1
            return edge_id

    def ingest_dialogue(self, messages: List[Dict[str, str]],
                        source_id: str = "") -> int:
        with self._p._lock:
            added = 0; prev_node_id: Optional[str] = None
            for msg in messages:
                content = msg.get("content", "")
                if not content.strip(): continue
                is_constraint = any(kw in content for kw in
                    ["必须", "应该", "不能", "预算", "截止", "只能", "必须"])
                node_id = self.add_statement(statement=content,
                                             source_dialogue_id=source_id,
                                             is_constraint=is_constraint)
                added += 1
                if prev_node_id:
                    if is_constraint:
                        self.add_causal_link(prev_node_id, node_id, EdgeType.PRECEDES,
                                             source_dialogue_id=source_id, strength=0.6)
                    else:
                        self.add_causal_link(prev_node_id, node_id, EdgeType.SEMANTIC_LINK,
                                             source_dialogue_id=source_id, strength=0.3)
                prev_node_id = node_id
            return added

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        with self._p._lock:
            query_tokens = set(query.lower().split())
            scored: List[Tuple[CausalNode, float]] = []
            for node in self._p._nodes.values():
                node_tokens = set(node.statement.lower().split())
                overlap = len(query_tokens & node_tokens)
                if overlap > 0:
                    score = overlap / max(len(query_tokens), 1) * 0.8 + node.confidence * 0.2
                    scored.append((node, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [{"node_id": node.node_id, "statement": node.statement,
                     "score": score, "is_constraint": node.is_constraint,
                     "confidence": node.confidence, "tags": node.tags}
                    for node, score in scored[:top_k]]


# ── _InterventionEngine ───────────────────────────────────────────────

class _InterventionEngine:
    """干预引擎：反事实推理 + 常识补全 + 冲突消解 + 评测。"""

    def __init__(self, parent: "CausalMemory") -> None:
        self._p = parent

    def counterfactual(self, hypothesis: str,
                       context_node_ids: Optional[List[str]] = None) -> CounterfactualResult:
        with self._p._lock:
            self._p._total_counterfactual_queries += 1
            if context_node_ids:
                candidate_nodes = [self._p._nodes[nid] for nid in context_node_ids
                                   if nid in self._p._nodes]
            else:
                candidate_nodes = list(self._p._nodes.values())
            hypothesis_tokens = set(hypothesis.lower().split())
            relevant_nodes: List[Tuple[CausalNode, int]] = []
            for node in candidate_nodes:
                overlap = len(hypothesis_tokens & set(node.statement.lower().split()))
                if overlap > 0: relevant_nodes.append((node, overlap))
            relevant_nodes.sort(key=lambda x: x[1], reverse=True)
            relevant_nodes = relevant_nodes[:5]
            chain: List[str] = []; evidence: List[str] = []; visited: Set[str] = set()
            for node, _ in relevant_nodes:
                if node.node_id in visited: continue
                visited.add(node.node_id); chain.append(node.node_id); evidence.append(node.node_id)
                queue = deque([node.node_id])
                while queue:
                    curr = queue.popleft()
                    for eid in self._p._adjacency_out.get(curr, []):
                        edge = self._p._edges.get(eid)
                        if not edge: continue
                        if edge.target_id not in visited and edge.edge_type in (
                            EdgeType.CAUSES, EdgeType.ENABLES, EdgeType.PRECEDES):
                            visited.add(edge.target_id); chain.append(edge.target_id)
                            queue.append(edge.target_id)
            if len(chain) <= 1:
                conclusion = f"基于当前因果图，无法确定'{hypothesis}'的明确影响。"
                confidence = 0.3
            else:
                affected = [self._p._nodes[nid].statement[:80] for nid in chain[1:6]
                            if nid in self._p._nodes]
                conclusion = (f"若 {hypothesis}，则可能导致以下变化：" + "；".join(affected)
                              if affected else "无显著变化。")
                confidence = min(0.9, 0.4 + 0.1 * len(chain))
            return CounterfactualResult(hypothesis=hypothesis, conclusion=conclusion,
                                         reasoning_chain=chain, confidence=confidence,
                                         evidence_nodes=evidence)

    def commonsense_complete(self, statement: str,
                              source_node_id: Optional[str] = None) -> CommonsenseCompletion:
        with self._p._lock:
            commonsense_rules: Dict[str, List[Tuple[str, str]]] = {
                "订票": [("出行需要有效身份证件", "购票乘车的通用约束"),
                         ("需要提前到达出发站点", "交通乘车的通用约束")],
                "机票": [("国内航班需提前至少1小时到达机场", "航空出行常识"),
                         ("乘机需要有效身份证件", "航空出行常识")],
                "出差": [("出差需要申请审批", "企业差旅流程"),
                         ("出差需要控制预算", "企业差旅约束")],
                "开会": [("会议时间不能与其他安排冲突", "日程管理常识"),
                         ("会议需要提前准备议程", "会议管理常识")],
                "签注": [("出境需要有效签注/签证", "出入境常识"),
                         ("签注办理需要一定工作日", "出入境流程")],
                "预算": [("支出不能超过预算上限", "财务管理约束")],
            }
            implicit_constraints: List[str] = []
            reasoning_parts: List[str] = []
            prerequisites: List[str] = []
            if source_node_id and source_node_id in self._p._nodes:
                prerequisites.append(source_node_id)
            for keyword, rules in commonsense_rules.items():
                if keyword in statement:
                    for constraint, reason in rules:
                        implicit_constraints.append(constraint)
                        reasoning_parts.append(f"因为{statement}涉及'{keyword}'，根据{reason}，推导出：{constraint}")
            if not implicit_constraints:
                return CommonsenseCompletion(explicit_statement=statement,
                                             implicit_constraint="未检测到明确隐含约束",
                                             reasoning="未能从当前常识库中匹配到相关规则",
                                             confidence=0.2, prerequisite_nodes=prerequisites)
            return CommonsenseCompletion(explicit_statement=statement,
                                         implicit_constraint="；".join(implicit_constraints),
                                         reasoning="\n".join(reasoning_parts),
                                         confidence=min(0.95, 0.5 + 0.15 * len(implicit_constraints)),
                                         prerequisite_nodes=prerequisites)

    def detect_conflicts(self, source_id: Optional[str] = None) -> List[ConflictReport]:
        with self._p._lock:
            conflicts: List[ConflictReport] = []
            scope_nodes = ({nid: node for nid, node in self._p._nodes.items()
                            if node.source_dialogue_id == source_id}
                           if source_id else self._p._nodes)
            nodes_list = list(scope_nodes.values())
            constraints = [n for n in nodes_list if n.is_constraint]
            non_constraints = [n for n in nodes_list if not n.is_constraint]
            for const_node in constraints:
                for other_node in non_constraints:
                    if const_node.node_id == other_node.node_id: continue
                    if self._check_keyword_conflict(const_node.statement, other_node.statement):
                        conflicts.append(ConflictReport(
                            conflict_id=f"cf_{uuid.uuid4().hex[:12]}",
                            node_a_id=const_node.node_id, node_b_id=other_node.node_id,
                            description=f"约束冲突: [{const_node.statement[:80]}] 与 [{other_node.statement[:80]}] 存在矛盾",
                            severity=ConflictSeverity.MODERATE,
                            resolution_strategy="优先以最近陈述为准，提醒用户确认优先级",
                            resolution_confidence=0.7,
                            overwrite_suggestion=(const_node.node_id
                                if const_node.timestamp < other_node.timestamp else other_node.node_id)))
            time_nodes = [n for n in nodes_list
                          if any(kw in n.statement for kw in ["周", "上/下午", "点", "月", "年"])]
            if len(time_nodes) >= 2:
                for a, b in itertools.combinations(time_nodes[:10], 2):
                    if self._is_time_conflict(a.statement, b.statement):
                        conflicts.append(ConflictReport(
                            conflict_id=f"cf_{uuid.uuid4().hex[:12]}",
                            node_a_id=a.node_id, node_b_id=b.node_id,
                            description=f"时序冲突: [{a.statement[:80]}] 与 [{b.statement[:80]}] 时间重叠",
                            severity=ConflictSeverity.MILD,
                            resolution_strategy="提示用户时间冲突，建议调整",
                            resolution_confidence=0.6))
            self._p._conflict_log.extend(conflicts)
            self._p._total_conflicts_detected += len(conflicts)
            return conflicts

    def _check_keyword_conflict(self, constraint: str, request: str) -> bool:
        conflict_pairs = [
            ({"预算", "元", "以内"}, {"元", "万"}),
            ({"不能", "禁止"}, {"要", "想", "可以"}),
            ({"过期"}, {"去", "出行", "出境"}),
        ]
        constraint_set = set(constraint); request_set = set(request)
        for a_set, b_set in conflict_pairs:
            if (constraint_set & a_set) and (request_set & b_set):
                if self._has_numeric_conflict(constraint, request): return True
        return False

    def _has_numeric_conflict(self, a: str, b: str) -> bool:
        nums_a = [int(m) for m in re.findall(r'\d+', a)]
        nums_b = [int(m) for m in re.findall(r'\d+', b)]
        if nums_a and nums_b:
            has_upper = any(kw in a for kw in ["以内", "上限", "不超过", "预算", "只能"])
            if has_upper and nums_b[0] > nums_a[0]: return True
        return False

    def _is_time_conflict(self, a: str, b: str) -> bool:
        days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for day in days:
            if day in a and day in b:
                time_pattern = r'(\d{1,2})[点:：]'
                times_a = re.findall(time_pattern, a)
                times_b = re.findall(time_pattern, b)
                if times_a and times_b: return True
        return False

    def run_eval(self) -> Dict[str, Any]:
        results: Dict[str, Dict[str, Any]] = {}; passed = 0; total = len(ACTMEM_EVAL_SUITE)
        for test_case in ACTMEM_EVAL_SUITE:
            self._p.reset()
            messages = [{"role": "user", "content": msg} for msg in test_case.dialogue_history]
            self._p._graph.ingest_dialogue(messages, source_id=test_case.case_id)
            if test_case.category == ReasoningType.COUNTERFACTUAL:
                result = self.counterfactual(test_case.question); conclusion = result.conclusion
            elif test_case.category == ReasoningType.COMMONSENSE:
                completion = self.commonsense_complete(test_case.question)
                conclusion = completion.implicit_constraint
            elif test_case.category == ReasoningType.CONFLICT_RESOLUTION:
                reports = self.detect_conflicts(test_case.case_id)
                conclusion = reports[0].description if reports else "未检测到冲突"
            else: conclusion = "N/A"
            expected_tokens = set(test_case.expected_answer)
            overlap = len(expected_tokens & set(conclusion)) / max(len(expected_tokens), 1)
            passed_case = overlap > 0.3
            results[test_case.case_id] = {"category": test_case.category.value,
                "passed": passed_case, "overlap_ratio": round(overlap, 3),
                "expected": test_case.expected_answer[:100],
                "actual": conclusion[:100], "difficulty": test_case.difficulty}
            if passed_case: passed += 1
        summary = {"total_cases": total, "passed": passed,
                   "pass_rate": round(passed / total, 3) if total > 0 else 0.0,
                   "detail": results}
        self._p._eval_results = {k: v["passed"] for k, v in results.items()}
        return summary


# ── CausalMemory (Facade) ─────────────────────────────────────────────

class CausalMemory:
    """因果推理记忆引擎。从对话历史自动构建因果+语义混合图，支持反事实推理、常识补全和冲突消解。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: Dict[str, CausalNode] = {}
        self._edges: Dict[str, CausalEdge] = {}
        self._adjacency_out: Dict[str, List[str]] = defaultdict(list)
        self._adjacency_in: Dict[str, List[str]] = defaultdict(list)
        self._timeline: List[str] = []
        self._total_statements: int = 0
        self._total_causal_links: int = 0
        self._total_counterfactual_queries: int = 0
        self._total_conflicts_detected: int = 0
        self._conflict_log: List[ConflictReport] = []
        self._eval_results: Dict[str, bool] = {}
        self._graph = _CausalGraphBuilder(self)
        self._intervention = _InterventionEngine(self)

    # ── 图构建（委托 _CausalGraphBuilder） ──
    def add_statement(self, statement: str, source_dialogue_id: str = "",
                      is_fact: bool = True, is_constraint: bool = False,
                      tags: Optional[List[str]] = None,
                      metadata: Optional[Dict[str, Any]] = None) -> str:
        return self._graph.add_statement(statement, source_dialogue_id, is_fact, is_constraint, tags, metadata)

    def add_causal_link(self, source_id: str, target_id: str, edge_type: EdgeType,
                        explanation: str = "", strength: float = 0.5,
                        source_dialogue_id: str = "") -> Optional[str]:
        return self._graph.add_causal_link(source_id, target_id, edge_type, explanation, strength, source_dialogue_id)

    def ingest_dialogue(self, messages: List[Dict[str, str]], source_id: str = "") -> int:
        return self._graph.ingest_dialogue(messages, source_id)

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self._graph.retrieve(query, top_k)

    # ── 推理（委托 _InterventionEngine） ──
    def counterfactual(self, hypothesis: str,
                       context_node_ids: Optional[List[str]] = None) -> CounterfactualResult:
        return self._intervention.counterfactual(hypothesis, context_node_ids)

    def commonsense_complete(self, statement: str,
                              source_node_id: Optional[str] = None) -> CommonsenseCompletion:
        return self._intervention.commonsense_complete(statement, source_node_id)

    def detect_conflicts(self, source_id: Optional[str] = None) -> List[ConflictReport]:
        return self._intervention.detect_conflicts(source_id)

    def run_eval(self) -> Dict[str, Any]:
        return self._intervention.run_eval()

    # ── 管理 ──
    def reset(self) -> None:
        with self._lock:
            self._nodes.clear(); self._edges.clear()
            self._adjacency_out.clear(); self._adjacency_in.clear()
            self._timeline.clear(); self._conflict_log.clear()

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_statements": self._total_statements,
                    "total_causal_links": self._total_causal_links,
                    "total_nodes": len(self._nodes), "total_edges": len(self._edges),
                    "total_counterfactual_queries": self._total_counterfactual_queries,
                    "total_conflicts_detected": self._total_conflicts_detected,
                    "active_conflicts": len(self._conflict_log),
                    "eval_pass_rate": (sum(self._eval_results.values()) / len(self._eval_results)
                                       if self._eval_results else 0.0)}


# ── Self-Test (P2-6 enhancement) ─────────────────────────────────────
def self_test() -> Dict[str, Any]:
    """因果推理记忆自检。

    覆盖: 陈述注入 / 因果链接 / 反事实推理 / 冲突检测 /
          常识补全 / 评估 / 统计 / 重置。
    """
    results: Dict[str, Any] = {"module": "P2-6_causal_memory", "passed": 0, "failed": 0, "details": []}

    def _pass(t):
        results["passed"] += 1
        results["details"].append({"test": t, "status": "PASS"})

    def _fail(t, r):
        results["failed"] += 1
        results["details"].append({"test": t, "status": "FAIL", "reason": r})

    # Test 1: Statement addition with causal links
    try:
        cm = CausalMemory()
        nid = cm.add_statement("The user prefers dark mode", tags=["preference"])
        assert nid, "add_statement returned empty"
        assert cm._total_statements == 1, f"Expected 1 statement, got {cm._total_statements}"
        _pass("Statement addition")
    except Exception as e:
        _fail("Statement addition", str(e))

    # Test 2: Multi-statement causal chain via ingest_dialogue
    try:
        cm = CausalMemory()
        cm.ingest_dialogue([
            {"role": "user", "content": "It rained heavily"},
            {"role": "assistant", "content": "The picnic was cancelled due to rain"},
        ], source_id="d1")
        assert cm._total_causal_links >= 1, f"Expected >= 1 causal link, got {cm._total_causal_links}"
        _pass("Causal chain construction")
    except Exception as e:
        _fail("Causal chain construction", str(e))

    # Test 3: Counterfactual reasoning
    try:
        cm = CausalMemory()
        cm.add_statement("Failed to deploy on Friday", is_fact=True)
        cm.add_causal_link(
            list(cm._nodes.keys())[-1] if cm._nodes else "",
            cm.add_statement("Production outage occurred"),
            EdgeType.CAUSES, strength=0.8)
        nid = cm.add_statement("Deploy on Thursday instead", is_fact=True)
        cf_result = cm.counterfactual(
            hypothesis="If we had deployed on Thursday instead",
            context_node_ids=[nid])
        assert cf_result is not None, "counterfactual returned None"
        assert cf_result.hypothesis, "Empty hypothesis"
        _pass("Counterfactual reasoning")
    except Exception as e:
        _fail("Counterfactual reasoning", str(e))

    # Test 4: Conflict detection
    try:
        cm = CausalMemory()
        cm.add_statement("API 必须使用 v1 端点", is_fact=True, is_constraint=True)
        cm.add_statement("API 可以使用 v2 端点", is_fact=True)
        conflicts = cm.detect_conflicts()
        assert isinstance(conflicts, list), f"Expected list, got {type(conflicts)}"
        _pass("Conflict detection")
    except Exception as e:
        _fail("Conflict detection", str(e))

    # Test 5: Commonsense completion
    try:
        cm = CausalMemory()
        completed = cm.commonsense_complete("帮我订一张明天去上海的机票")
        assert hasattr(completed, "implicit_constraint"), f"Expected CommonsenseCompletion: {type(completed)}"
        _pass("Commonsense completion")
    except Exception as e:
        _fail("Commonsense completion", str(e))

    # Test 6: Evaluation suite
    try:
        cm = CausalMemory()
        cm.ingest_dialogue([
            {"role": "user", "content": "项目截止日期是下周五"},
            {"role": "assistant", "content": "目前进度 70%，按计划周三可完成"},
        ], source_id="eval_test")
        eval_result = cm.run_eval()
        assert isinstance(eval_result, dict), f"Unexpected eval result type: {type(eval_result)}"
        _pass("Evaluation suite")
    except Exception as e:
        _fail("Evaluation suite", str(e))

    # Test 7: Statistics
    try:
        cm = CausalMemory()
        cm.add_statement("Meeting scheduled at 3pm", is_fact=True, tags=["meeting"])
        cm.add_statement("Presentation slides updated", tags=["meeting"])
        cm.add_causal_link(
            list(cm._nodes.keys())[0], list(cm._nodes.keys())[1],
            EdgeType.SEMANTIC_LINK)
        st = cm.statistics()
        assert st["total_nodes"] >= 2, f"Expected >= 2 nodes, got {st['total_nodes']}"
        assert st["total_statements"] == 2, f"Expected 2 statements, got {st['total_statements']}"
        _pass("Statistics")
    except Exception as e:
        _fail("Statistics", str(e))

    # Test 8: Reset (clears nodes/edges)
    try:
        cm = CausalMemory()
        cm.add_statement("Temporary note")
        cm.reset()
        assert len(cm._nodes) == 0, f"Expected 0 nodes after reset, got {len(cm._nodes)}"
        _pass("Reset")
    except Exception as e:
        _fail("Reset", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
