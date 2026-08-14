"""
P22-3: Progressive Disclosure Pipeline — L0-L4 渐进式披露管线

对标论文: 腾讯 Agent Memory (Progressive Disclosure Architecture, 2026.08)
核心发现: 上下文窗口有限，不应一次性暴露全部记忆，而应 L0→L1→L2→L3 四层渐进披露；
        符号化短时记忆压缩 + Mermaid 任务地图 + 三级水位自动触发确保最优预算分配。
三元语: L0原始 → L1原子 → L2场景 → L3人格 → 符号化压缩 → Mermaid地图 → 三级水位

设计要点:
- RawLevelExtractor: L0 原始层，从底层存储拉取完整记忆记录
- AtomicLevelEncoder: L1 原子层，将原始记录编码为结构化原子事实
- SceneLevelAggregator: L2 场景层，按时间/主题/任务聚合原子为场景
- PersonalityLevelBuilder: L3 人格层，从长期场景中提炼用户画像与偏好
- SymbolicShortTermCompressor: 符号化短时记忆压缩，将对话轮次压缩为符号序列
- MermaidTaskMapGenerator: 生成 Markdown Mermaid 任务拓扑图
- ThreeLevelWatermarkTrigger: 低/中/高三级水位自动触发四层切换
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================


class DisclosureLevel(Enum):
    """渐进披露层级"""
    L0_RAW = 0             # 原始记忆：完整记录
    L1_ATOMIC = 1          # 原子事实：结构化单元
    L2_SCENE = 2           # 场景聚合：上下文簇
    L3_PERSONA = 3         # 人格画像：长期偏好
    L4_META = 4            # 元认知：自我反思


class WatermarkLevel(Enum):
    """水位级别"""
    LOW = "low"             # 低水位：正常操作，仅 L0→L1
    MEDIUM = "medium"       # 中水位：L1→L2 场景聚合
    HIGH = "high"           # 高水位：L2→L3 人格提炼 + L0 截断


class SceneType(Enum):
    """场景类型"""
    CONVERSATION = "conversation"
    TASK_EXECUTION = "task_execution"
    RESEARCH_SESSION = "research_session"
    CODE_REVIEW = "code_review"
    MEETING = "meeting"
    CASUAL = "casual"


class CompressionStrategy(Enum):
    """压缩策略"""
    SYMBOLIC = "symbolic"           # 符号化压缩
    ABSTRACTIVE = "abstractive"     # 摘要式压缩
    KEYWORD_EXTRACT = "keyword_extract"  # 关键词提取
    TEMPLATE_FILL = "template_fill"      # 模板填充


class MapNodeShape(Enum):
    """Mermaid 节点形状"""
    ROUNDED = "rounded"
    DIAMOND = "diamond"
    HEXAGON = "hexagon"
    PARALLELOGRAM = "parallelogram"


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class RawMemory:
    """L0 原始记忆记录"""
    record_id: str
    timestamp: float
    content: str
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_size_bytes: int = 0


@dataclass
class AtomicFact:
    """L1 原子事实"""
    fact_id: str
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    source_record_ids: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SceneAggregate:
    """L2 场景聚合"""
    scene_id: str
    scene_type: SceneType
    start_time: float
    end_time: float
    atomic_fact_ids: List[str] = field(default_factory=list)
    summary: str = ""
    participant_ids: List[str] = field(default_factory=list)
    task_id: Optional[str] = None


@dataclass
class PersonalityProfile:
    """L3 人格画像"""
    profile_id: str
    user_id: str
    preferences: Dict[str, float] = field(default_factory=dict)
    expertise_domains: List[str] = field(default_factory=list)
    interaction_style: str = ""
    common_patterns: List[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SymbolicToken:
    """符号化标记"""
    token_id: int
    symbol: str
    meaning: str
    frequency: int = 0


@dataclass
class MermaidNode:
    """Mermaid 图节点"""
    node_id: str
    label: str
    shape: MapNodeShape = MapNodeShape.ROUNDED
    level: DisclosureLevel = DisclosureLevel.L0_RAW
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WatermarkState:
    """水位状态"""
    level: WatermarkLevel
    current_usage_bytes: int
    budget_bytes: int
    usage_ratio: float
    trigger_l1: bool = False
    trigger_l2: bool = False
    trigger_l3: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class PipelineStats:
    """管线统计"""
    raw_records_processed: int = 0
    atomic_facts_generated: int = 0
    scenes_aggregated: int = 0
    profiles_updated: int = 0
    compressions_performed: int = 0
    watermarks_triggered: int = 0
    mermaid_maps_generated: int = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "raw": self.raw_records_processed,
            "atomic": self.atomic_facts_generated,
            "scenes": self.scenes_aggregated,
            "profiles": self.profiles_updated,
            "compressions": self.compressions_performed,
            "watermarks": self.watermarks_triggered,
            "maps": self.mermaid_maps_generated,
        }


# ============================================================================
# Core Classes
# ============================================================================


class RawLevelExtractor:
    """L0 原始层提取器

    从底层存储拉取完整记忆记录，按时间窗口/来源过滤，
    支持批量拉取和游标分页。
    """

    def __init__(self, max_batch_size: int = 100) -> None:
        self._max_batch = max_batch_size
        self._lock = threading.RLock()
        self._records: Dict[str, RawMemory] = {}
        self._processed = 0

    def ingest(self, record: RawMemory) -> None:
        """摄入原始记录"""
        record.raw_size_bytes = len(record.content.encode("utf-8"))
        with self._lock:
            self._records[record.record_id] = record
            self._processed += 1

    def pull(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 50,
    ) -> List[RawMemory]:
        """时间窗口拉取"""
        with self._lock:
            records = list(self._records.values())
        if start_time is not None:
            records = [r for r in records if r.timestamp >= start_time]
        if end_time is not None:
            records = [r for r in records if r.timestamp <= end_time]
        records.sort(key=lambda r: r.timestamp)
        return records[:limit]

    def total_size_bytes(self) -> int:
        with self._lock:
            return sum(r.raw_size_bytes for r in self._records.values())

    @property
    def record_count(self) -> int:
        return len(self._records)


class AtomicLevelEncoder:
    """L1 原子层编码器

    将原始记忆记录分解为结构化 (subject, predicate, object) 三元组，
    每原子事实关联源记录 ID 用于溯源。
    """

    def __init__(self, confidence_threshold: float = 0.5) -> None:
        self._threshold = confidence_threshold
        self._lock = threading.RLock()
        self._facts: Dict[str, AtomicFact] = {}
        self._counter = 0

    def encode(self, record: RawMemory) -> List[AtomicFact]:
        """将原始记录编码为原子事实列表"""
        facts: List[AtomicFact] = []
        # 基于内容的结构化拆分（简化实现：按句分割）
        sentences = [s.strip() for s in record.content.replace("\n", ". ").split(".") if s.strip()]
        for sent in sentences[:10]:  # 限制每记录最多10个原子
            parts = sent.split(maxsplit=2)
            if len(parts) < 3:
                continue
            with self._lock:
                self._counter += 1
            fact = AtomicFact(
                fact_id=f"fact_{self._counter}",
                subject=parts[0],
                predicate=parts[1] if len(parts) > 1 else "relates_to",
                object=parts[2] if len(parts) > 2 else "",
                confidence=0.8,
                source_record_ids=[record.record_id],
                timestamp=record.timestamp,
            )
            if fact.confidence >= self._threshold:
                facts.append(fact)
                with self._lock:
                    self._facts[fact.fact_id] = fact
        return facts

    def get_facts_by_time(self, start: float, end: float) -> List[AtomicFact]:
        with self._lock:
            return [f for f in self._facts.values() if start <= f.timestamp <= end]

    @property
    def fact_count(self) -> int:
        return len(self._facts)


class SceneLevelAggregator:
    """L2 场景层聚合器

    按时序+主题聚类将原子事实聚合为场景，
    每个场景包含时间窗口、类型、参与者和任务上下文。
    """

    def __init__(self, window_seconds: float = 3600.0, max_facts_per_scene: int = 200) -> None:
        self._window = window_seconds
        self._max_facts = max_facts_per_scene
        self._lock = threading.RLock()
        self._scenes: Dict[str, SceneAggregate] = {}
        self._counter = 0

    def aggregate(self, facts: List[AtomicFact], scene_type: SceneType = SceneType.CONVERSATION) -> SceneAggregate:
        """聚合原子事实为场景"""
        if not facts:
            facts = []
        sorted_facts = sorted(facts, key=lambda f: f.timestamp)
        with self._lock:
            self._counter += 1
        scene = SceneAggregate(
            scene_id=f"scene_{self._counter}",
            scene_type=scene_type,
            start_time=sorted_facts[0].timestamp if sorted_facts else time.time(),
            end_time=sorted_facts[-1].timestamp if sorted_facts else time.time(),
            atomic_fact_ids=[f.fact_id for f in sorted_facts[:self._max_facts]],
            summary=f"Scene with {len(sorted_facts[:self._max_facts])} atomic facts",
        )
        with self._lock:
            self._scenes[scene.scene_id] = scene
        return scene

    def get_recent_scenes(self, n: int = 10) -> List[SceneAggregate]:
        with self._lock:
            scenes = sorted(self._scenes.values(), key=lambda s: s.end_time, reverse=True)
            return scenes[:n]

    @property
    def scene_count(self) -> int:
        return len(self._scenes)


class PersonalityLevelBuilder:
    """L3 人格画像构建器

    从长期场景历史中提炼用户画像：
    偏好分布、专业领域、交互风格、常见行为模式。
    """

    def __init__(self, min_scenes_for_profile: int = 10) -> None:
        self._min_scenes = min_scenes_for_profile
        self._lock = threading.RLock()
        self._profiles: Dict[str, PersonalityProfile] = {}

    def build(
        self,
        user_id: str,
        scenes: List[SceneAggregate],
        atomic_facts: List[AtomicFact],
    ) -> PersonalityProfile:
        """从场景和原子事实构建人格画像"""
        if len(scenes) < self._min_scenes:
            raise ValueError(f"Need at least {self._min_scenes} scenes, got {len(scenes)}")

        # 偏好统计
        preferences: Dict[str, float] = defaultdict(float)
        for fact in atomic_facts:
            key = f"{fact.predicate}_{fact.object}"[:32]
            preferences[key] += fact.confidence

        # 归一化
        total = sum(preferences.values()) or 1.0
        norm_prefs = {k: v / total for k, v in preferences.items()}

        # 专业领域（从高频谓词推断）
        predicate_counts: Dict[str, int] = defaultdict(int)
        for fact in atomic_facts:
            predicate_counts[fact.predicate] += 1
        domains = sorted(predicate_counts, key=predicate_counts.get, reverse=True)[:5]

        # 交互风格
        scene_types = [s.scene_type.value for s in scenes]
        dominant_type = max(set(scene_types), key=scene_types.count) if scene_types else "conversation"

        with self._lock:
            profile = PersonalityProfile(
                profile_id=f"profile_{user_id}",
                user_id=user_id,
                preferences=dict(sorted(norm_prefs.items(), key=lambda x: x[1], reverse=True)[:20]),
                expertise_domains=domains,
                interaction_style=dominant_type,
                common_patterns=[s.summary for s in scenes[-5:]],
            )
            self._profiles[user_id] = profile
        return profile

    def get_profile(self, user_id: str) -> Optional[PersonalityProfile]:
        with self._lock:
            return self._profiles.get(user_id)


class SymbolicShortTermCompressor:
    """符号化短时记忆压缩器

    将对话轮次压缩为符号序列，用有限符号集
    表示完整的对话状态，大幅减少 token 消耗。
    """

    def __init__(self, max_symbols: int = 64) -> None:
        self._max_symbols = max_symbols
        self._lock = threading.RLock()
        self._symbol_table: Dict[str, SymbolicToken] = {}
        self._compressions = 0

    def register_symbol(self, symbol: str, meaning: str) -> SymbolicToken:
        with self._lock:
            if symbol in self._symbol_table:
                self._symbol_table[symbol].frequency += 1
                return self._symbol_table[symbol]
            tid = len(self._symbol_table)
            token = SymbolicToken(token_id=tid, symbol=symbol, meaning=meaning, frequency=1)
            self._symbol_table[symbol] = token
        return token

    def compress(self, turns: List[str]) -> str:
        """压缩对话轮次为符号序列"""
        with self._lock:
            self._compressions += 1
        symbols: List[str] = []
        for turn in turns:
            # 简化：取前4字符作为符号键
            key = turn[:8].replace(" ", "_").lower()
            if key not in self._symbol_table:
                self.register_symbol(key, turn[:40])
            symbols.append(key)
        return "|".join(symbols[:self._max_symbols])

    def decompress(self, symbolic_sequence: str) -> List[str]:
        """解压缩符号序列为含义列表"""
        symbols = symbolic_sequence.split("|")
        result: List[str] = []
        for sym in symbols:
            token = self._symbol_table.get(sym)
            if token:
                result.append(token.meaning)
            else:
                result.append(f"[unknown:{sym}]")
        return result

    @property
    def compression_count(self) -> int:
        return self._compressions


class MermaidTaskMapGenerator:
    """Mermaid 任务地图生成器

    从场景/任务结构生成 Markdown Mermaid flowchart，
    直观展示任务拓扑与依赖关系。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generated = 0

    def generate(
        self,
        scenes: List[SceneAggregate],
        orientation: str = "TB",
    ) -> str:
        """生成 Mermaid flowchart 字符串"""
        lines: List[str] = [
            f"```mermaid",
            f"flowchart {orientation}",
        ]
        node_ids: Dict[str, str] = {}
        # 生成节点
        for i, scene in enumerate(scenes):
            nid = f"S{i}"
            node_ids[scene.scene_id] = nid
            shape_map = {
                SceneType.TASK_EXECUTION: "([",
                SceneType.MEETING: "{{",
                SceneType.CONVERSATION: "[",
                SceneType.RESEARCH_SESSION: "[[",
                SceneType.CODE_REVIEW: "[/",
                SceneType.CASUAL: "(",
            }
            open_b = shape_map.get(scene.scene_type, "[")
            close_b = open_b.replace("[", "]").replace("(", ")").replace("{", "}").replace("/", "\\")
            label = scene.summary[:30]
            lines.append(f"    {nid}{open_b}\"{label}\"{close_b}")

        # 生成时序边
        sorted_scenes = sorted(scenes, key=lambda s: s.start_time)
        for i in range(1, len(sorted_scenes)):
            prev = node_ids[sorted_scenes[i - 1].scene_id]
            curr = node_ids[sorted_scenes[i].scene_id]
            lines.append(f"    {prev} --> {curr}")

        lines.append("```")
        with self._lock:
            self._generated += 1
        return "\n".join(lines)

    @property
    def generated_count(self) -> int:
        return self._generated


