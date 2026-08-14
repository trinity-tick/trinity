"""
P11-8: Cross-Agent Synthesis Protocol — 对标 Silo-Bench 跨智能体知识合成

实现多 Agent 独立推理结果的统一融合:
  - CrossAgentMessageBus: 跨 Agent 异步消息总线（Pub/Sub）
  - synthesis_node(): 将多 Agent 独立推理结果融合为统一知识表示
  - conflict_resolution(): 基于置信度加权消解冲突
  - consensus_threshold: 控制融合门槛，达到阈值才接受为共识
  - 支持多轮迭代融合 + 打破信息孤岛 (Silo)

Reference:
    Silo-Bench: Breaking Knowledge Silos in Multi-Agent Systems (2026)
    Multi-Agent Debate & Consensus Building (DeepMind, 2025)
"""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class MessageType(Enum):
    """跨 Agent 消息类型。"""
    PROPOSAL = "proposal"                # 新的知识提案
    EVIDENCE = "evidence"                # 证据/支撑数据
    REBUTTAL = "rebuttal"                # 反驳
    REFINEMENT = "refinement"            # 改进建议
    CONSENSUS_REACHED = "consensus_reached"  # 共识达成
    CONFLICT_DECLARED = "conflict_declared"  # 冲突声明
    SYNTHESIS_COMPLETE = "synthesis_complete"


class ConflictStrategy(Enum):
    """冲突消解策略。"""
    MAJORITY_VOTE = "majority_vote"           # 多数投票
    WEIGHTED_CONFIDENCE = "weighted_confidence"  # 置信度加权
    EXPERT_TRUST = "expert_trust"             # 专家信任
    EVIDENCE_DRIVEN = "evidence_driven"       # 证据驱动
    HIERARCHICAL = "hierarchical"             # 层级仲裁


class SynthesisStatus(Enum):
    """合成状态。"""
    COLLECTING = "collecting"       # 收集各方推理
    DEBATING = "debating"           # 辩论中
    RESOLVING = "resolving"         # 冲突消解中
    CONSENSUS = "consensus"         # 达成共识
    DEADLOCK = "deadlock"           # 僵局（需人工介入）


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AgentMessage:
    """跨 Agent 消息。"""
    message_id: str
    sender_id: str
    message_type: MessageType
    topic: str                          # 讨论主题/知识域
    content: dict                       # 提案/证据/反驳的具体内容
    confidence: float = 0.0             # 置信度 0~1
    timestamp: float = field(default_factory=time.time)
    references: list[str] = field(default_factory=list)  # 引用的消息 ID
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "message_type": self.message_type.value,
            "topic": self.topic,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "references": self.references,
        }


@dataclass
class SynthesisResult:
    """合成结果。"""
    topic: str
    status: SynthesisStatus
    consensus_content: dict | None = None   # 达成共识的统一知识表示
    confidence: float = 0.0                  # 融合后置信度
    participating_agents: list[str] = field(default_factory=list)
    conflicting_points: list[dict] = field(default_factory=list)  # 冲突点列表
    resolution_log: list[dict] = field(default_factory=list)      # 消解过程日志
    iterations: int = 0                      # 迭代轮次
    timestamp: float = field(default_factory=time.time)
    message_count: int = 0


@dataclass
class ConflictPoint:
    """冲突点。"""
    field_name: str                         # 冲突的字段/知识项
    proposals: dict[str, Any]               # agent_id -> proposed_value
    confidences: dict[str, float]           # agent_id -> confidence
    resolved_value: Any = None              # 消解后的值
    resolution_strategy: ConflictStrategy = ConflictStrategy.WEIGHTED_CONFIDENCE


# ══════════════════════════════════════════════════════════════════════
# 跨 Agent 消息总线
# ══════════════════════════════════════════════════════════════════════

