"""
# status: orphan (2026-08-15 audit, not in runtime path)
P18-8: Context Management Engine — Three-Layer Lean Architecture
=================================================================

对标 2026 三层精益上下文架构。

设计要点：
  - Layer1 压缩层：重要性评分 + 语义分块 + 指令/事实/噪声三级分类
  - Layer2 路由层：意图分类 + 分层存储 + 跨区融合
  - Layer3 执行层：KV 缓存复用 + 推测解码
  - 动态预算分配：按意图类型和复杂度自适应分配 token 预算

核心组件：
  - CompressionLayer:      Layer1 压缩层（评分/分块/分类）
  - RoutingLayer:          Layer2 路由层（意图/存储/融合）
  - ExecutionLayer:        Layer3 执行层（KV缓存/推测解码）
  - DynamicBudgetAllocator: 动态预算分配
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class ContentClass(Enum):
    """内容三级分类。"""
    INSTRUCTION = "instruction"    # 指令：系统提示、任务定义
    FACT = "fact"                  # 事实：知识、数据
    NOISE = "noise"                # 噪声：重复、闲聊、无关


class IntentCategory(Enum):
    """意图分类。"""
    QUERY = "query"                # 查询检索
    REASON = "reason"              # 推理分析
    GENERATE = "generate"          # 生成创作
    CONVERSATION = "conversation"  # 闲聊对话
    COMMAND = "command"            # 系统指令


class StorageTier(Enum):
    """存储层级。"""
    HOT = "hot"                    # KV 缓存（毫秒级）
    WARM = "warm"                  # 压缩摘要（秒级）
    COLD = "cold"                  # 持久化存储（异步）


class DecodeMode(Enum):
    """解码模式。"""
    STANDARD = "standard"          # 标准自回归
    SPECULATIVE = "speculative"    # 推测解码
    KV_REUSE = "kv_reuse"          # KV 缓存复用


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ContentChunk:
    """语义分块。"""
    chunk_id: str
    content: str
    importance_score: float
    content_class: ContentClass
    token_estimate: int = 0
    embedding: Optional[List[float]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class IntentResult:
    """意图分类结果。"""
    intent_id: str
    category: IntentCategory
    confidence: float
    complexity: float                    # 0~1 复杂度
    expected_tokens: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextKVBlock:
    """KV 缓存块（上下文引擎专用）。"""
    block_id: str
    token_range: Tuple[int, int]
    content_hash: str
    layer_indices: List[int] = field(default_factory=list)
    access_count: int = 0
    last_hit: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)


@dataclass
class BudgetPlan:
    """预算分配方案。"""
    plan_id: str
    intent: IntentCategory
    total_budget: int
    compression_budget: int = 0
    routing_budget: int = 0
    execution_budget: int = 0
    reserve: int = 0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

IMPORTANCE_KEYWORDS: Dict[ContentClass, List[str]] = {
    ContentClass.INSTRUCTION: ["must", "required", "important", "关键", "必须", "重要", "always", "never"],
    ContentClass.FACT: ["fact", "data", "result", "发现", "根据", "according", "evidence"],
    ContentClass.NOISE: ["um", "uh", "anyway", "by the way", "顺便", "then", "ok"],
}

INTENT_BUDGET_MAP: Dict[IntentCategory, Dict[str, int]] = {
    IntentCategory.QUERY: {"compression": 20, "routing": 10, "execution": 70, "reserve": 0},
    IntentCategory.REASON: {"compression": 10, "routing": 10, "execution": 75, "reserve": 5},
    IntentCategory.GENERATE: {"compression": 5, "routing": 5, "execution": 85, "reserve": 5},
    IntentCategory.CONVERSATION: {"compression": 30, "routing": 20, "execution": 40, "reserve": 10},
    IntentCategory.COMMAND: {"compression": 15, "routing": 5, "execution": 75, "reserve": 5},
}


# ============================================================================
# Layer 1: Compression
# ============================================================================

class CompressionLayer:
    """Layer1 压缩层：重要性评分 + 语义分块 + 三级分类。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.chunks: List[ContentChunk] = []
        self.chunk_index: Dict[str, ContentChunk] = {}

    def score_importance(self, content: str) -> float:
        """基于关键词启发式重要性评分。"""
        content_lower = content.lower()
        instruction_score = sum(0.15 for kw in IMPORTANCE_KEYWORDS[ContentClass.INSTRUCTION] if kw in content_lower)
        fact_score = sum(0.10 for kw in IMPORTANCE_KEYWORDS[ContentClass.FACT] if kw in content_lower)
        noise_score = sum(0.05 for kw in IMPORTANCE_KEYWORDS[ContentClass.NOISE] if kw in content_lower)
        length_factor = min(len(content) / 500, 1.0) * 0.2
        score = min(0.3 + instruction_score + fact_score - noise_score * 0.5 + length_factor, 1.0)
        return max(0.05, score)

    def classify(self, content: str) -> ContentClass:
        """三级分类：指令 / 事实 / 噪声。"""
        content_lower = content.lower()
        inst_hits = sum(kw in content_lower for kw in IMPORTANCE_KEYWORDS[ContentClass.INSTRUCTION])
        fact_hits = sum(kw in content_lower for kw in IMPORTANCE_KEYWORDS[ContentClass.FACT])
        noise_hits = sum(kw in content_lower for kw in IMPORTANCE_KEYWORDS[ContentClass.NOISE])

        if inst_hits > max(fact_hits, noise_hits, 1):
            return ContentClass.INSTRUCTION
        elif noise_hits > max(inst_hits, fact_hits, 1):
            return ContentClass.NOISE
        else:
            return ContentClass.FACT

    def chunk(self, content: str, max_chunk_tokens: int = 512) -> List[ContentChunk]:
        """语义分块：按句子边界切分。"""
        with self._lock:
            chunks: List[ContentChunk] = []
            sentences = content.replace("!", ".").replace("?", ".").split(".")
            current_chunk = ""
            current_tokens = 0

            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                sent_tokens = len(sent.split())
                if current_tokens + sent_tokens > max_chunk_tokens and current_chunk:
                    chunk = self._create_chunk(current_chunk.strip())
                    chunks.append(chunk)
                    current_chunk = sent
                    current_tokens = sent_tokens
                else:
                    if current_chunk:
                        current_chunk += ". " + sent
                    else:
                        current_chunk = sent
                    current_tokens += sent_tokens

            if current_chunk.strip():
                chunk = self._create_chunk(current_chunk.strip())
                chunks.append(chunk)

            self.chunks.extend(chunks)
            return chunks

    def _create_chunk(self, text: str) -> ContentChunk:
        chunk = ContentChunk(
            chunk_id=str(uuid.uuid4())[:8],
            content=text,
            importance_score=self.score_importance(text),
            content_class=self.classify(text),
            token_estimate=len(text.split()),
        )
        self.chunk_index[chunk.chunk_id] = chunk
        return chunk

    def top_chunks(self, n: int = 10, min_importance: float = 0.0,
                   exclude_class: Optional[ContentClass] = None) -> List[ContentChunk]:
        with self._lock:
            candidates = self.chunks
            if min_importance > 0:
                candidates = [c for c in candidates if c.importance_score >= min_importance]
            if exclude_class:
                candidates = [c for c in candidates if c.content_class != exclude_class]
            candidates.sort(key=lambda c: c.importance_score, reverse=True)
            return candidates[:n]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            class_counts = defaultdict(int)
            total_importance = 0.0
            for c in self.chunks:
                class_counts[c.content_class.value] += 1
                total_importance += c.importance_score
            return {
                "total_chunks": len(self.chunks),
                "by_class": dict(class_counts),
                "avg_importance": round(total_importance / max(len(self.chunks), 1), 4),
            }