class ThreeLevelWatermarkTrigger:
    """三级水位自动触发器

    监控上下文使用量，在低/中/高三级水位自动触发
    不同层级的渐进披露操作，确保最优预算分配。
    """

    def __init__(
        self,
        budget_bytes: int = 128_000,
        low_threshold: float = 0.30,
        medium_threshold: float = 0.60,
        high_threshold: float = 0.85,
    ) -> None:
        self._budget = budget_bytes
        self._low = low_threshold
        self._medium = medium_threshold
        self._high = high_threshold
        self._lock = threading.RLock()
        self._states: deque[WatermarkState] = deque(maxlen=100)
        self._triggers = 0

    def check(self, current_usage_bytes: int) -> WatermarkState:
        """检查当前水位并返回触发状态"""
        ratio = current_usage_bytes / max(self._budget, 1)
        if ratio >= self._high:
            level = WatermarkLevel.HIGH
        elif ratio >= self._medium:
            level = WatermarkLevel.MEDIUM
        elif ratio >= self._low:
            level = WatermarkLevel.LOW
        else:
            level = WatermarkLevel.LOW  # baseline

        state = WatermarkState(
            level=level,
            current_usage_bytes=current_usage_bytes,
            budget_bytes=self._budget,
            usage_ratio=ratio,
            trigger_l1=ratio >= self._low,
            trigger_l2=ratio >= self._medium,
            trigger_l3=ratio >= self._high,
        )
        with self._lock:
            self._states.append(state)
            if any([state.trigger_l1, state.trigger_l2, state.trigger_l3]):
                self._triggers += 1
        return state

    def adjust_budget(self, new_budget: int) -> None:
        with self._lock:
            self._budget = new_budget

    @property
    def trigger_count(self) -> int:
        return self._triggers