class CrossAgentMessageBus:
    """跨 Agent 消息总线 (Pub/Sub 模式)。

    支持主题订阅、异步消息传递、消息持久化。
    """

    def __init__(self):
        self._lock = RLock()
        self._messages: list[AgentMessage] = []
        self._subscriptions: dict[str, set[str]] = defaultdict(set)  # agent_id -> {topic, ...}
        self._topic_messages: dict[str, list[AgentMessage]] = defaultdict(list)
        self._message_counter: int = 0

    def subscribe(self, agent_id: str, topic: str) -> None:
        """Agent 订阅某主题。"""
        with self._lock:
            self._subscriptions[agent_id].add(topic)

    def unsubscribe(self, agent_id: str, topic: str) -> None:
        """取消订阅。"""
        with self._lock:
            self._subscriptions[agent_id].discard(topic)

    def publish(self, sender_id: str, message_type: MessageType,
                topic: str, content: dict, confidence: float = 0.0,
                references: list[str] | None = None) -> AgentMessage:
        """发布消息到总线。"""
        with self._lock:
            self._message_counter += 1
            msg_id = f"MSG-{self._message_counter:08d}"
            msg = AgentMessage(
                message_id=msg_id,
                sender_id=sender_id,
                message_type=message_type,
                topic=topic,
                content=content,
                confidence=confidence,
                references=references or [],
            )
            self._messages.append(msg)
            self._topic_messages[topic].append(msg)
            return msg

    def get_messages(self, topic: str, since_timestamp: float = 0.0) -> list[AgentMessage]:
        """获取某主题下的消息。"""
        with self._lock:
            return [m for m in self._topic_messages.get(topic, [])
                    if m.timestamp >= since_timestamp]

    def get_subscribers(self, topic: str) -> list[str]:
        """获取某主题的订阅者。"""
        return [aid for aid, topics in self._subscriptions.items() if topic in topics]

    def get_agent_topics(self, agent_id: str) -> list[str]:
        """获取某 Agent 订阅的所有主题。"""
        return list(self._subscriptions.get(agent_id, set()))

    def topic_stats(self) -> dict:
        with self._lock:
            return {
                topic: len(msgs)
                for topic, msgs in self._topic_messages.items()
            }


# ══════════════════════════════════════════════════════════════════════
# 冲突消解器
# ══════════════════════════════════════════════════════════════════════

class ConflictResolver:
    """基于置信度加权的冲突消解器。"""

    def __init__(self, strategy: ConflictStrategy = ConflictStrategy.WEIGHTED_CONFIDENCE):
        self.strategy = strategy
        self._agent_reputation: dict[str, float] = defaultdict(lambda: 0.5)  # 0~1

    def set_reputation(self, agent_id: str, score: float) -> None:
        self._agent_reputation[agent_id] = max(0.0, min(1.0, score))

    def resolve(self, conflicts: list[ConflictPoint]) -> tuple[dict, list[dict]]:
        """消解冲突，返回统一结果和消解日志。"""
        resolved = {}
        log = []

        for cp in conflicts:
            if self.strategy == ConflictStrategy.WEIGHTED_CONFIDENCE:
                value, entry = self._weighted_confidence_resolve(cp)
            elif self.strategy == ConflictStrategy.MAJORITY_VOTE:
                value, entry = self._majority_vote_resolve(cp)
            elif self.strategy == ConflictStrategy.EXPERT_TRUST:
                value, entry = self._expert_trust_resolve(cp)
            else:
                value, entry = self._weighted_confidence_resolve(cp)

            cp.resolved_value = value
            cp.resolution_strategy = self.strategy
            resolved[cp.field_name] = value
            log.append(entry)

        return resolved, log

    def _weighted_confidence_resolve(self, cp: ConflictPoint) -> tuple[Any, dict]:
        """置信度加权消解。

        对于标量值：加权平均
        对于非标量值：选择加权分数最高者
        """
        best_agent = None
        best_score = -1.0
        weighted_sum = 0.0
        total_weight = 0.0

        for agent_id, value in cp.proposals.items():
            conf = cp.confidences.get(agent_id, 0.5)
            rep = self._agent_reputation.get(agent_id, 0.5)
            weight = conf * 0.6 + rep * 0.4  # 60% 置信度 + 40% 声誉

            if isinstance(value, (int, float)):
                weighted_sum += value * weight
                total_weight += weight
            if weight > best_score:
                best_score = weight
                best_agent = agent_id

        # 标量：加权平均
        all_scalar = all(isinstance(v, (int, float)) for v in cp.proposals.values())
        if all_scalar and total_weight > 0:
            resolved = weighted_sum / total_weight
        else:
            resolved = cp.proposals.get(best_agent, list(cp.proposals.values())[0])

        entry = {
            "field": cp.field_name,
            "strategy": "weighted_confidence",
            "proposals": {k: str(v)[:50] for k, v in cp.proposals.items()},
            "resolved": str(resolved)[:50],
            "best_agent": best_agent,
            "best_score": round(best_score, 3),
        }
        return resolved, entry

    def _majority_vote_resolve(self, cp: ConflictPoint) -> tuple[Any, dict]:
        """多数投票消解。"""
        from collections import Counter
        values = list(cp.proposals.values())
        # 非标量用字符串表示投票
        if all(isinstance(v, (int, float)) for v in values):
            counter = Counter(values)
        else:
            counter = Counter(str(v) for v in values)

        most_common = counter.most_common(1)
        if most_common:
            # 找到对应原始值
            winner_str = most_common[0][0]
            for agent_id, value in cp.proposals.items():
                if str(value) == winner_str:
                    entry = {
                        "field": cp.field_name,
                        "strategy": "majority_vote",
                        "votes": dict(counter),
                        "winner": winner_str,
                    }
                    return value, entry

        # Fallback
        fallback = list(cp.proposals.values())[0]
        return fallback, {"field": cp.field_name, "strategy": "majority_vote", "fallback": True}

    def _expert_trust_resolve(self, cp: ConflictPoint) -> tuple[Any, dict]:
        """专家信任消解：选声誉最高的 Agent 的提案。"""
        best_agent = max(cp.proposals.keys(),
                         key=lambda a: self._agent_reputation.get(a, 0.5))
        return cp.proposals[best_agent], {
            "field": cp.field_name,
            "strategy": "expert_trust",
            "chosen_expert": best_agent,
            "reputation": self._agent_reputation.get(best_agent, 0.5),
        }


