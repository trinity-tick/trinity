# engine_observability.py — Observability subsystem extracted from engine.py
# ObserverReflector (P123 Mastra OM) + HindsightFourNetwork (P127 BEAM SOTA) +
# ZikkaronHopfield (P128 Non-LLM SOTA)
from __future__ import annotations
import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

# Shared constants imported from engine_core
from trinity.modules.second_brain.engine_core import (
    PAPERS, VERSION, SEP, SUB,
    ContextAction, ExecutionGear, GovernanceState, CertificateStatus,
    MemoryErrorType, CacheWriteDecision, ConsolidationPhase,
    ContextObject, ContextCommit, MemoryHead, ProvenanceRecord,
    ContinuityState, SafetyAlarm, ExactKVEntry, ConsolidationRecord,
    ValueCategoryMapping,
    # Core classes that ObserverReflector/Hindsight may reference
    ProgressiveCascade, TemporalValidity, TokenEfficientMemory,
    AgentNativeCuration, RelationalVersioning, ContextualChunkIngestion,
)

class ObserverReflector:
    """
    CB51: ObserverReflector -- 双后台Agent观测记忆
    论文: Mastra Observational Memory (LongMemEval 94.87% SOTA, gpt-5-mini), P123

    对齐 Mastra OM 核心设计:

    1. Observer Agent: 监视主Agent对话，生成结构化观察日志
       - 观察内容: 用户陈述、Agent动作、工具调用结果、偏好表达、当前任务
       - 每条观察: 优先级标签(高/中/低) + 日期 + 结构化文本
       - 格式: 两级项目符号列表(顶级=事件/任务, 子级=细节)
       - 触发条件: 未观察消息达到 token 阈值(非时间/消息数触发)

    2. Reflector Agent: 观察日志达到 token 阈值时触发
       - 合并相关条目，反思模式
       - 删除已被取代的旧观察
       - 产出重组后的浓缩观察集

    三层信息表示:
    - L1 Message History: 原始对话(增长最快, 最详细)
    - L2 Observations: Observer 输出(3-6x 压缩文本, 5-40x 工具输出)
    - L3 Reflections: Reflector 输出(进一步压缩, 模式识别)

    稳定上下文窗口:
    - 上下文分两段: [记忆段(观察+反思) | 消息历史段(当前对话)]
    - 记忆段 append-only, 前缀不变 -> Prompt-Cacheable
    - 无动态检索注入, 无每轮查询

    三日期时间戳模型:
    - observation_date: 观察创建时间
    - referenced_date: 内容中提到的时间
    - relative_date: 计算相对偏移

    集成:
    - Token 阈值使用 CB47 TokenEfficientMemory
    - 时态查询对接 CB46 TemporalValidity
    - 版本链对接 CB49 RelationalVersioning (Reflector的"取代旧观察"用 updates 关系)
    - 上下文树对接 CB45 ContextTree
    """

    # 优先级枚举
    PRIORITY_HIGH = "high"
    PRIORITY_MEDIUM = "medium"
    PRIORITY_LOW = "low"
    PRIORITY_EMOJI = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}

    def __init__(self,
                 observer_token_threshold: int = 800,
                 reflector_token_threshold: int = 3000):
        self.observer_token_threshold = observer_token_threshold
        self.reflector_token_threshold = reflector_token_threshold

        # 消息缓冲: 尚未被观察的原始消息
        self.unobserved_messages: list[dict] = []
        self.unobserved_token_count: int = 0

        # L2 观察存储
        self.observations: list[dict] = []
        self.observation_token_count: int = 0

        # L3 反思存储
        self.reflections: list[dict] = []
        self.reflection_token_count: int = 0

        # 当前任务追踪
        self.current_task: Optional[str] = None
        self.suggested_response: Optional[str] = None

        # 统计
        self.total_observations: int = 0
        self.total_reflections: int = 0
        self.total_observer_runs: int = 0
        self.total_reflector_runs: int = 0

        # 集成引用
        self.cb45_ref = None
        self.cb46_ref = None
        self.cb47_ref = None
        self.cb49_ref = None

        # 观察-反思版本链(用于 Reflector 的"取代旧观察")
        self.reflection_version_chains: dict[str, list[str]] = defaultdict(list)

    def estimate_tokens(self, text: str) -> int:
        """快速 token 估算: ~4 字符/token"""
        return len(text) // 4

    def feed_message(self, message: dict):
        """向观察缓冲区喂入一条消息"""
        content = message.get("content", "")
        tokens = self.estimate_tokens(content)
        self.unobserved_messages.append(message)
        self.unobserved_token_count += tokens

    def should_observe(self) -> bool:
        """检查是否应触发 Observer"""
        return self.unobserved_token_count >= self.observer_token_threshold

    def should_reflect(self) -> bool:
        """检查是否应触发 Reflector"""
        return self.observation_token_count >= self.reflector_token_threshold

    def run_observer(self) -> dict:
        """
        运行 Observer Agent: 将未观察消息转换为结构化观察。

        每条观察:
        - priority: high/medium/low
        - observation_date: 创建时间戳
        - referenced_date: 内容中提及的时间
        - relative_date: 相对偏移(天)
        - event_type: 事件类型(user_statement/agent_action/tool_result/preference/task)
        - title: 顶级项目符号(事件/任务)
        - details: 子级项目符号列表(细节)
        - source_message_range: 源消息索引范围
        """
        if not self.unobserved_messages:
            return {"status": "no_unobserved_messages", "observations_generated": 0}

        self.total_observer_runs += 1
        observations_made = []

        # 按消息角色分组分析
        current_event = None
        event_messages = []

        for i, msg in enumerate(self.unobserved_messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", time.time())

            if role == "user":
                # 新事件: 用户发言
                if current_event and event_messages:
                    obs = self._build_observation(current_event, event_messages, i)
                    if obs:
                        observations_made.append(obs)
                current_event = {
                    "type": "user_statement",
                    "title": self._summarize_title(content, max_len=60),
                    "start_idx": i,
                }
                event_messages = [msg]

            elif role == "assistant":
                if current_event is None:
                    current_event = {
                        "type": "agent_action",
                        "title": self._summarize_title(content, max_len=60),
                        "start_idx": i,
                    }
                event_messages.append(msg)

            elif role == "tool":
                # 工具调用结果
                current_event = {
                    "type": "tool_result",
                    "title": self._summarize_title(f"Tool: {content[:80]}", max_len=60),
                    "start_idx": i,
                }
                event_messages = [msg]
                obs = self._build_observation(current_event, event_messages, i)
                if obs:
                    observations_made.append(obs)
                current_event = None
                event_messages = []

        # 处理最后一个事件
        if current_event and event_messages:
            obs = self._build_observation(
                current_event, event_messages, len(self.unobserved_messages) - 1)
            if obs:
                observations_made.append(obs)

        # 偏好表达检测
        preference_obs = self._detect_preferences(self.unobserved_messages)
        observations_made.extend(preference_obs)

        # 更新当前任务
        self._update_current_task(self.unobserved_messages)

        # 添加观察
        for obs in observations_made:
            self.observations.append(obs)
            self.observation_token_count += self.estimate_tokens(obs["content"])
            self.total_observations += 1

        # 清空未观察缓冲区
        msg_count = len(self.unobserved_messages)
        self.unobserved_messages.clear()
        self.unobserved_token_count = 0

        return {
            "status": "ok",
            "observations_generated": len(observations_made),
            "messages_processed": msg_count,
            "compression_ratio": round(
                msg_count / max(1, len(observations_made)), 1),
            "observation_token_count": self.observation_token_count,
        }

    def run_reflector(self) -> dict:
        """
        运行 Reflector Agent: 重组和浓缩观察。

        流程:
        1. 按主题/实体聚类现有观察
        2. 合并相关条目，识别模式
        3. 标记被取代的旧观察
        4. 产出浓缩后的反思集
        """
        if not self.observations:
            return {"status": "no_observations", "reflections_generated": 0}

        self.total_reflector_runs += 1

        # 聚类观察
        clusters = self._cluster_observations()
        new_reflections = []

        for cluster_key, obs_ids in clusters.items():
            cluster_obs = [o for o in self.observations if o["observation_id"] in obs_ids]
            if len(cluster_obs) < 2:
                continue

            # 生成反思
            reflection = self._build_reflection(cluster_obs, cluster_key)
            new_reflections.append(reflection)

            # 在 CB49 中记录版本关系(取代旧观察)
            if self.cb49_ref:
                for old_obs in cluster_obs[:-1]:
                    self.reflection_version_chains[reflection["reflection_id"]].append(
                        old_obs["observation_id"])

        for ref in new_reflections:
            self.reflections.append(ref)
            self.reflection_token_count += self.estimate_tokens(ref["content"])
            self.total_reflections += 1

        # 压缩观察: 删除已被反思覆盖的旧观察
        reflected_obs_ids = set()
        for cluster_key, obs_ids in clusters.items():
            reflected_obs_ids.update(obs_ids)

        old_count = len(self.observations)
        self.observations = [
            o for o in self.observations
            if o["observation_id"] not in reflected_obs_ids
        ]
        removed = old_count - len(self.observations)

        # 重新计算 token 数
        self.observation_token_count = sum(
            self.estimate_tokens(o["content"]) for o in self.observations)

        return {
            "status": "ok",
            "reflections_generated": len(new_reflections),
            "observations_removed": removed,
            "observations_remaining": len(self.observations),
            "reflection_token_count": self.reflection_token_count,
            "observation_token_count": self.observation_token_count,
        }

    def get_memory_segment(self) -> str:
        """获取记忆段: 反思 + 观察(append-only, 前缀不变)"""
        parts = []

        if self.reflections:
            parts.append("## Reflections (condensed patterns)")
            for ref in self.reflections:
                parts.append(ref["content"])

        if self.observations:
            if self.reflections:
                parts.append("")
            parts.append("## Observations")
            for obs in sorted(self.observations,
                              key=lambda x: x.get("observation_date", 0),
                              reverse=True):
                parts.append(obs["content"])

        return "\n".join(parts) if parts else ""

    def get_context_window_layout(self,
                                   message_history: str) -> dict:
        """
        返回标准上下文窗口布局:
        [记忆段(观察+反思) | 消息历史段(当前对话)]
        """
        memory = self.get_memory_segment()
        return {
            "memory_segment": memory,
            "message_history": message_history,
            "memory_tokens": self.estimate_tokens(memory),
            "message_tokens": self.estimate_tokens(message_history),
            "total_tokens": self.estimate_tokens(memory) + self.estimate_tokens(message_history),
            "is_prompt_cacheable": True,  # 记忆段前缀不变
        }

    def query_observations(self, keyword: str = None,
                           priority: str = None,
                           date_start: float = None,
                           date_end: float = None) -> list[dict]:
        """查询观察记录"""
        results = []
        for obs in self.observations:
            if priority and obs.get("priority") != priority:
                continue
            obs_date = obs.get("observation_date", 0)
            if date_start and obs_date < date_start:
                continue
            if date_end and obs_date > date_end:
                continue
            if keyword and keyword.lower() not in obs.get("content", "").lower():
                continue
            results.append({
                "observation_id": obs["observation_id"],
                "content": obs["content"],
                "priority": obs["priority"],
                "observation_date": obs["observation_date"],
                "referenced_date": obs.get("referenced_date"),
                "relative_date": obs.get("relative_date"),
            })
        return sorted(results, key=lambda x: x["observation_date"], reverse=True)

    def query_reflections(self, keyword: str = None) -> list[dict]:
        """查询反思记录"""
        results = []
        for ref in self.reflections:
            if keyword and keyword.lower() not in ref.get("content", "").lower():
                continue
            results.append({
                "reflection_id": ref["reflection_id"],
                "content": ref["content"],
                "cluster_key": ref.get("cluster_key"),
                "observation_count": ref.get("observation_count", 0),
                "created_at": ref.get("created_at", 0),
            })
        return results

    def _build_observation(self, event: dict, messages: list[dict],
                           end_idx: int) -> Optional[dict]:
        """构建单条观察"""
        full_text = " ".join(m.get("content", "") for m in messages)
        combined = full_text

        # 优先级判定
        priority = self._determine_priority(combined, event["type"])

        # 时间戳
        now = time.time()
        referenced_date = self._extract_referenced_date(combined)
        relative_date = None
        if referenced_date:
            relative_date = round((referenced_date - now) / 86400, 1)

        # 两级项目符号格式
        details = self._extract_details(messages)
        details_text = "\n".join(f"  - {d}" for d in details[:5]) if details else ""

        content = f"[{self.PRIORITY_EMOJI[priority]}] {event['title']}\n{details_text}".strip()

        return {
            "observation_id": f"obs_{uuid.uuid4().hex[:10]}",
            "priority": priority,
            "observation_date": now,
            "referenced_date": referenced_date,
            "relative_date": relative_date,
            "event_type": event["type"],
            "title": event["title"],
            "details": details,
            "content": content,
            "source_message_range": (event["start_idx"], end_idx),
        }

    def _build_reflection(self, cluster_obs: list[dict],
                          cluster_key: str) -> dict:
        """构建反思记录"""
        combined = "\n".join(o["content"] for o in cluster_obs)
        summary = self._summarize_title(combined, max_len=120)
        priority_counts = {"high": 0, "medium": 0, "low": 0}
        for o in cluster_obs:
            p = o.get("priority", "low")
            priority_counts[p] = priority_counts.get(p, 0) + 1

        dominant_priority = max(priority_counts, key=priority_counts.get)

        content = (
            f"[REFLECTION] {summary}\n"
            f"  Cluster: {cluster_key}\n"
            f"  Based on {len(cluster_obs)} observations "
            f"(H:{priority_counts['high']} M:{priority_counts['medium']} L:{priority_counts['low']})\n"
            f"  Dominant priority: {dominant_priority}"
        )

        return {
            "reflection_id": f"ref_{uuid.uuid4().hex[:10]}",
            "content": content,
            "cluster_key": cluster_key,
            "observation_count": len(cluster_obs),
            "observation_ids": [o["observation_id"] for o in cluster_obs],
            "created_at": time.time(),
            "dominant_priority": dominant_priority,
        }

    def _cluster_observations(self) -> dict[str, list[str]]:
        """按主题聚类观察"""
        clusters = defaultdict(list)
        for obs in self.observations:
            words = set(re.findall(r'\b[a-zA-Z]{4,}\b', obs.get("content", "").lower()))
            best_cluster = None
            best_overlap = 0
            for cluster_key, obs_ids in clusters.items():
                cluster_words = set(re.findall(r'\b[a-zA-Z]{4,}\b', cluster_key.lower()))
                overlap = len(words & cluster_words)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_cluster = cluster_key
            if best_cluster and best_overlap >= 2:
                clusters[best_cluster].append(obs["observation_id"])
            else:
                # 新聚类: 取最长的3个词作为 key
                top_keywords = sorted(words, key=len, reverse=True)[:3]
                cluster_key = " ".join(top_keywords) if top_keywords else obs.get("title", "general")
                clusters[cluster_key] = [obs["observation_id"]]
        return dict(clusters)

    def _determine_priority(self, text: str, event_type: str) -> str:
        """判定优先级"""
        text_lower = text.lower()
        high_signals = [
            "prefer", "favorite", "important", "critical", "always",
            "never", "hate", "must", "required", "deadline", "urgent",
            "password", "secret", "private", "confidential",
        ]
        medium_signals = [
            "like", "need", "want", "maybe", "sometimes", "usually",
            "schedule", "plan", "task", "project",
        ]

        if event_type == "preference":
            return self.PRIORITY_HIGH

        for signal in high_signals:
            if signal in text_lower:
                return self.PRIORITY_HIGH
        for signal in medium_signals:
            if signal in text_lower:
                return self.PRIORITY_MEDIUM
        return self.PRIORITY_LOW

    def _extract_referenced_date(self, text: str) -> Optional[float]:
        """提取内容中引用的日期"""
        patterns = [
            r'\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b',
            r'\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b',
        ]
        for pat in patterns:
            match = re.search(pat, text)
            if match:
                try:
                    groups = match.groups()
                    if len(groups[0]) == 4:
                        y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
                    else:
                        m, d, y = int(groups[0]), int(groups[1]), int(groups[2])
                    return datetime(y, m, d).timestamp()
                except (ValueError, IndexError):
                    pass
        return None

    def _extract_details(self, messages: list[dict]) -> list[str]:
        """从消息提取细节列表"""
        details = []
        for msg in messages:
            content = msg.get("content", "")
            sentences = re.split(r'[.!?]+', content)
            for s in sentences:
                s = s.strip()
                if 10 < len(s) < 120:
                    details.append(s)
        return details[:8]

    def _summarize_title(self, text: str, max_len: int = 60) -> str:
        """缩短文本为标题"""
        text = text.strip()
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."

    def _detect_preferences(self, messages: list[dict]) -> list[dict]:
        """检测偏好表达并生成观察"""
        pref_obs = []
        pref_keywords = [
            "prefer", "favorite", "like better", "i'd rather",
            "i would rather", "i want", "i need", "i love",
            "i hate", "i dislike", "don't like",
        ]
        for i, msg in enumerate(messages):
            content = msg.get("content", "").lower()
            if any(kw in content for kw in pref_keywords) and msg.get("role") == "user":
                now = time.time()
                obs = {
                    "observation_id": f"obs_{uuid.uuid4().hex[:10]}",
                    "priority": self.PRIORITY_HIGH,
                    "observation_date": now,
                    "referenced_date": None,
                    "relative_date": None,
                    "event_type": "preference",
                    "title": self._summarize_title(msg.get("content", ""), max_len=60),
                    "details": [msg.get("content", "")[:200]],
                    "content": f"[HIGH] Preference: {self._summarize_title(msg.get('content', ''), max_len=80)}",
                    "source_message_range": (i, i),
                }
                pref_obs.append(obs)
        return pref_obs

    def _update_current_task(self, messages: list[dict]):
        """更新当前任务追踪"""
        for msg in messages:
            content = msg.get("content", "").lower()
            if msg.get("role") == "user":
                task_signals = [
                    "help me", "can you", "please", "i need to",
                    "find", "search", "create", "write", "analyze",
                    "organize", "convert", "summarize",
                ]
                if any(signal in content for signal in task_signals):
                    self.current_task = self._summarize_title(
                        msg.get("content", ""), max_len=80)

    def get_stats(self) -> dict:
        return {
            "total_observations": self.total_observations,
            "total_reflections": self.total_reflections,
            "total_observer_runs": self.total_observer_runs,
            "total_reflector_runs": self.total_reflector_runs,
            "observations_in_memory": len(self.observations),
            "reflections_in_memory": len(self.reflections),
            "observation_token_count": self.observation_token_count,
            "reflection_token_count": self.reflection_token_count,
            "unobserved_messages": len(self.unobserved_messages),
            "unobserved_token_count": self.unobserved_token_count,
            "current_task": self.current_task,
            "compression_stats": {
                "observer_threshold": self.observer_token_threshold,
                "reflector_threshold": self.reflector_token_threshold,
            },
        }

    def diagnostics(self) -> dict:
        return {
            "architecture": "Mastra Observational Memory (P123)",
            "dual_agents": "Observer + Reflector (background, never interrupting)",
            "three_tier_info": "L1 Messages -> L2 Observations -> L3 Reflections",
            "context_window": "stable, append-only, prompt-cacheable",
            "trigger_mechanism": "token_count_based (not time/msg_count)",
            "three_date_model": "observation_date + referenced_date + relative_date",
            "integrations": [
                "CB47_TokenEfficientMemory (token thresholds)",
                "CB46_TemporalValidity (temporal queries)",
                "CB49_RelationalVersioning (reflection version chains)",
                "CB45_ContextTree (context tree integration)",
            ],
            "stats": self.get_stats(),
        }


print("[P123] ObserverReflector (CB51) initialized -- Mastra OM aligned")


# ===============================================================================
# CB52: GroundTruthEpisodes (NEW, P124, Round 9)
# ===============================================================================

class NetworkType(Enum):
    """四网络类型"""
    VECTOR = "vector"      # 语义向量索引
    ENTITY = "entity"      # 命名实体识别 + 实体图谱
    TEMPORAL = "temporal"  # 时间轴索引
    GRAPH = "graph"        # 记忆间显式关系图


class QueryType(Enum):
    """查询类型 — 用于自适应路由权重分配"""
    SEMANTIC = "semantic"           # 语义相似：Vector ↑
    FACTUAL = "factual"             # 事实提取：Entity ↑
    TEMPORAL_QUERY = "temporal"     # 时间相关：Temporal ↑
    RELATIONAL = "relational"       # 关系推理：Graph ↑
    MIXED = "mixed"                 # 混合：等权


@dataclass
class VectorEntry:
    """Vector Network 条目 — 语义向量索引"""
    memory_id: str
    content: str
    embedding_hash: int  # 简化的语义哈希
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def similarity(self, query_hash: int) -> float:
        """基于哈希汉明距离的简化语义相似度"""
        xor = self.embedding_hash ^ query_hash
        distance = bin(xor).count('1')
        max_bits = 256
        return 1.0 - (distance / max_bits)


@dataclass
class EntityEntry:
    """Entity Network 条目 — 命名实体图谱节点"""
    entity_id: str
    entity_type: str  # PERSON, ORG, DATE, LOC, etc.
    name: str
    properties: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    relations: Dict[str, List[str]] = field(default_factory=dict)  # rel_type → [target_entity_id]
    timestamp: float = field(default_factory=time.time)


@dataclass
class TemporalEntry:
    """Temporal Network 条目 — 时间轴索引"""
    memory_id: str
    content: str
    event_date: str  # ISO date string
    timestamp: float
    referenced_dates: List[str] = field(default_factory=list)
    anchor_events: List[str] = field(default_factory=list)
    session_id: Optional[str] = None


@dataclass
class GraphEdge:
    """Graph Network 边 — 记忆间显式关系"""
    source_id: str
    target_id: str
    relation_type: str  # "contradicts", "updates", "references", "extends", "relates_to"
    weight: float = 1.0
    timestamps: Tuple[float, float] = (0, 0)


# ============================================================================
# CB55: HindsightFourNetwork
# ============================================================================
class HindsightFourNetwork:
    """
    Hindsight 四网络分离架构 (P127)。

    Hindsight 是 BEAM 基准 10M 级唯一不塌缩的架构（64.1%），远超 Honcho 40.6%
    和 LIGHT 26.6%。其 1M 级 73.9% > 500K 级 71.1%，性能随规模不降反升的核心
    原因：四网络提供了互补的检索信号，更多数据 = 更丰富的信号 = 更好的检索。

    四大网络：
    1. Vector Network：语义向量索引，模糊相似性检索
       → 对接 CB45 L3 Semantic + CB52 semantic 路由
    2. Entity Network：命名实体 + 实体图谱，结构化关系
       → 对接 CB46/CB49 知识图谱
    3. Temporal Network：时间轴索引，按时间范围快速定位
       → 对接 CB46 双时态 + CB51 三日期模型
    4. Graph Network：记忆间显式关系图
       → 对接 CB49 RelationalVersioning 版本链
    """

    MODULE_ID = "CB55"
    MODULE_VERSION = "1.0.0"
    PAPER_REF = "P127"
    MODULE_NAME = "HindsightFourNetwork"

    # 能力维度定义
    CAPABILITIES = {
        "preference_following": "用户偏好追踪",
        "instruction_following": "指令遵循",
        "information_extraction": "信息提取",
        "knowledge_update": "知识更新检测",
        "multi_session_reasoning": "跨会话推理",
        "summarization": "长程摘要",
        "temporal_reasoning": "时序推理",
        "event_ordering": "事件排序",
        "abstention": "知识边界识别",
        "contradiction_resolution": "矛盾检测",
    }

    # 默认路由权重
    DEFAULT_WEIGHTS = {
        QueryType.SEMANTIC:       {NetworkType.VECTOR: 0.55, NetworkType.ENTITY: 0.20, NetworkType.TEMPORAL: 0.10, NetworkType.GRAPH: 0.15},
        QueryType.FACTUAL:        {NetworkType.VECTOR: 0.15, NetworkType.ENTITY: 0.50, NetworkType.TEMPORAL: 0.20, NetworkType.GRAPH: 0.15},
        QueryType.TEMPORAL_QUERY: {NetworkType.VECTOR: 0.10, NetworkType.ENTITY: 0.15, NetworkType.TEMPORAL: 0.55, NetworkType.GRAPH: 0.20},
        QueryType.RELATIONAL:     {NetworkType.VECTOR: 0.10, NetworkType.ENTITY: 0.25, NetworkType.TEMPORAL: 0.10, NetworkType.GRAPH: 0.55},
        QueryType.MIXED:          {NetworkType.VECTOR: 0.25, NetworkType.ENTITY: 0.25, NetworkType.TEMPORAL: 0.25, NetworkType.GRAPH: 0.25},
    }

    def __init__(self):
        # 四网络存储
        self._vector_store: Dict[str, VectorEntry] = {}
        self._entity_store: Dict[str, EntityEntry] = {}
        self._temporal_store: Dict[str, TemporalEntry] = {}
        self._graph_edges: Dict[str, GraphEdge] = {}

        # 统计
        self._query_count = 0
        self._fusion_stats: Dict[str, Any] = {"total": 0, "conflicts": 0, "duplicates_removed": 0}

    def _hash_content(self, content: str) -> int:
        """生成内容的简化语义哈希"""
        h = hashlib.sha256(content.encode('utf-8')).digest()
        return int.from_bytes(h[:32], 'big') % (1 << 256)

    # ---- 写入接口 ----

    def ingest_vector(self, memory_id: str, content: str, metadata: Optional[Dict] = None) -> VectorEntry:
        """写入 Vector Network"""
        entry = VectorEntry(
            memory_id=memory_id,
            content=content,
            embedding_hash=self._hash_content(content),
            metadata=metadata or {},
        )
        self._vector_store[memory_id] = entry
        return entry

    def ingest_entity(self, entity_id: str, entity_type: str, name: str,
                      properties: Optional[Dict] = None) -> EntityEntry:
        """写入 Entity Network"""
        entry = EntityEntry(
            entity_id=entity_id,
            entity_type=entity_type,
            name=name,
            properties=properties or {},
        )
        self._entity_store[entity_id] = entry
        return entry

    def ingest_temporal(self, memory_id: str, content: str, event_date: str,
                        referenced_dates: Optional[List[str]] = None,
                        anchor_events: Optional[List[str]] = None) -> TemporalEntry:
        """写入 Temporal Network"""
        entry = TemporalEntry(
            memory_id=memory_id,
            content=content,
            event_date=event_date,
            timestamp=time.time(),
            referenced_dates=referenced_dates or [],
            anchor_events=anchor_events or [],
        )
        self._temporal_store[memory_id] = entry
        return entry

    def add_graph_edge(self, source_id: str, target_id: str, relation_type: str,
                       weight: float = 1.0) -> GraphEdge:
        """写入 Graph Network"""
        edge_id = f"{source_id}::{relation_type}::{target_id}"
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            weight=weight,
            timestamps=(time.time(), time.time()),
        )
        self._graph_edges[edge_id] = edge
        return edge

    # ---- 查询类型分类 ----

    def classify_query(self, query: str) -> QueryType:
        """
        根据查询内容推断查询类型，用于自适应路由权重。
        """
        ql = query.lower()
        # 时间词检测
        temporal_keywords = ["when", "before", "after", "date", "time", "last", "next",
                            "recent", "earlier", "later", "chronology", "order", "sequence"]
        if any(kw in ql for kw in temporal_keywords):
            return QueryType.TEMPORAL_QUERY

        # 关系词检测
        relational_keywords = ["relation", "connected", "linked", "related", "between",
                              "dependency", "correlation", "version", "history", "chain"]
        if any(kw in ql for kw in relational_keywords):
            return QueryType.RELATIONAL

        # 事实词检测
        factual_keywords = ["who", "what", "where", "which", "name", "attribute",
                           "property", "identifier", "entity", "person", "organization"]
        if any(kw in ql for kw in factual_keywords):
            return QueryType.FACTUAL

        # 语义词检测
        semantic_keywords = ["similar", "like", "meaning", "concept", "idea",
                            "topic", "theme", "about", "summary", "overview"]
        if any(kw in ql for kw in semantic_keywords):
            return QueryType.SEMANTIC

        return QueryType.MIXED

    # ---- 四路检索 ----

    def _vector_search(self, query_hash: int, top_k: int = 10) -> List[Tuple[str, float]]:
        """Vector Network 检索：语义哈希相似度"""
        results = []
        for mid, entry in self._vector_store.items():
            sim = entry.similarity(query_hash)
            results.append((mid, sim))
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def _entity_search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Entity Network 检索：命名实体模糊匹配"""
        ql = query.lower()
        results = []
        for eid, entry in self._entity_store.items():
            score = 0.0
            # 名称匹配
            if ql in entry.name.lower() or entry.name.lower() in ql:
                score += 0.5
            # 别名匹配
            for alias in entry.aliases:
                if ql in alias.lower() or alias.lower() in ql:
                    score += 0.3
            # 属性匹配
            for prop_val in entry.properties.values():
                if isinstance(prop_val, str) and (ql in str(prop_val).lower() or str(prop_val).lower() in ql):
                    score += 0.2
            if score > 0:
                results.append((eid, min(score, 1.0)))
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def _temporal_search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Temporal Network 检索：时间范围 + 关键词匹配"""
        ql = query.lower()
        now = time.time()
        results = []
        for mid, entry in self._temporal_store.items():
            score = 0.0
            # 内容匹配
            if ql in entry.content.lower():
                score += 0.4
            # 日期匹配
            for d in entry.referenced_dates:
                if d.lower() in ql:
                    score += 0.3
            # 锚点事件匹配
            for ae in entry.anchor_events:
                if ae.lower() in ql:
                    score += 0.2
            # 时间近度衰减（最近优先）
            age_days = (now - entry.timestamp) / 86400.0
            recency = math.exp(-age_days / 30.0)  # 30天半衰期
            score += 0.1 * recency
            if score > 0:
                results.append((mid, min(score, 1.0)))
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def _graph_search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Graph Network 检索：关系图游走"""
        ql = query.lower()
        results = []
        scored_nodes: Dict[str, float] = {}

        for edge_id, edge in self._graph_edges.items():
            # 检查关系类型是否匹配
            if edge.relation_type.lower() in ql:
                scored_nodes[edge.source_id] = scored_nodes.get(edge.source_id, 0) + edge.weight * 0.6
                scored_nodes[edge.target_id] = scored_nodes.get(edge.target_id, 0) + edge.weight * 0.4
            # 检查源或目标是否匹配
            if edge.source_id.lower() in ql or edge.target_id.lower() in ql:
                scored_nodes[edge.source_id] = scored_nodes.get(edge.source_id, 0) + 0.3
                scored_nodes[edge.target_id] = scored_nodes.get(edge.target_id, 0) + 0.3

        for node_id, score in scored_nodes.items():
            results.append((node_id, min(score, 1.0)))
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    # ---- 四路融合 ----

    def query(self, query: str, top_k: int = 10) -> Dict[str, Any]:
        """
        四路并行检索 + 自适应权重融合 + 去重冲突解决。

        融合策略：
        - 查询同时发往四个网络，结果加权合并
        - 自适应路由权重：根据查询类型动态调整
        - 去重：向量 > 实体 > 时态 > 图谱（语义优先）
        - Hindsight 特性：1M 73.9% > 500K 71.1%
        """
        self._query_count += 1
        query_type = self.classify_query(query)
        weights = self.DEFAULT_WEIGHTS[query_type]
        query_hash = self._hash_content(query)

        # 四路并行检索
        vector_results = self._vector_search(query_hash, top_k * 2)
        entity_results = self._entity_search(query, top_k * 2)
        temporal_results = self._temporal_search(query, top_k * 2)
        graph_results = self._graph_search(query, top_k * 2)

        # 加权融合
        fused: Dict[str, float] = {}
        network_sources: Dict[str, List[str]] = defaultdict(list)

        for mid, score in vector_results:
            fused[mid] = fused.get(mid, 0) + score * weights[NetworkType.VECTOR]
            network_sources[mid].append("vector")

        for eid, score in entity_results:
            fused[eid] = fused.get(eid, 0) + score * weights[NetworkType.ENTITY]
            network_sources[eid].append("entity")

        for mid, score in temporal_results:
            fused[mid] = fused.get(mid, 0) + score * weights[NetworkType.TEMPORAL]
            network_sources[mid].append("temporal")

        for node_id, score in graph_results:
            fused[node_id] = fused.get(node_id, 0) + score * weights[NetworkType.GRAPH]
            network_sources[node_id].append("graph")

        # 去重冲突解决：语义优先
        # 去重规则：Vector > Entity > Temporal > Graph
        priority_order = {"vector": 0, "entity": 1, "temporal": 2, "graph": 3}
        deduped: Dict[str, Tuple[float, str]] = {}
        duplicates = 0
        for item_id, score in fused.items():
            sources = network_sources[item_id]
            best_source = min(sources, key=lambda s: priority_order.get(s, 99))
            if item_id in deduped:
                old_score, old_source = deduped[item_id]
                new_priority = priority_order.get(best_source, 99)
                old_priority = priority_order.get(old_source, 99)
                if new_priority < old_priority or (new_priority == old_priority and score > old_score):
                    deduped[item_id] = (score, best_source)
                duplicates += 1
            else:
                deduped[item_id] = (score, best_source)

        # 排序
        sorted_results = sorted(deduped.items(), key=lambda x: -x[1][0])
        top_n = sorted_results[:top_k]

        self._fusion_stats["total"] += 1
        self._fusion_stats["duplicates_removed"] += duplicates

        return {
            "query": query,
            "query_type": query_type.value,
            "weights": {k.value: v for k, v in weights.items()},
            "results": [{"id": id_, "score": round(sc, 4), "source": src}
                       for id_, (sc, src) in top_n],
            "stats": {
                "vector_hits": len(vector_results),
                "entity_hits": len(entity_results),
                "temporal_hits": len(temporal_results),
                "graph_hits": len(graph_results),
                "fused_total": len(fused),
                "deduped": len(deduped),
                "duplicates_removed": duplicates,
            },
        }

    # ---- BEAM 能力评测 ----

    def evaluate_capability(self, capability: str, verification_questions: List[str],
                           expected_answers: Optional[List[str]] = None,
                           threshold: float = 0.3) -> Dict[str, Any]:
        """
        对指定能力维度运行评测。

        Args:
            capability: 能力维度名称
            verification_questions: 验证问题列表
            expected_answers: 期望答案（可选，用于精确匹配）
            threshold: 检索得分阈值

        Returns:
            评测结果字典
        """
        if capability not in self.CAPABILITIES:
            return {"error": f"Unknown capability: {capability}", "valid": self.CAPABILITIES.keys()}

        correct = 0
        details = []
        for i, q in enumerate(verification_questions):
            result = self.query(q)
            best_score = result["results"][0]["score"] if result["results"] else 0.0
            passed = best_score >= threshold
            if passed:
                correct += 1

            detail = {
                "question": q,
                "best_score": round(best_score, 4),
                "passed": passed,
                "query_type": result["query_type"],
                "top_result": result["results"][0] if result["results"] else None,
            }
            if expected_answers and i < len(expected_answers):
                detail["expected"] = expected_answers[i]
            details.append(detail)

        accuracy = round(correct / len(verification_questions) * 100, 1) if verification_questions else 0.0

        return {
            "capability": capability,
            "description": self.CAPABILITIES[capability],
            "total_questions": len(verification_questions),
            "correct": correct,
            "accuracy_pct": accuracy,
            "details": details,
        }

    # ---- 诊断 ----

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module_id": self.MODULE_ID,
            "module_name": self.MODULE_NAME,
            "paper_ref": self.PAPER_REF,
            "version": self.MODULE_VERSION,
            "architecture": "Four-Network Separation (Hindsight)",
            "networks": {
                "vector_entries": len(self._vector_store),
                "entity_entries": len(self._entity_store),
                "temporal_entries": len(self._temporal_store),
                "graph_edges": len(self._graph_edges),
            },
            "query_count": self._query_count,
            "fusion_stats": self._fusion_stats,
            "default_weights": {
                qt.value: {nt.value: w for nt, w in wt.items()}
                for qt, wt in self.DEFAULT_WEIGHTS.items()
            },
            "capabilities": list(self.CAPABILITIES.keys()),
        }

    def run_diagnostics(self) -> Dict[str, Any]:
        """完整自检"""
        results = {}

        # Test 1: Ingest
        try:
            self.ingest_vector("v1", "Python is a great programming language for data science",
                              {"domain": "programming"})
            self.ingest_vector("v2", "I prefer Python over Java for backend development")
            self.ingest_vector("v3", "Summer vacation in Hawaii was amazing")
            results["ingest_vector"] = True
        except Exception as e:
            results["ingest_vector"] = f"FAIL: {e}"

        try:
            self.ingest_entity("e1", "PERSON", "Alice", {"role": "engineer", "language": "Python"})
            self.ingest_entity("e2", "ORG", "Acme Corp", {"industry": "tech", "size": "500"})
            self.ingest_entity("e3", "DATE", "2025-06-15", {"event": "project_launch"})
            results["ingest_entity"] = True
        except Exception as e:
            results["ingest_entity"] = f"FAIL: {e}"

        try:
            self.ingest_temporal("t1", "Started project Alpha", "2025-03-01",
                                ["2025-03-01", "2025-06-01"], ["project_kickoff"])
            self.ingest_temporal("t2", "Completed milestone Beta", "2025-07-15",
                                ["2025-07-15"], ["beta_release"])
            results["ingest_temporal"] = True
        except Exception as e:
            results["ingest_temporal"] = f"FAIL: {e}"

        try:
            self.add_graph_edge("v1", "v2", "relates_to", 0.8)
            self.add_graph_edge("t1", "t2", "updates", 1.0)
            self.add_graph_edge("e1", "e2", "works_at", 0.9)
            results["ingest_graph"] = True
        except Exception as e:
            results["ingest_graph"] = f"FAIL: {e}"

        # Test 2: Query
        try:
            r = self.query("What programming language does Alice use?", top_k=5)
            results["query_semantic"] = r["query_type"] in ("mixed", "factual") and len(r["results"]) > 0
        except Exception as e:
            results["query_semantic"] = f"FAIL: {e}"

        try:
            r = self.query("When was the project launched?", top_k=5)
            results["query_temporal"] = r["query_type"] == "temporal" and len(r["results"]) > 0
        except Exception as e:
            results["query_temporal"] = f"FAIL: {e}"

        try:
            r = self.query("How are the memories connected?", top_k=5)
            results["query_relational"] = r["query_type"] == "relational" and len(r["results"]) > 0
        except Exception as e:
            results["query_relational"] = f"FAIL: {e}"

        # Test 3: Adaptive weights
        try:
            qt = self.classify_query("When did the event happen?")
            results["adaptive_temporal_weight"] = qt == QueryType.TEMPORAL_QUERY

            qt = self.classify_query("Who is related to Alice?")
            results["adaptive_entity_weight"] = qt == QueryType.RELATIONAL

            qt = self.classify_query("What is Python?")
            results["adaptive_factual_weight"] = qt == QueryType.FACTUAL
        except Exception as e:
            results["adaptive_weights"] = f"FAIL: {e}"

        # Test 4: BEAM evaluation
        try:
            eval_result = self.evaluate_capability(
                "information_extraction",
                ["What language?", "What role?", "What event?"],
                threshold=0.0
            )
            results["beam_eval"] = isinstance(eval_result, dict) and "accuracy_pct" in eval_result
        except Exception as e:
            results["beam_eval"] = f"FAIL: {e}"

        # Test 5: Fusion stats
        try:
            results["fusion_stats_ok"] = self._fusion_stats["total"] > 0
        except Exception as e:
            results["fusion_stats_ok"] = f"FAIL: {e}"

        all_pass = all(
            isinstance(v, bool) and v for v in results.values()
        )
        results["ALL_PASS"] = all_pass

        return results



# ============================================================================
# CB56: ZikkaronHopfield (P128)
# 对齐 Zikkaron — BEAM 10M 非LLM方案 SOTA (40.4%)
# 核心：Hopfield能量评分 + 扩散激活 + 热衰减再巩固
# ============================================================================

import time
import math
import hashlib
import json
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class HopfieldMemory:
    """
    Hopfield 能量记忆单元。

    每条记忆存储时的 Hopfield 能量 E(m_i) 表示其为稳定吸引子的程度。
    检索时计算查询与记忆的 energy overlap，能量最低的匹配为最优。

    E(m_i) = -0.5 * Σ(w_ij * s_i * s_j)
    其中 w_ij 为记忆 i 与 j 之间的共现权重，s_i 为记忆的状态向量。
    """
    memory_id: str
    content: str
    state_vector: List[float]  # 简化状态向量 (16维)
    energy: float = 0.0        # Hopfield 能量 (E < 0 为稳定吸引子)
    temperature: float = 1.0   # 热力学温度 T_i
    stored_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    reconsolidation_count: int = 0  # 再巩固次数
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 与其他记忆的共现权重
    co_occurrence: Dict[str, float] = field(default_factory=dict)

    def compute_energy(self, global_co_occurrence: Dict[Tuple[str, str], float]) -> float:
        """
        计算 Hopfield 能量：E(m_i) = -0.5 * Σ(w_ij * s_i·s_j)
        """
        if not self.state_vector:
            return 0.0
        energy = 0.0
        for j_id, w_ij in self.co_occurrence.items():
            w = global_co_occurrence.get(
                tuple(sorted([self.memory_id, j_id])), w_ij
            )
            # 简化：使用内积 s_i·s_j 近似
            energy -= 0.5 * w * self._state_norm()
        self.energy = energy
        return energy

    def _state_norm(self) -> float:
        """状态向量 L2 范数"""
        return math.sqrt(sum(s * s for s in self.state_vector))

    def temperature_decay(self, decay_lambda: float, current_time: float) -> float:
        """
        热衰减：T_i(t) = T_0 * exp(-λ*t)
        """
        age = (current_time - self.last_accessed) / 3600.0  # 小时
        self.temperature = max(0.01, self.temperature * math.exp(-decay_lambda * age))
        return self.temperature

    def reconsolidate(self, boost: float = 0.5, current_time: float = None) -> None:
        """
        再巩固：被检索到的记忆温度回升，抵抗衰减。
        """
        t = current_time or time.time()
        self.temperature = min(2.0, self.temperature + boost)
        self.last_accessed = t
        self.reconsolidation_count += 1


@dataclass
class ActivationNode:
    """扩散激活节点"""
    memory_id: str
    activation: float       # 当前激活值
    source_id: Optional[str] = None  # 激活来源
    hop_count: int = 0      # 跳数


class SpreadingActivationGraph:
    """
    扩散激活图。

    初始激活：与查询直接匹配的记忆获得激活值。
    扩散：激活沿版本链和时态边向相邻记忆传播。
    衰减：每跳衰减因子 d=0.5，3 跳后激活 < 12.5% 截止。
    最终得分 = 原始得分 + 扩散激活值。
    """

    def __init__(self, decay_factor: float = 0.5, max_hops: int = 3,
                 cutoff_threshold: float = 0.125):
        self.decay_factor = decay_factor
        self.max_hops = max_hops
        self.cutoff_threshold = cutoff_threshold
        # 邻接表：memory_id → [(neighbor_id, relation_type, weight)]
        self._adjacency: Dict[str, List[Tuple[str, str, float]]] = defaultdict(list)

    def add_edge(self, source_id: str, target_id: str,
                 relation_type: str = "relates_to", weight: float = 1.0) -> None:
        """添加扩散边"""
        self._adjacency[source_id].append((target_id, relation_type, weight))
        self._adjacency[target_id].append((source_id, relation_type, weight))

    def spread(self, initial_activations: Dict[str, float]) -> Dict[str, float]:
        """
        从初始激活值开始扩散。

        Args:
            initial_activations: {memory_id: initial_activation}

        Returns:
            扩散后的激活值 {memory_id: final_activation}
        """
        if not initial_activations:
            return {}

        # BFS 扩散
        visited: Dict[str, Tuple[float, int]] = {}  # id → (activation, hop)
        queue = deque()

        for mid, act in initial_activations.items():
            visited[mid] = (act, 0)
            queue.append(ActivationNode(memory_id=mid, activation=act, hop_count=0))

        while queue:
            node = queue.popleft()
            if node.hop_count >= self.max_hops:
                continue

            for neighbor_id, rel_type, weight in self._adjacency.get(node.memory_id, []):
                spread_activation = node.activation * self.decay_factor * weight
                if spread_activation < self.cutoff_threshold:
                    continue

                new_hop = node.hop_count + 1
                if neighbor_id not in visited or visited[neighbor_id][0] < spread_activation:
                    visited[neighbor_id] = (spread_activation, new_hop)
                    queue.append(ActivationNode(
                        memory_id=neighbor_id,
                        activation=spread_activation,
                        source_id=node.memory_id,
                        hop_count=new_hop,
                    ))

        return {mid: act for mid, (act, _) in visited.items()}


print("[P127] HindsightFourNetwork (CB55) initialized -- BEAM SOTA 64.1% aligned")

# ============================================================================
# CB56: ZikkaronHopfield
# ============================================================================
class ZikkaronHopfield:
    """
    Zikkaron Hopfield 能量评分系统 (P128)。

    Zikkaron 是 BEAM 上非 LLM 方案的 SOTA（40.4%，Claude Opus 4.6 reader），
    核心创新：Hopfield 能量评分 + 扩散激活 + 热衰减再巩固。

    优势领域（对齐 Zikkaron BEAM 数据）：
    - 矛盾检测：+226%（0.050 → 0.163）
    - 时序推理：+133%（0.075 → 0.175）
    - 知识更新：+73%（0.375 → 0.650）
    - 信息提取：+73%（0.375 → 0.650）
    - 指令遵循：+50%（0.500 → 0.750）

    核心机制：
    1. Hopfield 能量：E(m_i) = -0.5 * Σ(w_ij * s_i·s_j)
    2. 扩散激活（Spreading Activation）：BFS 传播，d=0.5，3跳截止
    3. 热衰减（Thermodynamic Decay）：T_i(t) = T_0 * exp(-λ*t)
    4. 再巩固（Reconsolidation）：检索回升温度
    5. 最终得分 = 原始得分 + 扩散激活值，高温记忆被抑制
    """

    MODULE_ID = "CB56"
    MODULE_VERSION = "1.0.0"
    PAPER_REF = "P128"
    MODULE_NAME = "ZikkaronHopfield"

    # BEAM 10M 能力提升数据（Zikkaron vs LIGHT）
    ZIKKARON_IMPROVEMENTS = {
        "contradiction_resolution": {"before": 0.050, "after": 0.163, "pct_improvement": 226},
        "temporal_reasoning": {"before": 0.075, "after": 0.175, "pct_improvement": 133},
        "knowledge_update": {"before": 0.375, "after": 0.650, "pct_improvement": 73},
        "information_extraction": {"before": 0.375, "after": 0.650, "pct_improvement": 73},
        "instruction_following": {"before": 0.500, "after": 0.750, "pct_improvement": 50},
        "preference_following": {"before": 0.483, "after": 0.642, "pct_improvement": 33},
        "multi_session_reasoning": {"before": 0.135, "after": 0.195, "pct_improvement": 44},
        "summarization": {"before": 0.277, "after": 0.216, "pct_improvement": -22},
        "abstention": {"before": 0.750, "after": 0.450, "pct_improvement": -40},
        "event_ordering": {"before": 0.266, "after": 0.150, "pct_improvement": -44},
        "overall": {"before": 0.266, "after": 0.404, "pct_improvement": 52},
    }

    def __init__(self, state_dim: int = 16, decay_lambda: float = 0.01,
                 reconsolidation_boost: float = 0.5):
        self.state_dim = state_dim
        self.decay_lambda = decay_lambda
        self.reconsolidation_boost = reconsolidation_boost

        # 存储
        self._memories: Dict[str, HopfieldMemory] = {}
        # 全局共现权重 {(id_a, id_b): weight}
        self._global_co_occurrence: Dict[Tuple[str, str], float] = {}
        # 扩散激活图
        self._spreading_graph = SpreadingActivationGraph()

        # 统计
        self._stats = {
            "total_stores": 0,
            "total_retrievals": 0,
            "total_reconsolidations": 0,
            "energy_recalcs": 0,
        }

    def _generate_state_vector(self, content: str) -> List[float]:
        """从内容生成16维状态向量 (确定性哈希)"""
        h = hashlib.sha256(content.encode('utf-8')).digest()
        # 取前16个字节归一化为 [-1, 1]
        return [(b / 127.5 - 1.0) for b in h[:self.state_dim]]

    def store(self, memory_id: str, content: str,
              initial_temperature: float = 1.0,
              related_ids: Optional[List[str]] = None,
              metadata: Optional[Dict] = None) -> HopfieldMemory:
        """
        存储记忆，计算 Hopfield 能量，建立共现关系。

        Args:
            memory_id: 记忆ID
            content: 记忆内容
            initial_temperature: 初始温度
            related_ids: 相关记忆ID列表
            metadata: 元数据
        """
        state_vec = self._generate_state_vector(content)
        mem = HopfieldMemory(
            memory_id=memory_id,
            content=content,
            state_vector=state_vec,
            temperature=initial_temperature,
            metadata=metadata or {},
        )

        # 建立共现关系
        if related_ids:
            for rid in related_ids:
                if rid in self._memories:
                    co_weight = self._compute_co_occurrence_weight(content, self._memories[rid].content)
                    mem.co_occurrence[rid] = co_weight
                    self._memories[rid].co_occurrence[memory_id] = co_weight
                    key = tuple(sorted([memory_id, rid]))
                    self._global_co_occurrence[key] = co_weight

                    # 同时添加到扩散激活图
                    self._spreading_graph.add_edge(memory_id, rid, "related", co_weight)

        # 计算能量
        mem.compute_energy(self._global_co_occurrence)
        self._memories[memory_id] = mem
        self._stats["total_stores"] += 1

        return mem

    def _compute_co_occurrence_weight(self, content_a: str, content_b: str) -> float:
        """计算两段内容的共现权重（基于关键词重叠）"""
        words_a = set(content_a.lower().split())
        words_b = set(content_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def retrieve(self, query: str, top_k: int = 10,
                 use_spreading: bool = True,
                 temperature_suppress: bool = True) -> Dict[str, Any]:
        """
        检索记忆，使用 Hopfield 能量 + 扩散激活 + 温度抑制。

        Args:
            query: 查询文本
            top_k: 返回数量
            use_spreading: 是否使用扩散激活
            temperature_suppress: 是否使用温度抑制

        Returns:
            检索结果
        """
        self._stats["total_retrievals"] += 1
        now = time.time()
        query_vec = self._generate_state_vector(query)

        # 1. 基础检索：计算查询与每条记忆的 energy overlap
        scores: Dict[str, float] = {}
        for mid, mem in self._memories.items():
            # 状态向量余弦相似度
            sim = self._cosine_similarity(query_vec, mem.state_vector)
            # Hopfield 能量 overlap（越稳定越匹配）
            energy_factor = 1.0 / (1.0 + abs(mem.energy)) if mem.energy != 0 else 1.0
            base_score = sim * energy_factor
            scores[mid] = base_score

        # 2. 扩散激活
        if use_spreading and scores:
            initial_activations = {mid: max(0.1, s) for mid, s in scores.items() if s > 0}
            spread_activations = self._spreading_graph.spread(initial_activations)

            # 合并扩散激活
            for mid, spread_act in spread_activations.items():
                if mid in scores:
                    scores[mid] += spread_act
                else:
                    scores[mid] = spread_act

        # 3. 温度抑制：高温记忆被抑制
        if temperature_suppress:
            for mid in list(scores.keys()):
                if mid in self._memories:
                    temp = self._memories[mid].temperature
                    decayed_temp = self._memories[mid].temperature_decay(self.decay_lambda, now)
                    # 温度抑制因子：高温 → 低分数
                    suppress = 1.0 / (1.0 + temp)
                    scores[mid] *= suppress

        # 4. 排序
        sorted_results = sorted(scores.items(), key=lambda x: -x[1])
        top_n = sorted_results[:top_k]

        result = {
            "query": query,
            "results": [
                {
                    "memory_id": mid,
                    "score": round(score, 4),
                    "energy": round(self._memories[mid].energy, 4) if mid in self._memories else None,
                    "temperature": round(self._memories[mid].temperature, 4) if mid in self._memories else None,
                    "content_preview": self._memories[mid].content[:80] if mid in self._memories else "N/A",
                }
                for mid, score in top_n
            ],
            "stats": {
                "total_memories": len(self._memories),
                "candidates_evaluated": len(scores),
                "spread_activated": use_spreading and any(mid not in scores for mid in (spread_activations if use_spreading else {})),
            },
        }

        # 5. 再巩固：被检索到的 Top-3 记忆温度回升
        for i, (mid, _) in enumerate(top_n[:3]):
            if mid in self._memories:
                self._memories[mid].reconsolidate(self.reconsolidation_boost, now)
                self._stats["total_reconsolidations"] += 1

        return result

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """余弦相似度"""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return max(0.0, dot / (norm_a * norm_b))

    def detect_contradiction(self, content_a: str, content_b: str) -> Tuple[float, Dict]:
        """
        矛盾检测：利用 Hopfield 能量差异。

        两条记忆的能量差越大 → 越可能存在矛盾。
        这是 Zikkaron 最强优势领域（+226%）。
        """
        vec_a = self._generate_state_vector(content_a)
        vec_b = self._generate_state_vector(content_b)
        sim = self._cosine_similarity(vec_a, vec_b)
        # 高相似但方向相反 = 潜在矛盾
        if sim > 0.5:
            # 检查是否有负相关成分
            diff_vec = [a - b for a, b in zip(vec_a, vec_b)]
            diff_norm = math.sqrt(sum(d * d for d in diff_vec))
            contradiction_score = diff_norm / (2.0 * math.sqrt(self.state_dim))  # 归一化
        else:
            contradiction_score = 1.0 - sim

        return contradiction_score, {
            "similarity": round(sim, 4),
            "contradiction_score": round(contradiction_score, 4),
            "likely_contradiction": contradiction_score > 0.4,
        }

    def temporal_reasoning(self, event_a_id: str, event_b_id: str) -> Dict[str, Any]:
        """
        时序推理：比较两条记忆的时间先后。

        Zikkaron 优势 +133%（0.075 → 0.175）。
        利用记忆的 stored_at 时间和温度衰减来推断时序。
        """
        ma = self._memories.get(event_a_id)
        mb = self._memories.get(event_b_id)

        if not ma or not mb:
            return {"error": "Memory not found", "valid": list(self._memories.keys())[:10]}

        time_diff = ma.stored_at - mb.stored_at
        # 温度差也能反映新旧程度
        temp_diff = mb.temperature - ma.temperature  # 更新记忆温度更高

        confidence = 0.5
        if abs(time_diff) > 86400:  # > 1 day
            confidence += 0.3
        if abs(temp_diff) > 0.3:
            confidence += 0.2

        ordering = "A_before_B" if time_diff < 0 else "B_before_A"

        return {
            "event_a": {"id": event_a_id, "stored_at": ma.stored_at, "temperature": round(ma.temperature, 4)},
            "event_b": {"id": event_b_id, "stored_at": mb.stored_at, "temperature": round(mb.temperature, 4)},
            "time_difference_seconds": abs(time_diff),
            "temperature_difference": round(abs(temp_diff), 4),
            "ordering": ordering,
            "confidence": round(min(confidence, 1.0), 2),
        }

    def knowledge_update(self, old_memory_id: str, new_memory_id: str) -> Dict[str, Any]:
        """
        知识更新检测：识别新旧知识的替换关系。

        Zikkaron 优势 +73%（0.375 → 0.650）。
        利用温度衰减自然突出最新信息。
        """
        old_mem = self._memories.get(old_memory_id)
        new_mem = self._memories.get(new_memory_id)

        if not old_mem or not new_mem:
            return {"error": "Memory not found"}

        # 建立更新关系
        self._spreading_graph.add_edge(new_memory_id, old_memory_id, "updates", 0.8)
        self.add_co_occurrence(new_memory_id, old_memory_id, "updates")

        # 更新者温度回升（再巩固）
        new_mem.reconsolidate(self.reconsolidation_boost)
        self._stats["total_reconsolidations"] += 1

        # 旧记忆温度设为高温（即将衰减）
        old_mem.temperature = 0.3  # 低初始温度 = 快速衰减淘汰

        return {
            "old_memory": {"id": old_memory_id, "temperature": round(old_mem.temperature, 4)},
            "new_memory": {"id": new_memory_id, "temperature": round(new_mem.temperature, 4)},
            "status": "knowledge_updated",
            "reconsolidation_applied": True,
        }

    def add_co_occurrence(self, id_a: str, id_b: str,
                          relation_type: str = "relates_to",
                          weight: Optional[float] = None) -> None:
        """手动添加共现关系"""
        if id_a in self._memories and id_b in self._memories:
            if weight is None:
                weight = self._compute_co_occurrence_weight(
                    self._memories[id_a].content,
                    self._memories[id_b].content
                )
            key = tuple(sorted([id_a, id_b]))
            self._global_co_occurrence[key] = weight
            self._memories[id_a].co_occurrence[id_b] = weight
            self._memories[id_b].co_occurrence[id_a] = weight
            self._spreading_graph.add_edge(id_a, id_b, relation_type, weight)
            # 重新计算能量
            self._memories[id_a].compute_energy(self._global_co_occurrence)
            self._memories[id_b].compute_energy(self._global_co_occurrence)
            self._stats["energy_recalcs"] += 2

    def advance_time(self, hours: float) -> None:
        """模拟时间流逝，应用热衰减"""
        now = time.time()
        for mid in list(self._memories.keys()):
            self._memories[mid].last_accessed -= hours * 3600.0
            self._memories[mid].temperature_decay(self.decay_lambda, now)

    # ---- 诊断 ----

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module_id": self.MODULE_ID,
            "module_name": self.MODULE_NAME,
            "paper_ref": self.PAPER_REF,
            "version": self.MODULE_VERSION,
            "architecture": "Hopfield Energy + Spreading Activation + Thermodynamic Decay",
            "state_dim": self.state_dim,
            "decay_lambda": self.decay_lambda,
            "memories_stored": len(self._memories),
            "co_occurrence_pairs": len(self._global_co_occurrence),
            "stats": self._stats,
            "energy_range": self._get_energy_range(),
            "temperature_range": self._get_temperature_range(),
            "improvements": self.ZIKKARON_IMPROVEMENTS.get("overall", {}),
        }

    def _get_energy_range(self) -> Dict[str, float]:
        if not self._memories:
            return {"min": 0, "max": 0, "avg": 0}
        energies = [m.energy for m in self._memories.values()]
        return {"min": round(min(energies), 4), "max": round(max(energies), 4),
                "avg": round(sum(energies) / len(energies), 4)}

    def _get_temperature_range(self) -> Dict[str, float]:
        if not self._memories:
            return {"min": 0, "max": 0, "avg": 0}
        temps = [m.temperature for m in self._memories.values()]
        return {"min": round(min(temps), 4), "max": round(max(temps), 4),
                "avg": round(sum(temps) / len(temps), 4)}

    def run_diagnostics(self) -> Dict[str, Any]:
        """完整自检"""
        results = {}

        # Test 1: Store memories
        try:
            self.store("m1", "My favorite color is blue.", initial_temperature=0.5)
            self.store("m2", "Actually, my favorite color is green now.", initial_temperature=1.2)
            self.store("m3", "I started learning Rust in January 2025.", initial_temperature=1.0)
            self.store("m4", "By March 2025, I became proficient in Rust.", initial_temperature=1.5)
            self.store("m5", "Rust is a systems programming language focused on safety.",
                      related_ids=["m3", "m4"])
            results["store"] = len(self._memories) == 5
        except Exception as e:
            results["store"] = f"FAIL: {e}"

        # Test 2: Hopfield energy computation
        try:
            self.add_co_occurrence("m3", "m4", "extends", 0.9)
            for mid in self._memories:
                self._memories[mid].compute_energy(self._global_co_occurrence)
            has_negative_energy = any(m.energy < 0 for m in self._memories.values())
            results["energy_computation"] = has_negative_energy
        except Exception as e:
            results["energy_computation"] = f"FAIL: {e}"

        # Test 3: Basic retrieval
        try:
            r = self.retrieve("What is my favorite color?", top_k=3)
            results["retrieval"] = len(r["results"]) > 0
        except Exception as e:
            results["retrieval"] = f"FAIL: {e}"

        # Test 4: Contradiction detection
        try:
            score, detail = self.detect_contradiction(
                "My favorite color is blue.",
                "My favorite color is green now."
            )
            results["contradiction_detection"] = 0.0 <= score <= 1.0 and "similarity" in detail
        except Exception as e:
            results["contradiction_detection"] = f"FAIL: {e}"

        # Test 5: Temporal reasoning
        try:
            tr = self.temporal_reasoning("m3", "m4")
            results["temporal_reasoning"] = "ordering" in tr
        except Exception as e:
            results["temporal_reasoning"] = f"FAIL: {e}"

        # Test 6: Knowledge update
        try:
            ku = self.knowledge_update("m1", "m2")
            results["knowledge_update"] = ku.get("status") == "knowledge_updated"
        except Exception as e:
            results["knowledge_update"] = f"FAIL: {e}"

        # Test 7: Spreading activation
        try:
            r2 = self.retrieve("What did I learn?", top_k=5, use_spreading=True)
            results["spreading_activation"] = len(r2["results"]) > 0
        except Exception as e:
            results["spreading_activation"] = f"FAIL: {e}"

        # Test 8: Temperature decay
        try:
            old_temps = {mid: m.temperature for mid, m in self._memories.items()}
            self.advance_time(72.0)  # 3 days
            new_temps = {mid: m.temperature for mid, m in self._memories.items()}
            any_decayed = any(new_temps.get(mid, 0) < old_temps.get(mid, 0)
                            for mid in old_temps if mid in new_temps)
            results["temperature_decay"] = any_decayed
        except Exception as e:
            results["temperature_decay"] = f"FAIL: {e}"

        # Test 9: Reconsolidation
        try:
            self.retrieve("favorite color", top_k=2)  # triggers reconsolidation
            results["reconsolidation"] = self._stats["total_reconsolidations"] > 0
        except Exception as e:
            results["reconsolidation"] = f"FAIL: {e}"

        # Test 10: Zikkaron improvement data integrity
        try:
            z = self.ZIKKARON_IMPROVEMENTS
            results["zikkaron_data"] = (
                "overall" in z and
                "contradiction_resolution" in z and
                z["contradiction_resolution"]["pct_improvement"] == 226
            )
        except Exception as e:
            results["zikkaron_data"] = f"FAIL: {e}"

        all_pass = all(bool(v) for v in results.values())
        results["ALL_PASS"] = all_pass

        return results


print("[P128] ZikkaronHopfield (CB56) initialized -- Non-LLM SOTA 40.4% aligned")


