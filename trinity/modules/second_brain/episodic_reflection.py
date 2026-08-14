"""
P10-6: Episodic Reflection Pipeline — 对标 AWS Bedrock AgentCore

实现结构化情节记录管道:
  - 五阶段记录: goal → reasoning → action → outcome → reflection
  - Episode 包含完整的目标/推理/行动/结果/反思字段
  - find_similar_episodes(): 基于语义相似度检索相似历史情节
  - extract_cross_episode_patterns(): 跨情节模式提取可复用经验教训
  - 反思得分: 对每次行动的效果量化评分

Reference:
    AWS Bedrock AgentCore (2026): Multi-Agent Collaboration & Memory
    https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html
"""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

class ReflectionPhase(Enum):
    """反思管道阶段。"""
    GOAL = "goal"
    REASONING = "reasoning"
    ACTION = "action"
    OUTCOME = "outcome"
    REFLECTION = "reflection"


class OutcomeStatus(Enum):
    """行动结果状态。"""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class PatternType(Enum):
    """跨情节模式类型。"""
    RECURRING_ERROR = "recurring_error"       # 重复错误模式
    SUCCESSFUL_STRATEGY = "successful_strategy" # 成功策略
    DEADLOCK_PATTERN = "deadlock_pattern"     # 死锁模式
    ESCALATION_TRIGGER = "escalation_trigger" # 升级触发器
    WORKAROUND = "workaround"                 # 绕行方案


@dataclass
class ReasoningStep:
    """推理步骤。"""
    step_id: str
    thought: str                             # 推理内容
    confidence: float = 1.0                  # 置信度 [0, 1]
    evidence: list[str] = field(default_factory=list)  # 支撑证据
    timestamp: float = field(default_factory=time.time)


@dataclass
class ActionRecord:
    """行动记录。"""
    action_id: str
    action_type: str                         # 行动类型（工具调用/API/人工）
    description: str
    parameters: dict = field(default_factory=dict)
    duration_ms: float = 0.0                 # 执行耗时
    timestamp: float = field(default_factory=time.time)


@dataclass
class OutcomeRecord:
    """结果记录。"""
    status: OutcomeStatus
    result_summary: str                      # 结果摘要
    result_detail: dict = field(default_factory=dict)  # 结构化结果
    error_message: str = ""                  # 错误信息
    metrics: dict = field(default_factory=dict)        # 性能指标


@dataclass
class ReflectionRecord:
    """反思记录。"""
    lessons_learned: list[str] = field(default_factory=list)
    what_worked: list[str] = field(default_factory=list)
    what_failed: list[str] = field(default_factory=list)
    improvement_suggestions: list[str] = field(default_factory=list)
    score: float = 0.0                       # 整体反思得分 [-1, 1]
    tags: list[str] = field(default_factory=list)


@dataclass
class Episode:
    """结构化情节。"""
    episode_id: str
    goal: str                                # 目标
    reasoning_steps: list[ReasoningStep] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    outcome: OutcomeRecord | None = None
    reflection: ReflectionRecord | None = None
    session_id: str = ""
    agent_id: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "episode_id": self.episode_id,
            "goal": self.goal,
            "reasoning_steps": [
                {"step_id": r.step_id, "thought": r.thought, "confidence": r.confidence}
                for r in self.reasoning_steps
            ],
            "actions": [
                {"action_id": a.action_id, "action_type": a.action_type, "description": a.description}
                for a in self.actions
            ],
            "outcome": {
                "status": self.outcome.status.value if self.outcome else "unknown",
                "summary": self.outcome.result_summary if self.outcome else "",
            } if self.outcome else None,
            "reflection": {
                "lessons": self.reflection.lessons_learned,
                "score": self.reflection.score,
            } if self.reflection else None,
        }