# ============================================================================
# Layer 2: Routing
# ============================================================================

class RoutingLayer:
    """Layer2 路由层：意图分类 + 分层存储路由 + 跨区融合。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.intents: List[IntentResult] = []
        self.storage_routes: Dict[str, StorageTier] = {}

    def classify_intent(self, query: str) -> IntentResult:
        """意图分类：查询 / 推理 / 生成 / 闲聊 / 指令。"""
        with self._lock:
            q_lower = query.lower()
            intent_id = str(uuid.uuid4())[:8]

            # 启发式意图分类
            if any(kw in q_lower for kw in ["find", "search", "检索", "查询", "查找", "get", "show", "列出"]):
                category, confidence, complexity = IntentCategory.QUERY, 0.85, 0.3
            elif any(kw in q_lower for kw in ["why", "reason", "分析", "推理", "analyze", "explain"]):
                category, confidence, complexity = IntentCategory.REASON, 0.80, 0.6
            elif any(kw in q_lower for kw in ["create", "生成", "write", "编写", "画", "设计"]):
                category, confidence, complexity = IntentCategory.GENERATE, 0.90, 0.7
            elif any(kw in q_lower for kw in ["set", "config", "配置", "设置", "执行", "run"]):
                category, confidence, complexity = IntentCategory.COMMAND, 0.95, 0.2
            else:
                category, confidence, complexity = IntentCategory.CONVERSATION, 0.70, 0.1

            result = IntentResult(
                intent_id=intent_id, category=category, confidence=confidence,
                complexity=complexity, expected_tokens=50 + int(complexity * 1000),
            )
            self.intents.append(result)
            return result

    def route_storage(self, chunk: ContentChunk) -> StorageTier:
        """基于重要性评分的分层存储路由。"""
        if chunk.importance_score > 0.7 and chunk.content_class != ContentClass.NOISE:
            tier = StorageTier.HOT
        elif chunk.importance_score > 0.3:
            tier = StorageTier.WARM
        else:
            tier = StorageTier.COLD
        self.storage_routes[chunk.chunk_id] = tier
        return tier

    def fuse(self, hot_chunks: List[ContentChunk], warm_summaries: List[str],
             cold_context: Optional[str] = None) -> str:
        """跨区融合：HOT KV + WARM 摘要 + COLD 持久化。"""
        parts: List[str] = []

        if hot_chunks:
            parts.append("## Active Context (HOT)\n" + "\n".join(c.content for c in hot_chunks[:3]))

        if warm_summaries:
            parts.append("## Recent Summaries (WARM)\n" + "\n".join(warm_summaries[:5]))

        if cold_context:
            parts.append("## Archived Knowledge (COLD)\n" + cold_context[:500])

        return "\n\n".join(parts)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            intent_counts = defaultdict(int)
            for i in self.intents:
                intent_counts[i.category.value] += 1
            storage_counts = defaultdict(int)
            for t in self.storage_routes.values():
                storage_counts[t.value] += 1
            return {
                "total_intents": len(self.intents),
                "by_intent": dict(intent_counts),
                "storage_routes": dict(storage_counts),
            }


# ============================================================================
# Layer 3: Execution
# ============================================================================

class ExecutionLayer:
    """Layer3 执行层：KV 缓存复用 + 推测解码。"""

    def __init__(self, max_blocks: int = 1000):
        self._lock = threading.RLock()
        self.kv_blocks: Dict[str, ContextKVBlock] = {}
        self.max_blocks = max_blocks
        self.reuse_hits: int = 0
        self.reuse_misses: int = 0

    def cache_kv(self, tokens: List[int], layer_indices: Optional[List[int]] = None) -> str:
        """缓存 KV 块。"""
        with self._lock:
            content = "|".join(str(t) for t in tokens)
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            block = ContextKVBlock(
                block_id=str(uuid.uuid4())[:8],
                token_range=(0, len(tokens)),
                content_hash=content_hash,
                layer_indices=layer_indices or list(range(32)),
            )
            self.kv_blocks[block.block_id] = block

            # LRU 淘汰
            if len(self.kv_blocks) > self.max_blocks:
                sorted_blocks = sorted(self.kv_blocks.items(), key=lambda x: x[1].last_hit)
                for bid, _ in sorted_blocks[:len(self.kv_blocks) - self.max_blocks]:
                    self.kv_blocks.pop(bid, None)
            return block.block_id

    def reuse(self, content_hash: str) -> Optional[ContextKVBlock]:
        """KV 缓存复用：匹配相同前缀。"""
        with self._lock:
            for block in self.kv_blocks.values():
                if block.content_hash == content_hash:
                    block.access_count += 1
                    block.last_hit = time.time()
                    self.reuse_hits += 1
                    return block
            self.reuse_misses += 1
            return None

    def speculative_decode(self, tokens: List[int], draft_model_output: List[Tuple[int, float]],
                            acceptance_threshold: float = 0.7) -> List[int]:
        """推测解码：草稿模型快速生成 → 目标模型验证。"""
        with self._lock:
            accepted: List[int] = []
            for token_id, confidence in draft_model_output:
                if confidence >= acceptance_threshold:
                    accepted.append(token_id)
                else:
                    break
            return accepted

    def hit_rate(self) -> float:
        total = self.reuse_hits + self.reuse_misses
        return self.reuse_hits / max(total, 1)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "cached_blocks": len(self.kv_blocks),
                "reuse_hits": self.reuse_hits,
                "reuse_misses": self.reuse_misses,
                "hit_rate": round(self.hit_rate(), 4),
            }


# ============================================================================
# Dynamic Budget Allocator
# ============================================================================

class DynamicBudgetAllocator:
    """动态预算分配：按意图类型和复杂度自适应分配 token 预算。"""

    def __init__(self, default_budget: int = 4096):
        self._lock = threading.RLock()
        self.default_budget = default_budget
        self.plans: List[BudgetPlan] = []

    def allocate(self, intent: IntentResult, total_budget: Optional[int] = None) -> BudgetPlan:
        with self._lock:
            total = total_budget or self.default_budget
            base = INTENT_BUDGET_MAP.get(intent.category, INTENT_BUDGET_MAP[IntentCategory.CONVERSATION])

            # 复杂度调整
            complexity_factor = 1 + intent.complexity
            plan = BudgetPlan(
                plan_id=str(uuid.uuid4())[:8],
                intent=intent.category,
                total_budget=total,
                compression_budget=int(total * base["compression"] / 100),
                routing_budget=int(total * base["routing"] / 100),
                execution_budget=int(total * base["execution"] / 100 * complexity_factor),
                reserve=int(total * base["reserve"] / 100),
            )
            self.plans.append(plan)
            return plan

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            intent_stats = defaultdict(lambda: {"count": 0, "avg_exec_budget": 0})
            for p in self.plans:
                intent_stats[p.intent.value]["count"] += 1
                intent_stats[p.intent.value]["avg_exec_budget"] += p.execution_budget
            for k in intent_stats:
                c = max(intent_stats[k]["count"], 1)
                intent_stats[k]["avg_exec_budget"] = round(intent_stats[k]["avg_exec_budget"] / c, 1)
            return {
                "total_plans": len(self.plans),
                "by_intent": {k: dict(v) for k, v in intent_stats.items()},
            }


# ============================================================================
# Orchestrator
# ============================================================================

class ContextManagementEngine:
    """三层精益上下文管理引擎总控。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.compression = CompressionLayer()
        self.routing = RoutingLayer()
        self.execution = ExecutionLayer()
        self.budget = DynamicBudgetAllocator()

    def process(self, content: str, query: str) -> Dict[str, Any]:
        """完整上下文处理流水线。"""
        with self._lock:
            # Layer 1: 压缩
            chunks = self.compression.chunk(content)
            top = self.compression.top_chunks(n=5, min_importance=0.3, exclude_class=ContentClass.NOISE)

            # Layer 2: 路由
            intent = self.routing.classify_intent(query)
            for c in chunks:
                self.routing.route_storage(c)

            # Layer 3: 执行
            hot_chunks = [c for c in top if self.routing.storage_routes.get(c.chunk_id) == StorageTier.HOT]
            fused = self.routing.fuse(hot_chunks, [c.content for c in top if self.routing.storage_routes.get(c.chunk_id) == StorageTier.WARM])

            # 预算分配
            plan = self.budget.allocate(intent)

            return {
                "intent": intent.category.value,
                "complexity": intent.complexity,
                "chunks_total": len(chunks),
                "chunks_retained": len(top),
                "fused_length": len(fused),
                "budget": {
                    "total": plan.total_budget,
                    "compression": plan.compression_budget,
                    "routing": plan.routing_budget,
                    "execution": plan.execution_budget,
                },
            }

    def statistics(self) -> Dict[str, Any]:
        return {
            "layer1_compression": self.compression.statistics(),
            "layer2_routing": self.routing.statistics(),
            "layer3_execution": self.execution.statistics(),
            "budget": self.budget.statistics(),
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P18-8 Context Management Engine",
        "benchmark": "2026 Three-Layer Lean Context Architecture",
        "classes": 5,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "L1 Compression(Score/Chunk/Classify) → L2 Routing(Intent/Storage/Fusion) → L3 Execution(KV Reuse/Speculative) + Dynamic Budget",
        "key_metric": "Three-layer lean context with KV reuse hit-rate optimization",
        "thread_safe": True,
    }