# ══════════════════════════════════════════════════════════════════════
# Cross-Agent Synthesis 主类
# ══════════════════════════════════════════════════════════════════════

class CrossAgentSynthesis:
    """跨 Agent 知识合成引擎。

    核心流程：
    1. 多 Agent 通过 MessageBus 交换各自推理结果
    2. synthesis_node() 聚合各方提案
    3. conflict_resolution() 消解冲突
    4. consensus_threshold 判断是否达成共识
    5. 未达共识时触发多轮迭代辩论
    """

    def __init__(self, consensus_threshold: float = 0.7,
                 max_iterations: int = 5,
                 strategy: ConflictStrategy = ConflictStrategy.WEIGHTED_CONFIDENCE):
        self.consensus_threshold = consensus_threshold
        self.max_iterations = max_iterations
        self.bus = CrossAgentMessageBus()
        self.resolver = ConflictResolver(strategy)
        self._synthesis_history: list[SynthesisResult] = []

    # ── 核心：合成节点 ────────────────────────────────────────────

    def synthesis_node(self, topic: str, agent_proposals: dict[str, dict],
                       agent_confidences: dict[str, float] | None = None) -> SynthesisResult:
        """将多 Agent 独立推理结果融合为统一知识表示。

        Args:
            topic: 知识主题
            agent_proposals: {agent_id: {field: value, ...}} 各方提案
            agent_confidences: {agent_id: confidence} 各方置信度

        Returns:
            SynthesisResult with consensus_content if threshold met
        """
        agent_confidences = agent_confidences or {}
        agents = list(agent_proposals.keys())

        # 订阅主题
        for agent_id in agents:
            self.bus.subscribe(agent_id, topic)

        # 各 agent 发布提案
        for agent_id, proposal in agent_proposals.items():
            conf = agent_confidences.get(agent_id, 0.5)
            self.bus.publish(
                sender_id=agent_id,
                message_type=MessageType.PROPOSAL,
                topic=topic,
                content=proposal,
                confidence=conf,
            )

        result = SynthesisResult(
            topic=topic,
            status=SynthesisStatus.COLLECTING,
            participating_agents=agents,
            message_count=len(agents),
        )

        # 迭代融合
        for iteration in range(self.max_iterations):
            result.iterations = iteration + 1

            # 收集当前轮所有消息
            messages = self.bus.get_messages(topic)
            if not messages:
                break

            # 检测冲突
            conflicts = self._detect_conflicts(messages, agent_confidences)
            result.conflicting_points = [
                {"field": c.field_name, "proposals": {k: str(v)[:50] for k, v in c.proposals.items()}}
                for c in conflicts
            ]

            if not conflicts:
                # 无冲突 = 完全一致
                merged = self._merge_proposals(messages)
                result.status = SynthesisStatus.CONSENSUS
                result.consensus_content = merged
                result.confidence = self._compute_consensus_confidence(messages, agent_confidences)
                break

            # 冲突消解
            resolved_fields, resolution_log = self.resolver.resolve(conflicts)
            result.resolution_log.extend(resolution_log)

            # 合并消解后的结果
            merged = self._merge_resolved(messages, resolved_fields)
            consensus_conf = self._compute_consensus_confidence(messages, agent_confidences)

            if consensus_conf >= self.consensus_threshold:
                result.status = SynthesisStatus.CONSENSUS
                result.consensus_content = merged
                result.confidence = consensus_conf
                self.bus.publish(
                    sender_id="synthesis_engine",
                    message_type=MessageType.CONSENSUS_REACHED,
                    topic=topic,
                    content=merged,
                    confidence=consensus_conf,
                )
                break
            elif iteration < self.max_iterations - 1:
                # 通知各方还有冲突，触发下一轮辩论
                result.status = SynthesisStatus.DEBATING
                self.bus.publish(
                    sender_id="synthesis_engine",
                    message_type=MessageType.CONFLICT_DECLARED,
                    topic=topic,
                    content={"conflicts": result.conflicting_points},
                    confidence=consensus_conf,
                )
            else:
                result.status = SynthesisStatus.DEADLOCK
                result.consensus_content = merged
                result.confidence = consensus_conf

        # 最终广播
        self.bus.publish(
            sender_id="synthesis_engine",
            message_type=MessageType.SYNTHESIS_COMPLETE,
            topic=topic,
            content={"status": result.status.value, "confidence": result.confidence},
            confidence=result.confidence,
        )

        self._synthesis_history.append(result)
        return result

    # ── 冲突检测 ──────────────────────────────────────────────────

    def _detect_conflicts(self, messages: list[AgentMessage],
                          agent_confidences: dict[str, float]) -> list[ConflictPoint]:
        """检测各 Agent 提案间的冲突点。"""
        # 收集所有字段
        all_fields: dict[str, dict[str, Any]] = defaultdict(dict)
        all_confidences: dict[str, dict[str, float]] = defaultdict(dict)

        for msg in messages:
            if msg.message_type != MessageType.PROPOSAL:
                continue
            for field, value in msg.content.items():
                all_fields[field][msg.sender_id] = value
                all_confidences[field][msg.sender_id] = msg.confidence

        conflicts = []
        for field, proposals in all_fields.items():
            unique_values = set(
                str(v) if not isinstance(v, (int, float)) else v
                for v in proposals.values()
            )
            if len(unique_values) > 1:
                conflicts.append(ConflictPoint(
                    field_name=field,
                    proposals=dict(proposals),
                    confidences=dict(all_confidences[field]),
                ))

        return conflicts

    def _merge_proposals(self, messages: list[AgentMessage]) -> dict:
        """无冲突时直接合并提案。"""
        merged = {}
        for msg in messages:
            if msg.message_type == MessageType.PROPOSAL:
                for field, value in msg.content.items():
                    if field not in merged:
                        merged[field] = value
        return merged

    def _merge_resolved(self, messages: list[AgentMessage],
                        resolved_fields: dict) -> dict:
        """合并消解后的字段。"""
        merged = self._merge_proposals(messages)
        merged.update(resolved_fields)
        return merged

    def _compute_consensus_confidence(self, messages: list[AgentMessage],
                                      agent_confidences: dict[str, float]) -> float:
        """计算共识置信度。"""
        proposals = [m for m in messages if m.message_type == MessageType.PROPOSAL]
        if not proposals:
            return 0.0
        confidences = [m.confidence for m in proposals]
        agent_rep_scores = [
            self.resolver._agent_reputation.get(m.sender_id, 0.5)
            for m in proposals
        ]
        # 置信度 60% + 声誉 40%
        combined = [
            0.6 * confidences[i] + 0.4 * agent_rep_scores[i]
            for i in range(len(proposals))
        ]
        return sum(combined) / len(combined) if combined else 0.0

    # ── 冲突消解 ──────────────────────────────────────────────────

    def conflict_resolution(self, topic: str,
                            strategy: ConflictStrategy | None = None) -> dict:
        """对外暴露的冲突消解接口。

        基于置信度加权消解指定主题下的冲突。
        """
        if strategy:
            self.resolver.strategy = strategy

        messages = self.bus.get_messages(topic)
        agent_confidences = {m.sender_id: m.confidence for m in messages}

        conflicts = self._detect_conflicts(messages, agent_confidences)
        if not conflicts:
            merged = self._merge_proposals(messages)
            return {"status": "no_conflict", "merged": merged}

        resolved, log = self.resolver.resolve(conflicts)
        merged = self._merge_resolved(messages, resolved)
        return {
            "status": "resolved",
            "merged": merged,
            "resolution_log": log,
            "conflict_count": len(conflicts),
        }

    # ── 工具 ──────────────────────────────────────────────────────

    def set_consensus_threshold(self, threshold: float) -> None:
        """动态调整共识门槛。"""
        self.consensus_threshold = max(0.0, min(1.0, threshold))

    def get_synthesis_history(self) -> list[SynthesisResult]:
        return list(self._synthesis_history)

    # ── 统计 ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        total = len(self._synthesis_history)
        consensus_count = sum(
            1 for r in self._synthesis_history
            if r.status == SynthesisStatus.CONSENSUS
        )
        deadlock_count = sum(
            1 for r in self._synthesis_history
            if r.status == SynthesisStatus.DEADLOCK
        )
        avg_confidence = (
            sum(r.confidence for r in self._synthesis_history) / total
            if total > 0 else 0.0
        )
        return {
            "total_syntheses": total,
            "consensus_reached": consensus_count,
            "consensus_rate": round(consensus_count / total, 3) if total > 0 else 0.0,
            "deadlocks": deadlock_count,
            "avg_confidence": round(avg_confidence, 3),
            "avg_iterations": round(
                sum(r.iterations for r in self._synthesis_history) / total, 1
            ) if total > 0 else 0.0,
            "consensus_threshold": self.consensus_threshold,
            "max_iterations": self.max_iterations,
            "message_bus_stats": self.bus.topic_stats(),
            "resolution_strategy": self.resolver.strategy.value,
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    engine = CrossAgentSynthesis(consensus_threshold=0.6, max_iterations=3)

    # 模拟 3 个 Agent 对同一主题的独立推理
    proposals = {
        "agent_alpha": {
            "conclusion": "The system should use vector search",
            "confidence_score": 0.85,
            "recommended_model": "all-MiniLM-L6-v2",
            "latency_ms": 12,
        },
        "agent_beta": {
            "conclusion": "The system should use hybrid search",
            "confidence_score": 0.78,
            "recommended_model": "all-mpnet-base-v2",
            "latency_ms": 25,
        },
        "agent_gamma": {
            "conclusion": "The system should use vector search",
            "confidence_score": 0.90,
            "recommended_model": "all-MiniLM-L6-v2",
            "latency_ms": 15,  # slightly different
        },
    }

    confidences = {
        "agent_alpha": 0.85,
        "agent_beta": 0.78,
        "agent_gamma": 0.90,
    }

    # 设置声誉
    engine.resolver.set_reputation("agent_alpha", 0.9)
    engine.resolver.set_reputation("agent_beta", 0.6)
    engine.resolver.set_reputation("agent_gamma", 0.7)

    print("=" * 60)
    print("Cross-Agent Synthesis — Self Test")
    print("=" * 60)

    result = engine.synthesis_node(
        topic="search_strategy_selection",
        agent_proposals=proposals,
        agent_confidences=confidences,
    )

    print(f"\n[Result] Status: {result.status.value}")
    print(f"  Confidence: {result.confidence:.3f}")
    print(f"  Iterations: {result.iterations}")
    print(f"  Agents: {result.participating_agents}")

    if result.consensus_content:
        print(f"\n[Consensus Content]")
        for k, v in result.consensus_content.items():
            print(f"  {k}: {v}")

    if result.conflicting_points:
        print(f"\n[Conflicts Detected] {len(result.conflicting_points)}")
        for cp in result.conflicting_points:
            print(f"  - {cp['field']}: {cp['proposals']}")

    if result.resolution_log:
        print(f"\n[Resolution Log]")
        for entry in result.resolution_log:
            print(f"  - {entry['field']}: {entry['strategy']} -> {entry.get('resolved', 'N/A')}")

    print(f"\n[Stats] {json.dumps(engine.get_stats(), indent=2, default=str)}")
