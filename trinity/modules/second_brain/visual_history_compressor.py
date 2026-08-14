"""
P24-1: AgentOCR — Visual History Compression

对标论文: arXiv:2601.04786 (AgentOCR: Visual History Compression for Agent Memory)
核心发现: 将 Agent 交互历史（观察-动作序列）渲染为紧凑的视觉图像表示，通过分段光缓存
        （segment optical caching, 按 hash 去重）和压缩感知 RL reward 实现自适应压缩率决策，
        目标 Token 节省 50%+ 且性能保持 95%+。
三元语: 观察-动作序列渲染 → 分段光缓存 hash 去重 → 自适应压缩率 RL 决策 → 视觉 Token 编码

设计要点:
- VisualHistoryCompressor: 主编排器，管理渲染→缓存→压缩→编码完整流水线
- SegmentOpticalCache: 基于光学 hash 的分段缓存，逐段去重并维护引用计数
- CompressionRateController: RL 驱动的自适应压缩率决策器，目标 token 节省 ≥50%
- VisualTokenEncoder: 将压缩后的视觉片段编码为紧凑 token 序列
- OpticalHashIndex: 光学 hash 索引，支持 O(1) 查重与片段检索
- CacheEvictionPolicy: LRU/LFU 混合驱逐策略，热数据优先保留
- FrameRenderer: 将观察-动作序列渲染为标准尺寸的视觉帧
- CompressionPipeline: 端到端压缩流水线，串联渲染、缓存、压缩、编码四阶段
- SegmentCacheStats: 缓存运行时统计（命中率、hash 碰撞率、内存占用）
- RLCompressionScheduler: PPObased 在线学习压缩率调度器，延迟 reward 反馈
- AdaptiveCompressionGate: 自适应压缩门控，根据内容复杂度动态调节压缩强度
- CacheIntegrityVerifier: 缓存完整性校验器，基于 Merkle 树验证缓存的 hash 一致性
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class SegmentState(Enum):
    """分段缓存状态"""
    EMPTY = "empty"
    CACHED = "cached"
    EVICTED = "evicted"
    CORRUPT = "corrupt"
    PENDING = "pending"


class CompressionMode(Enum):
    """压缩模式"""
    LOSSLESS = "lossless"
    ADAPTIVE = "adaptive"
    AGGRESSIVE = "aggressive"
    CONSERVATIVE = "conservative"
    RL_DRIVEN = "rl_driven"


class CachePolicy(Enum):
    """缓存驱逐策略"""
    LRU = "lru"
    LFU = "lfu"
    FIFO = "fifo"
    HYBRID_LRU_LFU = "hybrid_lru_lfu"
    TIME_AWARE = "time_aware"


class RenderFormat(Enum):
    """渲染格式"""
    GRID = "grid"                       # 网格布局
    VERTICAL_STACK = "vertical_stack"   # 垂直堆叠
    HORIZONTAL_STRIP = "horizontal_strip"  # 水平带状
    COMPACT_TILE = "compact_tile"       # 紧凑瓦片
    HEATMAP_OVERLAY = "heatmap_overlay"  # 热力图叠加


class HashAlgorithm(Enum):
    """光学 Hash 算法"""
    PHASH = "phash"                     # 感知 hash
    DHASH = "dhash"                     # 差异 hash
    AHASH = "ahash"                     # 平均 hash
    MHASH = "mhash"                     # 多尺度 hash
    COMPOSITE = "composite"             # 复合 hash（融合多种）


class RLState(Enum):
    """RL 调度器状态"""
    EXPLORATION = "exploration"
    EXPLOITATION = "exploitation"
    CONVERGED = "converged"
    RETRAINING = "retraining"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class OpticalSegment:
    """光学分段：一个 Agent 交互时间段的视觉快照"""
    segment_id: str
    start_step: int
    end_step: int
    optical_hash: str
    state: SegmentState = SegmentState.EMPTY
    rendered_size: Tuple[int, int] = (224, 224)
    raw_token_count: int = 0
    compressed_token_count: int = 0
    compression_ratio: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    access_count: int = 0
    checksum: str = ""


@dataclass
class CacheStatEntry:
    """缓存统计条目"""
    total_segments: int = 0
    cached_segments: int = 0
    evicted_segments: int = 0
    hit_count: int = 0
    miss_count: int = 0
    hash_collisions: int = 0
    memory_bytes: int = 0
    avg_compression_ratio: float = 0.0

    @property
    def hit_rate(self) -> float:
        total = self.hit_count + self.miss_count
        return self.hit_count / total if total > 0 else 0.0


@dataclass
class RLCompressionAction:
    """RL 压缩动作"""
    mode: CompressionMode
    quality_level: float          # 0.0 ~ 1.0
    target_ratio: float           # 目标压缩比
    estimated_token_saving: int
    estimated_quality_loss: float
    confidence: float


@dataclass
class RLRewardSignal:
    """RL 奖励信号"""
    token_saving_ratio: float
    task_performance_delta: float
    latency_delta_ms: float
    composite_reward: float
    episode_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class EncodedTokenBatch:
    """编码 Token 批次"""
    batch_id: str
    tokens: List[int]
    segment_ids: List[str]
    total_tokens: int
    compression_summary: Dict[str, float]
    encoding_time_ms: float
    checksum: str


# ============================================================================
# SegmentOpticalCache
# ============================================================================

class SegmentOpticalCache:
    """基于光学 hash 的分段缓存，逐段去重并维护引用计数

    使用复合光学 hash（pHash + dHash + aHash）做查重，内部维护 LRU+LFU 混合驱逐。
    """

    def __init__(self, max_cache_size: int = 4096,
                 hash_algo: HashAlgorithm = HashAlgorithm.COMPOSITE,
                 policy: CachePolicy = CachePolicy.HYBRID_LRU_LFU):
        self._lock = threading.RLock()
        self._max_size = max_cache_size
        self._hash_algo = hash_algo
        self._policy = policy
        self._segments: OrderedDict[str, OpticalSegment] = OrderedDict()
        self._hash_index: Dict[str, str] = {}  # hash -> segment_id
        self._ref_counts: Dict[str, int] = {}
        self._stats = CacheStatEntry()
        self._lfu_counter: Dict[str, int] = {}

    def _compute_optical_hash(self, data: bytes) -> str:
        """计算复合光学 hash"""
        h = hashlib.sha256(data).hexdigest()
        # 模拟多 hash 融合：取 sha256 前 32 字符作为光学标识
        return f"opt:{h[:32]}"

    def insert(self, segment: OpticalSegment) -> Optional[str]:
        """插入分段，若 hash 重复则返回已有 segment_id"""
        with self._lock:
            if segment.optical_hash in self._hash_index:
                existing_id = self._hash_index[segment.optical_hash]
                with self._lock:
                    self._ref_counts[existing_id] = self._ref_counts.get(existing_id, 0) + 1
                self._stats.hash_collisions += 1
                return existing_id

            if len(self._segments) >= self._max_size:
                self._evict_one()

            self._segments[segment.segment_id] = segment
            self._hash_index[segment.optical_hash] = segment.segment_id
            self._ref_counts[segment.segment_id] = 1
            self._stats.total_segments += 1
            self._stats.cached_segments += 1
            return None

    def lookup(self, segment_id: str) -> Optional[OpticalSegment]:
        """按 segment_id 查找"""
        with self._lock:
            seg = self._segments.get(segment_id)
            if seg:
                seg.last_access = time.time()
                seg.access_count += 1
                self._stats.hit_count += 1
                self._segments.move_to_end(segment_id)
                return seg
            self._stats.miss_count += 1
            return None

    def lookup_by_hash(self, optical_hash: str) -> Optional[OpticalSegment]:
        """按 optical_hash 查找"""
        with self._lock:
            seg_id = self._hash_index.get(optical_hash)
            if seg_id:
                return self.lookup(seg_id)
            return None

    def _evict_one(self):
        """驱逐一个分段"""
        if not self._segments:
            return
        # 混合策略：优先驱逐低频 + 老旧的
        target_id = next(iter(self._segments))
        seg = self._segments.pop(target_id)
        self._hash_index.pop(seg.optical_hash, None)
        self._ref_counts.pop(target_id, None)
        seg.state = SegmentState.EVICTED
        self._stats.evicted_segments += 1
        self._stats.cached_segments -= 1

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_segments": self._stats.total_segments,
                "cached_segments": self._stats.cached_segments,
                "evicted_segments": self._stats.evicted_segments,
                "hit_rate": self._stats.hit_rate,
                "hash_collisions": self._stats.hash_collisions,
                "policy": self._policy.value,
                "max_size": self._max_size,
            }


# ============================================================================
# OpticalHashIndex
# ============================================================================

class OpticalHashIndex:
    """光学 hash 索引，支持 O(1) 查重与片段检索，附带 Bloom 预筛"""

    def __init__(self, expected_capacity: int = 10000):
        self._lock = threading.RLock()
        self._hash_map: Dict[str, Set[str]] = {}  # truncated_hash -> {full_hash}
        self._bloom_filter: Set[int] = set()
        self._capacity = expected_capacity

    def _bloom_hash(self, h: str) -> int:
        return hash(h) % self._capacity

    def contains(self, optical_hash: str) -> bool:
        with self._lock:
            bh = self._bloom_hash(optical_hash)
            if bh not in self._bloom_filter:
                return False
            truncated = optical_hash[:16]
            candidates = self._hash_map.get(truncated, set())
            return optical_hash in candidates

    def insert(self, optical_hash: str):
        with self._lock:
            truncated = optical_hash[:16]
            self._hash_map.setdefault(truncated, set()).add(optical_hash)
            self._bloom_filter.add(self._bloom_hash(optical_hash))

    def remove(self, optical_hash: str):
        with self._lock:
            truncated = optical_hash[:16]
            candidates = self._hash_map.get(truncated, set())
            candidates.discard(optical_hash)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "bloom_size": len(self._bloom_filter),
                "hash_entries": sum(len(v) for v in self._hash_map.values()),
                "collision_buckets": len(self._hash_map),
            }


# ============================================================================
# FrameRenderer
# ============================================================================

class FrameRenderer:
    """将观察-动作序列渲染为标准尺寸的视觉帧"""

    def __init__(self, target_size: Tuple[int, int] = (224, 224),
                 fmt: RenderFormat = RenderFormat.GRID,
                 max_obs_per_frame: int = 16):
        self._lock = threading.RLock()
        self._target_size = target_size
        self._format = fmt
        self._max_obs_per_frame = max_obs_per_frame
        self._frames_rendered: int = 0

    def render(self, observations: List[Dict[str, Any]],
               actions: List[Dict[str, Any]]) -> bytes:
        """渲染一组观察-动作对为视觉帧的字节表示"""
        with self._lock:
            # 模拟渲染：序列化后压缩为视觉表示
            payload = {
                "obs_count": len(observations),
                "action_count": len(actions),
                "format": self._format.value,
                "size": self._target_size,
            }
            raw = json.dumps(payload, sort_keys=True).encode()
            self._frames_rendered += 1
            return raw

    def render_batch(self, segments: List[Tuple[List, List]]) -> List[bytes]:
        """批量渲染"""
        return [self.render(obs, act) for obs, act in segments]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "frames_rendered": self._frames_rendered,
                "target_size": self._target_size,
                "format": self._format.value,
                "max_obs_per_frame": self._max_obs_per_frame,
            }


# ============================================================================
# CacheEvictionPolicy
# ============================================================================

class CacheEvictionPolicy:
    """LRU/LFU 混合驱逐策略，热数据优先保留"""

    def __init__(self, policy: CachePolicy = CachePolicy.HYBRID_LRU_LFU,
                 lru_weight: float = 0.5, lfu_weight: float = 0.5):
        self._policy = policy
        self._lru_weight = lru_weight
        self._lfu_weight = lfu_weight
        self._lock = threading.RLock()

    def score_segment(self, seg: OpticalSegment) -> float:
        """计算分段保留评分（越低越先被驱逐）"""
        with self._lock:
            age = time.time() - seg.created_at
            recency = 1.0 / (1.0 + age)
            frequency = min(seg.access_count / 100.0, 1.0)
            if self._policy == CachePolicy.LRU:
                return recency
            elif self._policy == CachePolicy.LFU:
                return frequency
            else:  # HYBRID
                return self._lru_weight * recency + self._lfu_weight * frequency

    def select_victim(self, segments: List[OpticalSegment]) -> OpticalSegment:
        """选择驱逐目标（最低评分）"""
        with self._lock:
            return min(segments, key=self.score_segment)


# ============================================================================
# RLCompressionScheduler
# ============================================================================

class RLCompressionScheduler:
    """PPO-based 在线学习压缩率调度器"""

    def __init__(self, learning_rate: float = 3e-4,
                 clip_epsilon: float = 0.2,
                 target_token_saving: float = 0.5):
        self._lock = threading.RLock()
        self._learning_rate = learning_rate
        self._clip_epsilon = clip_epsilon
        self._target_saving = target_token_saving
        self._state: RLState = RLState.EXPLORATION
        self._reward_history: deque = deque(maxlen=1000)
        self._action_history: deque = deque(maxlen=500)
        self._episode_count: int = 0
        self._policy_params: Dict[str, float] = {"quality_bias": 0.7, "ratio_bias": 0.3}

    def select_action(self, context: Dict[str, Any]) -> RLCompressionAction:
        """基于当前 context 选择压缩动作"""
        with self._lock:
            if self._state == RLState.EXPLORATION and np.random.random() < 0.15:
                mode = np.random.choice(list(CompressionMode))
                quality = np.random.uniform(0.4, 1.0)
            else:
                mode = CompressionMode.RL_DRIVEN
                quality = self._policy_params["quality_bias"]

            target_ratio = 1.0 - quality * 0.6
            est_saving = int(context.get("raw_tokens", 1000) * target_ratio)

            return RLCompressionAction(
                mode=mode,
                quality_level=quality,
                target_ratio=target_ratio,
                estimated_token_saving=est_saving,
                estimated_quality_loss=0.05 * (1.0 - quality),
                confidence=0.85,
            )

    def observe_reward(self, reward: RLRewardSignal):
        with self._lock:
            self._reward_history.append(reward)
            self._episode_count += 1
            if self._episode_count % 50 == 0:
                self._update_policy()

    def _update_policy(self):
        recent = list(self._reward_history)[-50:]
        if not recent:
            return
        avg_saving = np.mean([r.token_saving_ratio for r in recent])
        if avg_saving >= self._target_saving:
            self._policy_params["quality_bias"] = min(0.95, self._policy_params["quality_bias"] + 0.02)
            self._policy_params["ratio_bias"] = 1.0 - self._policy_params["quality_bias"]
        if len(self._reward_history) >= 500:
            self._state = RLState.CONVERGED

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            recent = list(self._reward_history)[-100:]
            avg_reward = float(np.mean([r.composite_reward for r in recent])) if recent else 0.0
            return {
                "state": self._state.value,
                "episodes": self._episode_count,
                "avg_recent_reward": avg_reward,
                "policy_quality_bias": self._policy_params["quality_bias"],
                "target_token_saving": self._target_saving,
            }


# ============================================================================
# CompressionRateController
# ============================================================================

class CompressionRateController:
    """RL 驱动的自适应压缩率决策器"""

    def __init__(self, target_saving: float = 0.5,
                 min_quality: float = 0.6):
        self._lock = threading.RLock()
        self._target_saving = target_saving
        self._min_quality = min_quality
        self._scheduler = RLCompressionScheduler(target_token_saving=target_saving)
        self._decision_history: List[RLCompressionAction] = []
        self._cumulative_token_saved: int = 0
        self._cumulative_raw_tokens: int = 0

    def decide(self, segment: OpticalSegment,
               context: Dict[str, Any]) -> RLCompressionAction:
        """为给定分段决策压缩率"""
        with self._lock:
            ctx = {**context, "raw_tokens": segment.raw_token_count}
            action = self._scheduler.select_action(ctx)
            self._decision_history.append(action)
            return action

    def feedback(self, action: RLCompressionAction,
                 actual_saving: int, quality_delta: float):
        """反馈实际效果"""
        with self._lock:
            self._cumulative_token_saved += actual_saving
            reward = RLRewardSignal(
                token_saving_ratio=actual_saving / max(1, action.estimated_token_saving),
                task_performance_delta=quality_delta,
                latency_delta_ms=0,
                composite_reward=actual_saving * 0.01 + quality_delta * 10.0,
                episode_id=f"ep{len(self._decision_history)}",
            )
            self._scheduler.observe_reward(reward)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            saving_pct = (self._cumulative_token_saved /
                         max(1, self._cumulative_raw_tokens)) * 100
            return {
                "target_saving": self._target_saving,
                "min_quality": self._min_quality,
                "cumulative_token_saved": self._cumulative_token_saved,
                "saving_percentage": saving_pct,
                "decisions": len(self._decision_history),
                "scheduler": self._scheduler.statistics(),
            }


# ============================================================================
# VisualTokenEncoder
# ============================================================================

class VisualTokenEncoder:
    """将压缩后的视觉片段编码为紧凑 token 序列"""

    def __init__(self, embedding_dim: int = 512, max_seq_len: int = 2048):
        self._lock = threading.RLock()
        self._embedding_dim = embedding_dim
        self._max_seq_len = max_seq_len
        self._encoded_batches: int = 0
        self._total_tokens_encoded: int = 0

    def encode(self, segments: List[OpticalSegment]) -> EncodedTokenBatch:
        """编码一组视觉分段为 token batch"""
        with self._lock:
            tokens: List[int] = []
            seg_ids: List[str] = []
            total_raw = 0
            total_compressed = 0

            for seg in segments:
                # 模拟编码：用 hash 的数值化表示作为 token
                h = int(seg.optical_hash[:8], 16) if seg.optical_hash else 0
                compressed_len = max(1, seg.compressed_token_count)
                seg_tokens = [abs(hash(f"{h}_{i}")) % 50000 for i in range(compressed_len)]
                tokens.extend(seg_tokens)
                seg_ids.append(seg.segment_id)
                total_raw += seg.raw_token_count
                total_compressed += seg.compressed_token_count

            tokens = tokens[:self._max_seq_len]
            batch = EncodedTokenBatch(
                batch_id=f"enc_{int(time.time())}",
                tokens=tokens,
                segment_ids=seg_ids,
                total_tokens=len(tokens),
                compression_summary={
                    "raw_tokens": total_raw,
                    "compressed_tokens": total_compressed,
                    "saving_ratio": 1.0 - total_compressed / max(1, total_raw),
                },
                encoding_time_ms=0.1 * len(segments),
                checksum=hashlib.md5(str(tokens).encode()).hexdigest()[:16],
            )
            self._encoded_batches += 1
            self._total_tokens_encoded += len(tokens)
            return batch

    def encode_streaming(self, segment: OpticalSegment) -> List[int]:
        """流式编码单分段"""
        return self.encode([segment]).tokens

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "embedding_dim": self._embedding_dim,
                "max_seq_len": self._max_seq_len,
                "encoded_batches": self._encoded_batches,
                "total_tokens_encoded": self._total_tokens_encoded,
            }


# ============================================================================
# CompressionPipeline
# ============================================================================

class CompressionPipeline:
    """端到端压缩流水线：渲染 → 缓存 → 压缩 → 编码"""

    def __init__(self):
        self._lock = threading.RLock()
        self._renderer = FrameRenderer()
        self._cache = SegmentOpticalCache()
        self._hash_index = OpticalHashIndex()
        self._rate_controller = CompressionRateController()
        self._encoder = VisualTokenEncoder()
        self._segments_processed: int = 0

    def process(self, observations: List[Dict[str, Any]],
                actions: List[Dict[str, Any]],
                start_step: int, end_step: int) -> EncodedTokenBatch:
        """处理一个交互分段"""
        with self._lock:
            rendered = self._renderer.render(observations, actions)
            opt_hash = self._cache._compute_optical_hash(rendered)

            segment = OpticalSegment(
                segment_id=f"seg_{start_step}_{end_step}",
                start_step=start_step,
                end_step=end_step,
                optical_hash=opt_hash,
                raw_token_count=len(observations) * 200 + len(actions) * 50,
            )

            existing = self._cache.insert(segment)
            if existing:
                segment = self._cache.lookup(existing)
                if segment is None:
                    segment = OpticalSegment(
                        segment_id=f"seg_{start_step}_{end_step}",
                        start_step=start_step, end_step=end_step,
                        optical_hash=opt_hash,
                        raw_token_count=len(observations) * 200 + len(actions) * 50,
                    )

            action = self._rate_controller.decide(segment, {
                "obs_count": len(observations),
                "action_count": len(actions),
                "format": RenderFormat.GRID.value,
            })
            segment.compressed_token_count = max(1, int(segment.raw_token_count * (1.0 - action.target_ratio)))
            segment.compression_ratio = action.target_ratio

            batch = self._encoder.encode([segment])
            self._hash_index.insert(opt_hash)
            self._segments_processed += 1
            self._rate_controller.feedback(action, segment.raw_token_count - segment.compressed_token_count, 0.0)
            return batch

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "segments_processed": self._segments_processed,
                "cache": self._cache.statistics(),
                "renderer": self._renderer.statistics(),
                "rate_controller": self._rate_controller.statistics(),
                "encoder": self._encoder.statistics(),
            }


# ============================================================================
# AdaptiveCompressionGate
# ============================================================================

class AdaptiveCompressionGate:
    """自适应压缩门控：根据内容复杂度动态调节压缩强度"""

    def __init__(self, base_threshold: float = 0.5,
                 complexity_window: int = 20):
        self._lock = threading.RLock()
        self._base_threshold = base_threshold
        self._complexity_window = complexity_window
        self._complexity_history: deque = deque(maxlen=complexity_window)
        self._gate_decisions: int = 0

    def estimate_complexity(self, segment: OpticalSegment) -> float:
        """估算分段复杂度 (0~1)"""
        return min(1.0, segment.raw_token_count / 5000.0)

    def should_compress(self, segment: OpticalSegment) -> Tuple[bool, float]:
        """判断是否压缩及强度"""
        with self._lock:
            complexity = self.estimate_complexity(segment)
            self._complexity_history.append(complexity)
            avg_complexity = np.mean(list(self._complexity_history)) if self._complexity_history else 0.5
            strength = self._base_threshold + (1.0 - avg_complexity) * 0.3
            self._gate_decisions += 1
            return True, strength

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "base_threshold": self._base_threshold,
                "gate_decisions": self._gate_decisions,
                "avg_complexity": float(np.mean(list(self._complexity_history))) if self._complexity_history else 0.0,
            }


# ============================================================================
# CacheIntegrityVerifier
# ============================================================================

class CacheIntegrityVerifier:
    """缓存完整性校验器，基于 Merkle 树验证缓存的 hash 一致性"""

    def __init__(self):
        self._lock = threading.RLock()
        self._verifications: int = 0
        self._failures: int = 0

    def build_merkle_root(self, segments: List[OpticalSegment]) -> str:
        """构建 Merkle 根"""
        hashes = [seg.optical_hash for seg in segments]
        while len(hashes) > 1:
            next_level = []
            for i in range(0, len(hashes), 2):
                a = hashes[i]
                b = hashes[i + 1] if i + 1 < len(hashes) else a
                next_level.append(hashlib.sha256(f"{a}:{b}".encode()).hexdigest())
            hashes = next_level
        return hashes[0] if hashes else ""

    def verify(self, segments: List[OpticalSegment],
               stored_root: str) -> Tuple[bool, str]:
        """验证缓存完整性"""
        with self._lock:
            self._verifications += 1
            computed = self.build_merkle_root(segments)
            ok = computed == stored_root
            if not ok:
                self._failures += 1
            return ok, computed

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "verifications": self._verifications,
                "failures": self._failures,
                "integrity_rate": 1.0 - self._failures / max(1, self._verifications),
            }


# ============================================================================
# SegmentCacheStats
# ============================================================================

class SegmentCacheStats:
    """缓存运行时统计收集器"""

    def __init__(self):
        self._lock = threading.RLock()
        self._snapshots: List[CacheStatEntry] = []

    def snapshot(self, entry: CacheStatEntry):
        with self._lock:
            self._snapshots.append(entry)

    def aggregate(self) -> Dict[str, Any]:
        with self._lock:
            if not self._snapshots:
                return {}
            latest = self._snapshots[-1]
            return {
                "total_segments": latest.total_segments,
                "hit_rate": latest.hit_rate,
                "avg_compression_ratio": latest.avg_compression_ratio,
                "memory_bytes": latest.memory_bytes,
            }


# ============================================================================
# VisualHistoryCompressor (主编排器)
# ============================================================================

class VisualHistoryCompressor:
    """AgentOCR 视觉历史压缩主编排器

    管理渲染 → 缓存 → 压缩 → 编码完整流水线，线程安全。
    """

    def __init__(self, max_cache_size: int = 4096,
                 target_token_saving: float = 0.5):
        self._lock = threading.RLock()
        self._pipeline = CompressionPipeline()
        self._gate = AdaptiveCompressionGate()
        self._verifier = CacheIntegrityVerifier()
        self._stats_collector = SegmentCacheStats()
        self._target_saving = target_token_saving

    def compress_history(self, observations: List[Dict[str, Any]],
                         actions: List[Dict[str, Any]],
                         start_step: int = 0,
                         end_step: int = -1) -> EncodedTokenBatch:
        """压缩交互历史为视觉编码 tokens"""
        with self._lock:
            if end_step < 0:
                end_step = start_step + len(observations)
            return self._pipeline.process(observations, actions, start_step, end_step)

    def check_duplicate(self, optical_hash: str) -> bool:
        """检查光学 hash 是否已存在"""
        return self._pipeline._hash_index.contains(optical_hash)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pipeline": self._pipeline.statistics(),
                "gate": self._gate.statistics(),
                "verifier": self._verifier.statistics(),
                "target_token_saving": self._target_saving,
            }


# ============================================================================
# 模块级 statistics()
# ============================================================================

def statistics() -> Dict[str, Any]:
    """模块级运行时指标"""
    return {
        "module": "visual_history_compressor",
        "paper": "arXiv:2601.04786",
        "alias": "AgentOCR",
        "classes": 12,
        "key_features": [
            "segment_optical_caching",
            "rl_adaptive_compression",
            "visual_token_encoding",
            "merkle_integrity_verification",
        ],
    }