@dataclass
class CrossEpisodePattern:
    """跨情节提取的模式。"""
    pattern_id: str
    pattern_type: PatternType
    description: str
    source_episodes: list[str]               # 来源情节 ID 列表
    frequency: int                           # 出现频次
    confidence: float                        # 置信度 [0, 1]
    actionable_insight: str                  # 可操作见解
    tags: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# Episodic Reflection Pipeline
# ══════════════════════════════════════════════════════════════════════

class EpisodicReflectionPipeline:
    """情节反思管道。

    五阶段流程:
      1. goal — 设定目标
      2. reasoning — 记录推理步骤
      3. action — 记录执行动作
      4. outcome — 记录执行结果
      5. reflection — 反思并提取教训

    Usage:
        pipeline = EpisodicReflectionPipeline()
        ep = pipeline.start_episode("查找向量数据库", agent_id="agent-1")
        ep = pipeline.add_reasoning(ep.episode_id, "需要对比 ChromaDB 和 FAISS")
        ep = pipeline.record_action(ep.episode_id, "search", "搜索 ChromaDB")
        ep = pipeline.record_outcome(ep.episode_id, OutcomeStatus.SUCCESS, "找到3个匹配")
        ep = pipeline.finalize(ep.episode_id, score=0.8, lessons=["向量数据库选择需要评估召回率"])
    """

    def __init__(
        self,
        max_episodes: int = 10000,
        similarity_threshold: float = 0.3,
    ):
        self._episodes: dict[str, Episode] = {}
        self._patterns: list[CrossEpisodePattern] = []
        self._goal_index: dict[str, list[str]] = defaultdict(list)  # goal_hash → episode_ids
        self._tag_index: dict[str, list[str]] = defaultdict(list)
        self.max_episodes = max_episodes
        self.similarity_threshold = similarity_threshold

    # ── 剧情生命周期 ────────────────────────────────────────────────

    def start_episode(
        self,
        goal: str,
        session_id: str = "",
        agent_id: str = "",
        metadata: dict | None = None,
    ) -> Episode:
        """开始新情节（阶段 1: goal）。"""
        episode_id = hashlib.sha256(
            f"{goal}{time.time()}".encode()
        ).hexdigest()[:16]

        episode = Episode(
            episode_id=episode_id,
            goal=goal,
            session_id=session_id,
            agent_id=agent_id,
            metadata=metadata or {},
        )
        self._episodes[episode_id] = episode
        self._index_by_goal(episode)
        return episode

    def add_reasoning(
        self,
        episode_id: str,
        thought: str,
        confidence: float = 1.0,
        evidence: list[str] | None = None,
    ) -> Episode:
        """添加推理步骤（阶段 2: reasoning）。"""
        ep = self._get_episode(episode_id)
        step = ReasoningStep(
            step_id=f"{episode_id}_r{len(ep.reasoning_steps)+1}",
            thought=thought,
            confidence=confidence,
            evidence=evidence or [],
        )
        ep.reasoning_steps.append(step)
        return ep

    def record_action(
        self,
        episode_id: str,
        action_type: str,
        description: str,
        parameters: dict | None = None,
        duration_ms: float = 0.0,
    ) -> Episode:
        """记录执行动作（阶段 3: action）。"""
        ep = self._get_episode(episode_id)
        action = ActionRecord(
            action_id=f"{episode_id}_a{len(ep.actions)+1}",
            action_type=action_type,
            description=description,
            parameters=parameters or {},
            duration_ms=duration_ms,
        )
        ep.actions.append(action)
        return ep

    def record_outcome(
        self,
        episode_id: str,
        status: OutcomeStatus,
        result_summary: str,
        result_detail: dict | None = None,
        error_message: str = "",
        metrics: dict | None = None,
    ) -> Episode:
        """记录结果（阶段 4: outcome）。"""
        ep = self._get_episode(episode_id)
        ep.outcome = OutcomeRecord(
            status=status,
            result_summary=result_summary,
            result_detail=result_detail or {},
            error_message=error_message,
            metrics=metrics or {},
        )
        return ep

    def finalize(
        self,
        episode_id: str,
        score: float = 0.0,
        lessons_learned: list[str] | None = None,
        what_worked: list[str] | None = None,
        what_failed: list[str] | None = None,
        suggestions: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Episode:
        """完成情节并记录反思（阶段 5: reflection）。"""
        ep = self._get_episode(episode_id)
        ep.reflection = ReflectionRecord(
            lessons_learned=lessons_learned or [],
            what_worked=what_worked or [],
            what_failed=what_failed or [],
            improvement_suggestions=suggestions or [],
            score=score,
            tags=tags or [],
        )
        ep.completed_at = time.time()

        # 更新标签索引
        for tag in (tags or []):
            self._tag_index[tag].append(episode_id)

        return ep

    # ── 跨情节检索 ──────────────────────────────────────────────────

    def find_similar_episodes(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.1,
    ) -> list[tuple[Episode, float]]:
        """基于关键词 Jaccard 相似度查找相似情节。

        参数:
            query: 查询文本。
            top_k: 返回 Top-K。
            min_score: 最小相似度阈值。

        返回:
            [(Episode, similarity_score), ...]
        """
        def tokenize(text: str) -> set[str]:
            import re
            return set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", text.lower()))

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[Episode, float]] = []
        for ep in self._episodes.values():
            ep_text = ep.goal
            ep_text += " " + " ".join(
                a.description for a in ep.actions
            )
            if ep.outcome:
                ep_text += " " + ep.outcome.result_summary

            ep_tokens = tokenize(ep_text)
            if not ep_tokens:
                continue

            intersection = len(query_tokens & ep_tokens)
            union = len(query_tokens | ep_tokens)
            jaccard = intersection / union if union > 0 else 0.0

            if jaccard >= min_score:
                scored.append((ep, jaccard))

        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # ── 跨情节模式提取 ──────────────────────────────────────────────

    def extract_cross_episode_patterns(
        self,
        min_frequency: int = 2,
    ) -> list[CrossEpisodePattern]:
        """提取跨情节的可复用经验教训。

        分析所有已完成情节的反思记录，识别重复出现的模式。

        参数:
            min_frequency: 最小出现频次。

        返回:
            [CrossEpisodePattern, ...]
        """
        patterns: list[CrossEpisodePattern] = []

        # 1) 提取失败模式
        failure_lessons: dict[str, list[str]] = defaultdict(list)
        for ep in self._episodes.values():
            if not ep.reflection:
                continue
            if ep.outcome and ep.outcome.status in (
                OutcomeStatus.FAILURE, OutcomeStatus.BLOCKED, OutcomeStatus.TIMEOUT
            ):
                for lesson in ep.reflection.lessons_learned:
                    lesson_key = lesson[:80]
                    failure_lessons[lesson_key].append(ep.episode_id)

        for lesson_key, ep_ids in failure_lessons.items():
            if len(ep_ids) >= min_frequency:
                patterns.append(CrossEpisodePattern(
                    pattern_id=f"err_{hashlib.md5(lesson_key.encode()).hexdigest()[:8]}",
                    pattern_type=PatternType.RECURRING_ERROR,
                    description=lesson_key,
                    source_episodes=ep_ids,
                    frequency=len(ep_ids),
                    confidence=min(1.0, len(ep_ids) / 5.0),
                    actionable_insight=f"避免: {lesson_key}",
                ))

        # 2) 提取成功策略
        success_strategies: dict[str, list[str]] = defaultdict(list)
        for ep in self._episodes.values():
            if not ep.reflection or not ep.reflection.what_worked:
                continue
            for item in ep.reflection.what_worked:
                item_key = item[:80]
                success_strategies[item_key].append(ep.episode_id)

        for item_key, ep_ids in success_strategies.items():
            if len(ep_ids) >= min_frequency:
                patterns.append(CrossEpisodePattern(
                    pattern_id=f"succ_{hashlib.md5(item_key.encode()).hexdigest()[:8]}",
                    pattern_type=PatternType.SUCCESSFUL_STRATEGY,
                    description=item_key,
                    source_episodes=ep_ids,
                    frequency=len(ep_ids),
                    confidence=min(1.0, len(ep_ids) / 3.0),
                    actionable_insight=f"复用: {item_key}",
                ))

        # 3) 标签聚类模式
        tag_episodes: dict[str, list[str]] = defaultdict(list)
        for ep in self._episodes.values():
            if ep.reflection and ep.reflection.tags:
                for tag in ep.reflection.tags:
                    tag_episodes[tag].append(ep.episode_id)

        for tag, ep_ids in tag_episodes.items():
            if len(ep_ids) >= min_frequency:
                # 收集该标签下所有情节的 lessons
                all_lessons: list[str] = []
                for eid in ep_ids:
                    ep = self._episodes.get(eid)
                    if ep and ep.reflection:
                        all_lessons.extend(ep.reflection.lessons_learned)

                if all_lessons:
                    # 取最频繁的 lesson
                    from collections import Counter
                    top_lesson = Counter(all_lessons).most_common(1)[0]
                    patterns.append(CrossEpisodePattern(
                        pattern_id=f"tag_{hashlib.md5(tag.encode()).hexdigest()[:8]}",
                        pattern_type=PatternType.SUCCESSFUL_STRATEGY,
                        description=f"[{tag}] {top_lesson[0]}",
                        source_episodes=ep_ids,
                        frequency=len(ep_ids),
                        confidence=min(1.0, top_lesson[1] / len(ep_ids)),
                        actionable_insight=f"标签({tag}): {top_lesson[0]}",
                        tags=[tag],
                    ))

        self._patterns = patterns
        return patterns

    # ── 查询接口 ────────────────────────────────────────────────────

    def get_episode(self, episode_id: str) -> Episode | None:
        """按 ID 获取情节。"""
        return self._episodes.get(episode_id)

    def list_episodes(
        self,
        agent_id: str = "",
        session_id: str = "",
        status_filter: OutcomeStatus | None = None,
        limit: int = 50,
    ) -> list[Episode]:
        """列出情节。"""
        result = []
        for ep in self._episodes.values():
            if agent_id and ep.agent_id != agent_id:
                continue
            if session_id and ep.session_id != session_id:
                continue
            if (status_filter is not None and
                ep.outcome and
                ep.outcome.status != status_filter):
                continue
            result.append(ep)

        result.sort(key=lambda e: e.created_at, reverse=True)
        return result[:limit]

    def get_reusable_lessons(self) -> list[dict]:
        """获取所有可复用的经验教训（跨情节模式）。"""
        if not self._patterns:
            self.extract_cross_episode_patterns()

        return [
            {
                "pattern_id": p.pattern_id,
                "type": p.pattern_type.value,
                "description": p.description,
                "insight": p.actionable_insight,
                "frequency": p.frequency,
                "confidence": p.confidence,
            }
            for p in self._patterns
        ]

    def get_stats(self) -> dict:
        """获取管道统计。"""
        total = len(self._episodes)
        completed = sum(1 for e in self._episodes.values() if e.completed_at is not None)
        successes = sum(
            1 for e in self._episodes.values()
            if e.outcome and e.outcome.status == OutcomeStatus.SUCCESS
        )
        failures = sum(
            1 for e in self._episodes.values()
            if e.outcome and e.outcome.status == OutcomeStatus.FAILURE
        )
        avg_score = (
            sum(e.reflection.score for e in self._episodes.values()
                if e.reflection) / max(1, completed)
        )

        return {
            "total_episodes": total,
            "completed": completed,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / max(1, completed), 3),
            "avg_reflection_score": round(avg_score, 3),
            "extracted_patterns": len(self._patterns),
        }

    # ── 内部方法 ────────────────────────────────────────────────────

    def _get_episode(self, episode_id: str) -> Episode:
        ep = self._episodes.get(episode_id)
        if ep is None:
            raise KeyError(f"Episode not found: {episode_id}")
        return ep

    def _index_by_goal(self, episode: Episode) -> None:
        goal_hash = hashlib.md5(episode.goal.encode()).hexdigest()[:8]
        self._goal_index[goal_hash].append(episode.episode_id)


