"""
# status: orphan (2026-08-15 audit, not in runtime path)
P25-3: Agent Skill Graph — 对标 AgentSkillGraph 2026.06
三元语: Register → Match → Offload → Distill
设计要点:
  - SkillNode 为技能节点 dataclass，含能力标签/置信度/依赖链
  - SkillGraph 维护 nodes dict + edges list，支持动态增删
  - MetaAgentOffloader 匹配最适 sub-agent skill 并路由
  - CapabilityMatcher 按 embedding cosine 相似度匹配技能
  - distill_knowledge 实现跨 agent 技能蒸馏
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SkillNode:
    """Agent 技能节点 — 含能力标签、置信度与依赖链。"""

    skill_id: str
    name: str
    capability_tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    dependencies: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class SkillGraph:
    """技能图 — nodes dict + edges list，支持动态增删与查询。"""

    def __init__(self) -> None:
        self.nodes: dict[str, SkillNode] = {}
        self.edges: list[tuple[str, str]] = []
        self._lock = threading.RLock()
        self._op_count = 0

    def add_skill(self, node: SkillNode) -> None:
        """添加技能节点（已存在则更新 confidence 并合并 tags）。"""
        with self._lock:
            if node.skill_id in self.nodes:
                existing = self.nodes[node.skill_id]
                existing.confidence = max(existing.confidence, node.confidence)
                existing.capability_tags = list(
                    set(existing.capability_tags) | set(node.capability_tags)
                )
                existing.dependencies = list(
                    set(existing.dependencies) | set(node.dependencies)
                )
            else:
                self.nodes[node.skill_id] = node
            for dep in node.dependencies:
                edge = (node.skill_id, dep)
                if edge not in self.edges:
                    self.edges.append(edge)
            self._op_count += 1

    def remove_skill(self, skill_id: str) -> bool:
        """移除技能节点及关联边。"""
        with self._lock:
            if skill_id not in self.nodes:
                return False
            del self.nodes[skill_id]
            self.edges = [
                (s, t) for s, t in self.edges if s != skill_id and t != skill_id
            ]
            self._op_count += 1
            return True

    def get_skill(self, skill_id: str) -> Optional[SkillNode]:
        return self.nodes.get(skill_id)

    def statistics(self) -> dict:
        with self._lock:
            return {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "op_count": self._op_count,
            }


class MetaAgentOffloader:
    """元代理卸载器 — 按 task_spec 匹配最适 sub-agent skill 并路由。

    offload() 返回匹配到的 skill_id，若无可匹配则返回 None。
    """

    def __init__(self, graph: Optional[SkillGraph] = None) -> None:
        self._graph = graph or SkillGraph()
        self._offload_count = 0

    def offload(self, task_spec: dict) -> Optional[str]:
        """匹配最适 sub-agent skill。

        task_spec 预期含 {"capability": str, "min_confidence": float}。
        """
        capability = task_spec.get("capability", "")
        min_conf = task_spec.get("min_confidence", 0.0)

        best: Optional[SkillNode] = None
        best_score = -1.0
        for node in self._graph.nodes.values():
            if capability in node.capability_tags and node.confidence >= min_conf:
                score = node.confidence * len(node.capability_tags)
                if score > best_score:
                    best_score = score
                    best = node
        if best is not None:
            self._offload_count += 1
            return best.skill_id
        return None

    def statistics(self) -> dict:
        return {"offload_count": self._offload_count}


class CapabilityMatcher:
    """能力匹配器 — 按 task_embedding 余弦相似度匹配技能节点。"""

    def __init__(self, graph: Optional[SkillGraph] = None) -> None:
        self._graph = graph or SkillGraph()
        self._embeddings: dict[str, list[float]] = {}

    def register_embedding(self, skill_id: str, embedding: list[float]) -> None:
        self._embeddings[skill_id] = embedding

    def _cosine(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def match(
        self, task_embedding: list[float], top_k: int = 5
    ) -> list[SkillNode]:
        """按 embedding 余弦相似度返回 top_k 技能节点。"""
        scored: list[tuple[SkillNode, float]] = []
        for skill_id, emb in self._embeddings.items():
            node = self._graph.nodes.get(skill_id)
            if node is None:
                continue
            sim = self._cosine(task_embedding, emb)
            scored.append((node, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [node for node, _ in scored[:top_k]]

    def statistics(self) -> dict:
        return {"embedding_count": len(self._embeddings)}


def distill_knowledge(
    source_agent_skills: list[dict],
    target_agent: str,
) -> SkillGraph:
    """跨 agent 技能蒸馏 — 将源 agent 技能集蒸馏为目标 agent 的 SkillGraph。

    source_agent_skills 每项含 {"skill_id", "name", "tags", "confidence"}。
    """
    graph = SkillGraph()
    for raw in source_agent_skills:
        node = SkillNode(
            skill_id=f"{target_agent}:{raw['skill_id']}",
            name=raw.get("name", raw["skill_id"]),
            capability_tags=raw.get("tags", []),
            confidence=raw.get("confidence", 0.5),
            metadata={"source_agent": raw.get("source", "unknown")},
        )
        graph.add_skill(node)
    logger.info(
        "Distilled %d skills from source agents → target agent '%s'",
        len(source_agent_skills),
        target_agent,
    )
    return graph