class ProgressiveDisclosurePipeline:
    """渐进式披露管线 — 顶层编排器

    组合 L0(原始)/L1(原子)/L2(场景)/L3(人格) 四层管线 +
    符号化压缩 + 任务地图 + 三级水位，实现全链路渐进披露。
    """

    def __init__(
        self,
        raw_extractor: Optional[RawLevelExtractor] = None,
        atomic_encoder: Optional[AtomicLevelEncoder] = None,
        scene_aggregator: Optional[SceneLevelAggregator] = None,
        persona_builder: Optional[PersonalityLevelBuilder] = None,
        compressor: Optional[SymbolicShortTermCompressor] = None,
        map_generator: Optional[MermaidTaskMapGenerator] = None,
        watermark_trigger: Optional[ThreeLevelWatermarkTrigger] = None,
    ) -> None:
        self.raw = raw_extractor or RawLevelExtractor()
        self.atomic = atomic_encoder or AtomicLevelEncoder()
        self.scene = scene_aggregator or SceneLevelAggregator()
        self.persona = persona_builder or PersonalityLevelBuilder()
        self.compressor = compressor or SymbolicShortTermCompressor()
        self.mapper = map_generator or MermaidTaskMapGenerator()
        self.watermark = watermark_trigger or ThreeLevelWatermarkTrigger()
        self._lock = threading.RLock()
        self._stats = PipelineStats()

    def process(self, record: RawMemory) -> Dict[str, Any]:
        """处理单条原始记录：L0 → L1 → 水位检查 → L2/L3"""
        # L0: 提取
        self.raw.ingest(record)
        self._stats.raw_records_processed += 1

        # L1: 原子化
        facts = self.atomic.encode(record)
        self._stats.atomic_facts_generated += len(facts)

        # 水位检查
        current_usage = self.raw.total_size_bytes()
        state = self.watermark.check(current_usage)
        if state.trigger_l1 or state.trigger_l2 or state.trigger_l3:
            self._stats.watermarks_triggered += 1

        # L2: 场景聚合（中水位以上触发）
        if state.trigger_l2:
            scene = self.scene.aggregate(facts)
            self._stats.scenes_aggregated += 1

        # L3: 人格更新（高水位触发）
        if state.trigger_l3:
            recent_scenes = self.scene.get_recent_scenes(20)
            all_facts = self.atomic.get_facts_by_time(0, time.time())
            if len(recent_scenes) >= self.persona._min_scenes:
                self.persona.build("default_user", recent_scenes, all_facts)
                self._stats.profiles_updated += 1

        return {
            "level": "L0",
            "record_id": record.record_id,
            "facts_generated": len(facts),
            "watermark": state.level.value,
        }

    def generate_mermaid_map(self) -> str:
        """生成当前场景的 Mermaid 任务地图"""
        scenes = self.scene.get_recent_scenes(30)
        result = self.mapper.generate(scenes)
        self._stats.mermaid_maps_generated += 1
        return result

    def compress_dialogue(self, turns: List[str]) -> Tuple[str, WatermarkState]:
        """符号化压缩对话"""
        compressed = self.compressor.compress(turns)
        self._stats.compressions_performed += 1
        usage = len(compressed.encode("utf-8"))
        state = self.watermark.check(usage)
        return compressed, state

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        return {
            "module": "Progressive_Disclosure",
            "raw_records": self.raw.record_count,
            "atomic_facts": self.atomic.fact_count,
            "scenes": self.scene.scene_count,
            "profiles": len(self.persona._profiles),
            "compressions": self.compressor.compression_count,
            "mermaid_maps": self.mapper.generated_count,
            "watermark_triggers": self.watermark.trigger_count,
            "stats": self._stats.summary(),
        }


# ============================================================================
# Module-level statistics
# ============================================================================


def statistics() -> Dict[str, Any]:
    """模块级运行时指标"""
    return {
        "module": "progressive_disclosure_pipeline",
        "class_count": 7,
        "disclosure_levels": [l.name for l in DisclosureLevel],
        "watermark_levels": [w.value for w in WatermarkLevel],
        "scene_types": [s.value for s in SceneType],
        "compression_strategies": [c.value for c in CompressionStrategy],
    }