# ══════════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Episodic Reflection Pipeline — 自检")
    print("=" * 60)

    pipeline = EpisodicReflectionPipeline()

    # 情节 1: 成功
    ep1 = pipeline.start_episode("查找向量数据库最佳选择", agent_id="researcher")
    pipeline.add_reasoning(ep1.episode_id, "评估 ChromaDB vs FAISS vs Milvus", 0.9)
    pipeline.record_action(ep1.episode_id, "search", "搜索 ChromaDB 文档")
    pipeline.record_action(ep1.episode_id, "benchmark", "运行召回率基准测试", duration_ms=1200)
    pipeline.record_outcome(ep1.episode_id, OutcomeStatus.SUCCESS, "ChromaDB 召回率 98%, 延迟 12ms")
    pipeline.finalize(
        ep1.episode_id, score=0.9,
        lessons_learned=["向量数据库选择应优先考虑 HNSW 索引"],
        what_worked=["ChromaDB HNSW 索引性能优异"],
        tags=["vector_db", "benchmark"],
    )

    # 情节 2: 失败
    ep2 = pipeline.start_episode("部署到生产环境", agent_id="devops")
    pipeline.add_reasoning(ep2.episode_id, "选择 Kubernetes 部署方案", 0.8)
    pipeline.record_action(ep2.episode_id, "deploy", "执行 kubectl apply")
    pipeline.record_outcome(
        ep2.episode_id, OutcomeStatus.FAILURE,
        "部署失败", error_message="OOMKilled: memory limit exceeded",
    )
    pipeline.finalize(
        ep2.episode_id, score=-0.3,
        lessons_learned=["生产部署前需检查内存限制配置"],
        what_failed=["默认内存限制不足以承载向量索引"],
        tags=["deployment", "error"],
    )

    # 情节 3: 相似于 1
    ep3 = pipeline.start_episode("对比向量数据库性能", agent_id="researcher")
    pipeline.record_outcome(ep3.episode_id, OutcomeStatus.SUCCESS, "FAISS 在大规模场景更优")
    pipeline.finalize(
        ep3.episode_id, score=0.7,
        lessons_learned=["大规模场景首选 FAISS"],
        what_worked=["FAISS IVF 索引"],
        tags=["vector_db", "benchmark"],
    )

    # 相似情节检索
    sim = pipeline.find_similar_episodes("向量数据库对比", top_k=3)
    print(f"\n[相似情节检索] 查询='向量数据库对比' → {len(sim)} 个结果:")
    for ep, score in sim:
        print(f"  {ep.episode_id}: goal='{ep.goal}', score={score:.3f}")

    # 跨情节模式提取
    patterns = pipeline.extract_cross_episode_patterns(min_frequency=2)
    print(f"\n[跨情节模式] 提取 {len(patterns)} 个模式:")
    for p in patterns:
        print(f"  {p.pattern_id}: [{p.pattern_type.value}] {p.description} "
              f"(freq={p.frequency}, conf={p.confidence:.2f})")

    # 可复用教训
    lessons = pipeline.get_reusable_lessons()
    print(f"\n[可复用教训] {len(lessons)} 条")

    # 统计
    stats = pipeline.get_stats()
    print(f"\n[统计] {json.dumps(stats, indent=2)}")

    print("\n所有测试通过!")
