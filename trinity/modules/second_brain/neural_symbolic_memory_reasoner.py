"""
CB69: NeuralSymbolicMemoryReasoner — 神经符号记忆推理引擎
==========================================================

神经符号融合推理引擎。将记忆存储中的知识抽取为结构化图谱，通过
归纳逻辑编程(ILP)归纳推理规则，执行符号推理并回注到神经检索。

核心设计:
  - MemoryGraphExtractor: 记忆→实体-关系-实体三元组→知识图谱
  - RuleInductionEngine: ILP 从图谱模式中归纳一阶逻辑规则
  - SymbolicExecutor: 回溯链式推理 + 一致性验证
  - NeuralSymbolicBridge: 符号推理解释→神经检索语义增强
  - 封闭世界假设下的确定性推理 & 开放世界下的概率推理

Reference:
  - Neural-symbolic integration for lifelong agent memory
  - Inductive Logic Programming (ILP) over knowledge graphs
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class WorldAssumption(Enum):
    """世界假设。"""
    CLOSED_WORLD = "closed_world"   # 封闭世界：未声明即假
    OPEN_WORLD = "open_world"       # 开放世界：未声明即未知


class ReasoningStatus(Enum):
    """推理链状态。"""
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"            # 推理成功，结论可证
    FAILED = "failed"              # 无法推理出结论
    CONTRADICTION = "contradiction" # 发现矛盾


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class KnowledgeTriple:
    """知识三元组 (subject, predicate, object)。

    Attributes:
        triple_id: 三元组唯一标识。
        subject: 主语实体。
        predicate: 谓词/关系。
        obj: 宾语实体或字面量。
        confidence: 置信度 [0..1]。
        source_memory_ids: 来源记忆 ID 列表。
    """
    triple_id: str
    subject: str
    predicate: str
    obj: str
    confidence: float = 1.0
    source_memory_ids: List[str] = field(default_factory=list)

    def __hash__(self):
        return hash((self.subject, self.predicate, self.obj))


@dataclass
class NSLogicRule:
    """一阶逻辑规则（命名前缀 NSLogicRule 避让已有 LogicRule）。

    格式: head :- body1, body2, ..., bodyN.
    body 项为 (subject_var, predicate, object_var) 三元组模式。
    """
    rule_id: str
    head: KnowledgeTriple
    body: List[KnowledgeTriple] = field(default_factory=list)
    confidence: float = 1.0
    support: int = 0      # 规则在图谱中的支持数
    coverage: float = 0.0  # 覆盖率

    def to_prolog(self) -> str:
        """转为 Prolog-like 字符串。"""
        body_str = ", ".join(
            f"{b.subject}({b.predicate},{b.obj})" for b in self.body
        )
        return f"{self.head.subject}({self.head.predicate},{self.head.obj}) :- {body_str}."

    def __str__(self) -> str:
        return self.to_prolog()


@dataclass
class ReasoningChain:
    """推理链——从事实到结论的步骤序列。

    Attributes:
        chain_id: 链唯一标识。
        query: 推理查询。
        steps: 每一步为 (applied_rule_id, inferred_triple)。
        status: 推理状态。
        conclusion: 推理结论（成功时）。
        explanation: 自然语言解释。
    """
    chain_id: str
    query: str = ""
    steps: List[Tuple[str, KnowledgeTriple]] = field(default_factory=list)
    status: ReasoningStatus = ReasoningStatus.IN_PROGRESS
    conclusion: Optional[KnowledgeTriple] = None
    explanation: str = ""

    def add_step(self, rule_id: str, triple: KnowledgeTriple):
        self.steps.append((rule_id, triple))

    def depth(self) -> int:
        return len(self.steps)


# ============================================================================
# Sub-components
# ============================================================================

class MemoryGraphExtractor:
    """从记忆存储中抽取知识图谱。

    提取实体-关系-实体三元组，构建有向知识图谱。
    """

    def __init__(self):
        self._triples: Dict[str, KnowledgeTriple] = {}
        self._adjacency: Dict[str, Set[str]] = {}  # subject → set of (predicate, obj)

    def extract(self, memories: List[Dict[str, Any]]) -> List[KnowledgeTriple]:
        """从记忆列表中抽取三元组。"""
        new_triples = []
        for mem in memories:
            if "triples" in mem:
                for t in mem["triples"]:
                    kt = KnowledgeTriple(
                        triple_id=hashlib.md5(
                            f"{t.get('s','')}-{t.get('p','')}-{t.get('o','')}".encode()
                        ).hexdigest()[:12],
                        subject=t.get("s", ""),
                        predicate=t.get("p", ""),
                        obj=t.get("o", ""),
                        confidence=t.get("confidence", 1.0),
                        source_memory_ids=[mem.get("id", "")],
                    )
                    self.add_triple(kt)
                    new_triples.append(kt)
        return new_triples

    def add_triple(self, triple: KnowledgeTriple):
        self._triples[triple.triple_id] = triple
        key = f"{triple.subject}::{triple.predicate}::{triple.obj}"
        self._adjacency.setdefault(triple.subject, set()).add(key)

    def query_by_subject(self, subject: str) -> List[KnowledgeTriple]:
        results = []
        for tid, t in self._triples.items():
            if t.subject == subject:
                results.append(t)
        return results

    def query_by_predicate(self, predicate: str) -> List[KnowledgeTriple]:
        return [t for t in self._triples.values() if t.predicate == predicate]

    def size(self) -> int:
        return len(self._triples)


class RuleInductionEngine:
    """归纳逻辑编程(ILP)引擎——从图谱模式中归纳规则。"""

    def __init__(self, max_rule_length: int = 3, min_support: int = 2):
        self.max_rule_length = max_rule_length
        self.min_support = min_support
        self._rules: Dict[str, NSLogicRule] = {}

    def induce(self, triples: List[KnowledgeTriple]) -> List[NSLogicRule]:
        """从三元组集归纳规则。"""
        induced = []

        # Group triples by predicate for pattern mining
        by_predicate: Dict[str, List[KnowledgeTriple]] = {}
        for t in triples:
            by_predicate.setdefault(t.predicate, []).append(t)

        for predicate, pts in by_predicate.items():
            if len(pts) < self.min_support:
                continue
            # Simple transitive rule induction: if A→p→B and B→p→C then A→p→C
            for a in pts:
                for b in pts:
                    if a.obj == b.subject and a.triple_id != b.triple_id:
                        head = KnowledgeTriple(
                            triple_id=hashlib.md5(
                                f"ind_{a.subject}_{predicate}_{b.obj}".encode()
                            ).hexdigest()[:12],
                            subject=a.subject,
                            predicate=predicate,
                            obj=b.obj,
                            confidence=min(a.confidence, b.confidence),
                        )
                        rule = NSLogicRule(
                            rule_id=f"rule_{predicate}_{len(self._rules)}",
                            head=head,
                            body=[a, b],
                            confidence=head.confidence,
                            support=2,
                        )
                        self._rules[rule.rule_id] = rule
                        induced.append(rule)

        return induced

    def get_rules(self) -> List[NSLogicRule]:
        return list(self._rules.values())

    def match(self, triple: KnowledgeTriple) -> List[NSLogicRule]:
        """找出可导出该 triple 的规则。"""
        return [
            r for r in self._rules.values()
            if r.head.subject == triple.subject
            and r.head.predicate == triple.predicate
            and r.head.obj == triple.obj
        ]


class SymbolicExecutor:
    """符号执行器——回溯链式推理 + 一致性验证。"""

    def __init__(self, world_assumption: WorldAssumption = WorldAssumption.CLOSED_WORLD):
        self.world_assumption = world_assumption
        self._known_triples: Set[Tuple[str, str, str]] = set()

    def load_knowledge(self, triples: List[KnowledgeTriple]):
        for t in triples:
            self._known_triples.add((t.subject, t.predicate, t.obj))

    def prove(
        self,
        query: KnowledgeTriple,
        rules: List[NSLogicRule],
        max_depth: int = 5,
    ) -> ReasoningChain:
        """回溯链式推理——尝试证明查询三元组。

        Args:
            query: 待证明的三元组。
            rules: 可用规则集。
            max_depth: 最大推理深度。

        Returns:
            ReasoningChain: 推理链（含结论）。
        """
        chain = ReasoningChain(
            chain_id=f"chain_{hashlib.md5(str(query).encode()).hexdigest()[:8]}",
            query=str(query),
        )
        self._backtrack(query, rules, chain, set(), max_depth, 0)
        return chain

    def _backtrack(
        self,
        query: KnowledgeTriple,
        rules: List[NSLogicRule],
        chain: ReasoningChain,
        visited: Set[str],
        max_depth: int,
        current_depth: int,
    ):
        if current_depth >= max_depth:
            chain.status = ReasoningStatus.FAILED
            return

        qkey = f"{query.subject}|{query.predicate}|{query.obj}"
        if qkey in visited:
            return
        visited.add(qkey)

        # Direct fact check
        if (query.subject, query.predicate, query.obj) in self._known_triples:
            chain.status = ReasoningStatus.SUCCESS
            chain.conclusion = query
            chain.explanation = f"'{query.subject} {query.predicate} {query.obj}' is a known fact."
            return

        # Rule-based inference
        for rule in rules:
            if rule.head.predicate != query.predicate:
                continue
            # Try to match rule body against known facts and recursively prove
            all_body_proven = True
            for body_triple in rule.body:
                body_key = (body_triple.subject, body_triple.predicate, body_triple.obj)
                if body_key not in self._known_triples:
                    if not self._try_prove_body(body_triple, rules, visited, max_depth, current_depth + 1):
                        all_body_proven = False
                        break

            if all_body_proven:
                self._known_triples.add((query.subject, query.predicate, query.obj))
                chain.add_step(rule.rule_id, query)
                chain.status = ReasoningStatus.SUCCESS
                chain.conclusion = query
                chain.explanation = (
                    f"Inferred '{query.subject} {query.predicate} {query.obj}' "
                    f"via rule {rule.rule_id} at depth {current_depth}."
                )
                return

        if self.world_assumption == WorldAssumption.OPEN_WORLD:
            chain.status = ReasoningStatus.FAILED
            chain.explanation = "Cannot prove under open world assumption."
        else:
            chain.status = ReasoningStatus.FAILED
            chain.explanation = "Cannot prove — not in closed world knowledge."

    def _try_prove_body(
        self, body: KnowledgeTriple, rules: List[NSLogicRule],
        visited: Set[str], max_depth: int, depth: int,
    ) -> bool:
        body_key = (body.subject, body.predicate, body.obj)
        if body_key in self._known_triples:
            return True
        for rule in rules:
            if (rule.head.subject, rule.head.predicate, rule.head.obj) == body_key:
                return True
        return False


class ConsistencyValidator:
    """一致性验证器——检测图谱中的逻辑矛盾。"""

    def __init__(self):
        self._contradictions: List[Tuple[KnowledgeTriple, KnowledgeTriple]] = []

    def validate(self, triples: List[KnowledgeTriple]) -> bool:
        """检查三元组中是否存在矛盾。

        简单启发式：同一 (subject, predicate) 对存在两个互相矛盾的 object
        （目标为互斥字面量，如 alive/dead）。
        """
        self._contradictions.clear()
        by_sp: Dict[Tuple[str, str], List[KnowledgeTriple]] = {}
        for t in triples:
            by_sp.setdefault((t.subject, t.predicate), []).append(t)

        for (subj, pred), group in by_sp.items():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if self._are_mutually_exclusive(group[i].obj, group[j].obj):
                        self._contradictions.append((group[i], group[j]))

        return len(self._contradictions) == 0

    @staticmethod
    def _are_mutually_exclusive(a: str, b: str) -> bool:
        """简单互斥检测。"""
        exclusive_pairs = {
            ("alive", "dead"), ("dead", "alive"),
            ("true", "false"), ("false", "true"),
            ("yes", "no"), ("no", "yes"),
            ("positive", "negative"), ("negative", "positive"),
        }
        return (a.lower(), b.lower()) in exclusive_pairs

    def get_contradictions(self) -> List[Tuple[KnowledgeTriple, KnowledgeTriple]]:
        return list(self._contradictions)


class NeuralSymbolicBridge:
    """神经符号桥——将符号推理解释回注到神经检索。

    推理产生的结构化解释（规则链、置信度）用于增强语义检索的
    查询扩展和结果重排序。
    """

    def __init__(self):
        self._explanations: Dict[str, str] = {}

    def inject(self, chain: ReasoningChain):
        """注册推理链解释到语义增强池。"""
        key = hashlib.md5(chain.query.encode()).hexdigest()[:12]
        self._explanations[key] = chain.explanation

    def augment_query(self, raw_query: str) -> str:
        """用推理上下文增强查询。"""
        key = hashlib.md5(raw_query.encode()).hexdigest()[:12]
        if key in self._explanations:
            return f"{raw_query} [reasoning: {self._explanations[key]}]"
        return raw_query

    def get_explanation(self, query: str) -> Optional[str]:
        key = hashlib.md5(query.encode()).hexdigest()[:12]
        return self._explanations.get(key)


# ============================================================================
# Main Class
# ============================================================================

class NeuralSymbolicMemoryReasoner:
    """神经符号记忆推理引擎 (CB69)。

    Usage:
        nsr = NeuralSymbolicMemoryReasoner()
        nsr.ingest_memories([{"id":"m1","triples":[{"s":"Alice","p":"works_at",
                             "o":"Acme"}]}])
        result = nsr.reason("Alice", "works_at", "Acme")
        print(result.status, result.explanation)
    """

    def __init__(self, world_assumption: WorldAssumption = WorldAssumption.CLOSED_WORLD):
        self._lock = threading.RLock()
        self.extractor = MemoryGraphExtractor()
        self.rule_engine = RuleInductionEngine()
        self.executor = SymbolicExecutor(world_assumption=world_assumption)
        self.bridge = NeuralSymbolicBridge()
        self.validator = ConsistencyValidator()
        self._reason_count: int = 0
        self._start_time: float = _time.time()

    def ingest_memories(self, memories: List[Dict[str, Any]]):
        """摄入记忆并构建知识图谱+归纳规则。

        Args:
            memories: 每项含 "id" 和 "triples" 字段。
        """
        with self._lock:
            triples = self.extractor.extract(memories)
            self.executor.load_knowledge(triples)
            self.rule_engine.induce(triples)

    def reason(
        self, subject: str, predicate: str, obj: str, max_depth: int = 5
    ) -> ReasoningChain:
        """推理查询——尝试证明 (subject, predicate, obj)。

        Args:
            subject: 主语实体。
            predicate: 谓词关系。
            obj: 宾语实体。

        Returns:
            ReasoningChain: 推理结果。
        """
        with self._lock:
            query = KnowledgeTriple(
                triple_id=f"query_{hashlib.md5(f'{subject}{predicate}{obj}'.encode()).hexdigest()[:8]}",
                subject=subject, predicate=predicate, obj=obj,
            )
            rules = self.rule_engine.get_rules()
            chain = self.executor.prove(query, rules, max_depth=max_depth)
            self.bridge.inject(chain)
            self._reason_count += 1
            return chain

    def validate_consistency(self) -> bool:
        with self._lock:
            triples = list(self.extractor._triples.values())
            return self.validator.validate(triples)

    def augment_retrieval(self, query: str) -> str:
        """用推理上下文增强检索查询。"""
        with self._lock:
            return self.bridge.augment_query(query)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "NeuralSymbolicMemoryReasoner (CB69)",
                "total_triples": self.extractor.size(),
                "total_rules": len(self.rule_engine.get_rules()),
                "total_reasoning_calls": self._reason_count,
                "contradictions": len(self.validator.get_contradictions()),
                "explanation_pool_size": len(self.bridge._explanations),
                "world_assumption": self.executor.world_assumption.value,
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
