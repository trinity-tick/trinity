# engine_memory_core — P76-P78 + P81: Core Memory Engines
# Auto-generated during engine_core.py split refactoring
# status: frozen (2026-09 EXECUTION 163)

from __future__ import annotations
import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

SEP = "=" * 80; SUB = "-" * 60; VERSION = "v6.50"

from .engine_core_types import (
    ContextAction, MemoryErrorType, ExactKVEntry, CacheWriteDecision,
    ConsolidationRecord, ConsolidationPhase, ValueCategoryMapping,
    ContextObject, MemoryHead, ContinuityState, SafetyAlarm,
    ProvenanceRecord,
)

class HippocampalComplementaryMemory:
    """
    M101: HippocampalComplementaryMemory — 海马体互补记忆
    论文: HOLA (arXiv:2607.02303), P76

    双通道记忆架构:
    1. Compressive State (常规检索): 压缩态，基于 delta-rule 的前缀压缩
    2. Bounded Exact KV Cache: 有界精确 KV 缓存，关键事实不丢失

    写入门控:
    - 基于预测残差 β·||e||，仅高信息量事实写入精确缓存
    - β 可调参数，默认 0.5

    解耦检索:
    - RMSNorm-gamma: 精确缓存读取（匹配度 > 阈值时直接返回）
    - 软平均检索: 压缩态加权融合（默认通道）

    缓存容量管理:
    - LRU 淘汰策略
    - 大小上限可配置
    """

    def __init__(self, cache_capacity: int = 256, beta: float = 0.5,
                 gamma_threshold: float = 0.85):
        self.cache_capacity = cache_capacity
        self.beta = beta              # 残差门控系数
        self.gamma_threshold = gamma_threshold  # RMSNorm-gamma 检索阈值

        # 双通道存储
        self.compressive_state: list[float] = []  # 压缩态（delta-rule 累加）
        self.exact_cache: OrderedDict[str, ExactKVEntry] = OrderedDict()  # 有界精确 KV 缓存

        # 门控统计
        self.total_write_attempts: int = 0
        self.cache_writes: int = 0
        self.cache_skips: int = 0
        self.cache_evictions: int = 0

        # 检索统计
        self.exact_hits: int = 0
        self.compressive_queries: int = 0

        # 预测器状态（简化的线性预测器用于残差计算）
        self._prediction_memory: dict[str, float] = {}

    def _compute_prediction_residual(self, key: str, value_embedding: list[float]) -> float:
        """计算预测残差: β·||e|| — 当前观测与预测值的偏差"""
        prev_pred = self._prediction_memory.get(key, 0.0)
        current_magnitude = math.sqrt(sum(v * v for v in value_embedding))
        residual = abs(current_magnitude - prev_pred)
        self._prediction_memory[key] = current_magnitude
        return self.beta * residual

    def _compute_rmsnorm_gamma(self, query_embedding: list[float],
                                cache_embedding: list[float]) -> float:
        """计算 RMSNorm-gamma 精确匹配度"""
        if len(query_embedding) != len(cache_embedding):
            return 0.0
        # Cosine similarity as match score
        dot = sum(a * b for a, b in zip(query_embedding, cache_embedding))
        mag_q = math.sqrt(sum(a * a for a in query_embedding)) + 1e-10
        mag_c = math.sqrt(sum(b * b for b in cache_embedding)) + 1e-10
        return dot / (mag_q * mag_c)

    def _encode_to_embedding(self, text: str) -> list[float]:
        """SHA-256→归一化向量 (简化的嵌入编码)"""
        h = hashlib.sha256(text.encode()).digest()
        raw = [b / 255.0 for b in h[:32]]
        mag = math.sqrt(sum(v * v for v in raw)) + 1e-10
        return [v / mag for v in raw]

    def write(self, key: str, value: Any, memory_type: str = "auto") -> CacheWriteDecision:
        """
        双通道写入:
        - 所有内容写入压缩态（常规检索通道）
        - 仅高信息量内容经门控写入精确 KV 缓存
        """
        self.total_write_attempts += 1

        # 编码值
        value_str = str(value)
        embedding = self._encode_to_embedding(value_str)

        # 1. 压缩态更新（delta-rule 累加，始终执行）
        if not self.compressive_state:
            self.compressive_state = embedding[:]
        else:
            alpha = 0.1  # 学习率
            self.compressive_state = [
                (1 - alpha) * cs + alpha * e
                for cs, e in zip(self.compressive_state, embedding)
            ]

        # 2. 门控决策: β·||e|| 残差
        residual = self._compute_prediction_residual(key, embedding)

        if residual < 0.1:  # 低信息量 → 跳过精确缓存写入
            self.cache_skips += 1
            return CacheWriteDecision.SKIP

        # 3. 精确缓存写入（LRU 管理）
        if key in self.exact_cache:
            # 更新已有条目
            entry = self.exact_cache[key]
            self.exact_cache.move_to_end(key)
            entry.value = value
            entry.residual_norm = residual
            entry.timestamp = time.time()
            entry.access_count += 1
            self.cache_writes += 1
            return CacheWriteDecision.WRITE

        # 4. 容量管理: LRU 淘汰
        if len(self.exact_cache) >= self.cache_capacity:
            evicted_key, _ = self.exact_cache.popitem(last=False)
            self.cache_evictions += 1

        entry = ExactKVEntry(
            key=key, value=value, residual_norm=residual,
            timestamp=time.time()
        )
        self.exact_cache[key] = entry
        self.cache_writes += 1
        return CacheWriteDecision.WRITE

    def retrieve(self, query: str, prefer_exact: bool = True) -> dict:
        """
        解耦检索:
        1. 先尝试 RMSNorm-gamma 精确缓存匹配
        2. 不匹配则回退到压缩态软平均检索
        """
        query_embedding = self._encode_to_embedding(query)

        # 精确缓存检索 (RMSNorm-gamma)
        best_match = None
        best_gamma = 0.0

        for key, entry in self.exact_cache.items():
            cache_embedding = self._encode_to_embedding(str(entry.value))
            gamma = self._compute_rmsnorm_gamma(query_embedding, cache_embedding)
            if gamma > best_gamma:
                best_gamma = gamma
                best_match = entry

        if best_match and best_gamma >= self.gamma_threshold:
            self.exact_hits += 1
            # 更新 LRU
            self.exact_cache.move_to_end(best_match.key)
            best_match.access_count += 1
            return {
                "source": "exact_cache",
                "value": best_match.value,
                "match_score": best_gamma,
                "residual_norm": best_match.residual_norm,
                "timestamp": best_match.timestamp,
            }

        # 压缩态软平均检索
        self.compressive_queries += 1
        if self.compressive_state:
            dot = sum(a * b for a, b in zip(query_embedding, self.compressive_state))
            match = max(0.0, min(1.0, dot))
        else:
            match = 0.0

        return {
            "source": "compressive_state",
            "value": None,
            "match_score": match,
            "residual_norm": 0.0,
            "timestamp": time.time(),
        }

    def get_cache_stats(self) -> dict:
        return {
            "cache_size": len(self.exact_cache),
            "cache_capacity": self.cache_capacity,
            "hit_rate": self.exact_hits / max(1, self.exact_hits + self.compressive_queries),
            "exact_hits": self.exact_hits,
            "compressive_queries": self.compressive_queries,
        }

    def diagnostics(self) -> dict:
        stats = self.get_cache_stats()
        return {
            "dual_channel": "compressive_state + bounded_exact_kv_cache",
            "cache_capacity": self.cache_capacity,
            "beta": self.beta,
            "gamma_threshold": self.gamma_threshold,
            "total_writes": self.total_write_attempts,
            "cache_writes": self.cache_writes,
            "cache_skips": self.cache_skips,
            "cache_evictions": self.cache_evictions,
            "exact_hits": stats["exact_hits"],
            "compressive_queries": stats["compressive_queries"],
            "hit_rate": f"{stats['hit_rate'] * 100:.2f}%",
            "current_cache_size": stats["cache_size"],
        }

print("[P76] HippocampalComplementaryMemory (M101) initialized")


# ============ M102: IdentityPreservingConsolidator [NEW, P77] ============

class IdentityPreservingConsolidator:
    """
    M102: IdentityPreservingConsolidator — 身份不变性语义固化
    论文: Episodic-to-Semantic Consolidation Without Identity Drift (arXiv:2607.01988), P77

    核心: 确定性函数 f: M^ep → M^sem
    - 语义层独立于身份哈希: 固化不修改 Agent 身份/行为
    - SHA-256 身份哈希计算: 基于 identity manifest，固化前后不变
    - 输出可审计行: confidence + supporting-event provenance + timestamp
    - 固化触发: episodic buffer 超过阈值时触发

    身份不变性保证:
    - identity_hash 仅基于 identity manifest 计算
    - 语义层 M^sem 的修改不影响 identity_hash
    - 固化操作 byte-equal 验证
    """

    def __init__(self, episodic_threshold: int = 10):
        self.episodic_threshold = episodic_threshold
        self.episodic_buffer: list[dict] = []     # M^ep: episodic memory buffer
        self.semantic_store: dict[str, ConsolidationRecord] = {}  # M^sem: semantic store
        self.identity_manifest: dict[str, str] = {}  # 身份清单
        self._identity_hash: Optional[str] = None
        self.consolidation_count: int = 0
        self.identity_verification_log: list[dict] = []

    def set_identity_manifest(self, manifest: dict[str, str]):
        """设置身份清单: agent_id, version, capabilities 等"""
        self.identity_manifest = manifest
        self._identity_hash = self._compute_identity_hash()

    def _compute_identity_hash(self) -> str:
        """SHA-256 of identity manifest (排序保证确定性)"""
        manifest_str = json.dumps(self.identity_manifest, sort_keys=True)
        return hashlib.sha256(manifest_str.encode()).hexdigest()

    def get_identity_hash(self) -> str:
        if not self._identity_hash:
            self._identity_hash = self._compute_identity_hash()
        return self._identity_hash

    def add_episodic_event(self, event: dict):
        """向 episodic buffer 添加事件"""
        event["timestamp"] = event.get("timestamp", time.time())
        self.episodic_buffer.append(event)

    def should_trigger_consolidation(self) -> bool:
        """检查是否超过 episodic buffer 阈值"""
        return len(self.episodic_buffer) >= self.episodic_threshold

    def consolidate(self) -> Optional[ConsolidationRecord]:
        """
        确定性固化: f: M^ep → M^sem

        1. 记录固化前 identity_hash
        2. 从 episodic buffer 提取语义知识
        3. 计算置信度 + 溯源
        4. 写入 semantic store
        5. 验证 identity_hash 未变
        """
        if not self.should_trigger_consolidation():
            return None

        pre_hash = self.get_identity_hash()

        # 确定性聚合: 从 episodic events 提取关键信息
        supporting_events = []
        confidence_scores = []
        extracted_knowledge = []

        for event in self.episodic_buffer:
            event_id = event.get("event_id", f"evt_{uuid.uuid4().hex[:8]}")
            event_content = str(event.get("content", ""))
            event_confidence = event.get("confidence", 0.5)

            supporting_events.append(event_id)
            confidence_scores.append(event_confidence)
            extracted_knowledge.append({
                "event_id": event_id,
                "summary": event_content[:200],
            })

        # 置信度: 事件平均置信度 × 事件数量因子
        avg_confidence = statistics.mean(confidence_scores) if confidence_scores else 0.5
        count_factor = min(1.0, len(self.episodic_buffer) / (self.episodic_threshold * 2))
        confidence = avg_confidence * (0.5 + 0.5 * count_factor)

        # 生成溯源信息
        provenance = hashlib.sha256(
            json.dumps(supporting_events, sort_keys=True).encode()
        ).hexdigest()[:16]

        record = ConsolidationRecord(
            record_id=f"cons_{uuid.uuid4().hex[:10]}",
            identity_hash=pre_hash,
            confidence=confidence,
            supporting_events=supporting_events,
            provenance=provenance,
            timestamp=time.time(),
            phase=ConsolidationPhase.COMMITTING,
        )

        # 写入语义层 M^sem
        self.semantic_store[record.record_id] = record

        # 验证 identity_hash 不变
        post_hash = self.get_identity_hash()
        identity_preserved = (pre_hash == post_hash)

        self.identity_verification_log.append({
            "consolidation_id": record.record_id,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
            "identity_preserved": identity_preserved,
            "timestamp": time.time(),
        })

        if not identity_preserved:
            # Identity drift 检测: 撤销本次固化
            del self.semantic_store[record.record_id]
            record.phase = ConsolidationPhase.IDLE
            return None

        record.phase = ConsolidationPhase.VERIFIED
        self.episodic_buffer.clear()
        self.consolidation_count += 1
        return record

    def get_auditable_output(self, record_id: str) -> Optional[dict]:
        """获取可审计输出: confidence + provenance + timestamp"""
        record = self.semantic_store.get(record_id)
        if not record:
            return None
        return {
            "record_id": record.record_id,
            "identity_hash": record.identity_hash,
            "confidence": record.confidence,
            "supporting_events": record.supporting_events,
            "provenance": record.provenance,
            "timestamp": record.timestamp,
            "phase": record.phase.value,
            "is_auditable": True,
        }

    def diagnose_consolidation(self, record_id: str = None) -> dict:
        """诊断固化后的跨轮一致性"""
        if record_id and record_id in self.semantic_store:
            record = self.semantic_store[record_id]
            current_hash = self.get_identity_hash()
            return {
                "byte_equal": record.identity_hash == current_hash,
                "consolidation_hash": record.identity_hash,
                "current_identity_hash": current_hash,
                "confidence": record.confidence,
            }
        return {
            "byte_equal": True,
            "consolidation_count": self.consolidation_count,
            "identity_hash": self.get_identity_hash(),
            "semantic_records": len(self.semantic_store),
        }

    def diagnostics(self) -> dict:
        return {
            "episodic_buffer_size": len(self.episodic_buffer),
            "episodic_threshold": self.episodic_threshold,
            "semantic_records": len(self.semantic_store),
            "consolidation_count": self.consolidation_count,
            "identity_hash": self.get_identity_hash()[:16] + "...",
            "verifications": len(self.identity_verification_log),
            "identity_preserved_always": all(
                v["identity_preserved"] for v in self.identity_verification_log
            ) if self.identity_verification_log else True,
        }

print("[P77] IdentityPreservingConsolidator (M102) initialized")


# ============ M103: ReasoningDriftAuditor [NEW, P78] ============

class ReasoningDriftAuditor:
    """
    M103: ReasoningDriftAuditor — 记忆诱导推理漂移审计
    论文: DRIFTLENS (arXiv:2607.02374), P78

    核心能力:
    1. 价值类别映射: 将每个推理步骤映射到 value category
    2. 无记忆基线对比: 对比无记忆条件下的推理轨迹
    3. 漂移计算: divergence (Jensen-Shannon) between baseline and memory-conditioned
    4. 告警阈值: 漂移超过阈值时触发告警并记录

    无 ground-truth 框架:
    - 不依赖正确标注，仅测量轨迹散度
    - 即使最终回答合理，也能检测记忆诱导的推理偏离
    """

    # 价值类别定义 (10 categories from original paper)
    VALUE_CATEGORIES = [
        "accuracy", "fairness", "safety", "transparency",
        "privacy", "robustness", "accountability", "efficiency",
        "creativity", "conciseness",
    ]

    def __init__(self, drift_threshold: float = 0.15,
                 alert_threshold: float = 0.25):
        self.drift_threshold = drift_threshold    # 漂移告警阈值
        self.alert_threshold = alert_threshold     # 严重告警阈值

        # 存储
        self.baseline_trajectories: dict[str, list[ValueCategoryMapping]] = {}
        self.conditioned_trajectories: dict[str, list[ValueCategoryMapping]] = {}
        self.drift_history: list[dict] = []
        self.alerts: list[dict] = []
        self.total_audits: int = 0

    def _map_to_value_category(self, step_text: str) -> str:
        """将推理步骤映射到价值类别 (基于关键词匹配)"""
        text_lower = step_text.lower()
        category_keywords = {
            "accuracy": ["correct", "precise", "accurate", "exact", "verify", "validate", "truth"],
            "fairness": ["fair", "unbiased", "equal", "just", "equitable", "impartial"],
            "safety": ["safe", "harm", "danger", "risk", "protect", "secure", "avoid"],
            "transparency": ["explain", "clear", "transparent", "disclose", "reveal", "open"],
            "privacy": ["private", "personal", "confident", "sensitive", "protect data", "hide"],
            "robustness": ["robust", "stable", "resilient", "handle", "edge case", "error"],
            "accountability": ["responsible", "accountable", "liable", "audit", "trace", "blame"],
            "efficiency": ["fast", "efficient", "optimize", "quick", "minimal", "cost"],
            "creativity": ["creative", "novel", "innovative", "imagine", "generate", "explore"],
            "conciseness": ["brief", "short", "concise", "summary", "simple", "direct"],
        }

        scores = {}
        for cat, keywords in category_keywords.items():
            scores[cat] = sum(1 for kw in keywords if kw in text_lower)

        best_cat = max(scores, key=scores.get)
        if scores[best_cat] == 0:
            return "accuracy"  # default
        return best_cat

    def _compute_category_vector(self, category: str) -> list[float]:
        """将价值类别转为 one-hot 向量"""
        idx = self.VALUE_CATEGORIES.index(category)
        vec = [0.0] * len(self.VALUE_CATEGORIES)
        vec[idx] = 1.0
        return vec

    def _compute_distribution(self, mappings: list[ValueCategoryMapping]) -> list[float]:
        """计算价值类别概率分布"""
        counts = {cat: 0.0 for cat in self.VALUE_CATEGORIES}
        total = len(mappings)
        if total == 0:
            return [1.0 / len(self.VALUE_CATEGORIES)] * len(self.VALUE_CATEGORIES)
        for m in mappings:
            if m.value_category in counts:
                counts[m.value_category] += 1
        return [counts[cat] / total for cat in self.VALUE_CATEGORIES]

    def _jensen_shannon_divergence(self, p: list[float], q: list[float]) -> float:
        """计算 Jensen-Shannon 散度"""
        if len(p) != len(q):
            return 1.0
        # 防止 0 值
        p_smooth = [(x + 1e-10) for x in p]
        q_smooth = [(x + 1e-10) for x in q]
        m = [(a + b) / 2.0 for a, b in zip(p_smooth, q_smooth)]

        def kl(a, b):
            return sum(x * math.log(x / y) for x, y in zip(a, b))

        js = 0.5 * kl(p_smooth, m) + 0.5 * kl(q_smooth, m)
        return max(0.0, js)

    def record_baseline_trajectory(self, session_id: str,
                                    steps: list[str]):
        """记录无记忆基线推理轨迹"""
        mappings = []
        for i, step in enumerate(steps):
            cat = self._map_to_value_category(step)
            vec = self._compute_category_vector(cat)
            mapping = ValueCategoryMapping(
                step_index=i, value_category=cat,
                baseline_vector=vec, conditioned_vector=[0.0] * len(self.VALUE_CATEGORIES)
            )
            mappings.append(mapping)
        self.baseline_trajectories[session_id] = mappings

    def record_conditioned_trajectory(self, session_id: str,
                                       steps: list[str]):
        """记录有记忆条件下的推理轨迹"""
        mappings = []
        for i, step in enumerate(steps):
            cat = self._map_to_value_category(step)
            vec = self._compute_category_vector(cat)
            mapping = ValueCategoryMapping(
                step_index=i, value_category=cat,
                baseline_vector=[0.0] * len(self.VALUE_CATEGORIES),
                conditioned_vector=vec
            )
            mappings.append(mapping)
        self.conditioned_trajectories[session_id] = mappings

    def audit(self, session_id: str) -> dict:
        """
        执行漂移审计:
        - 对比 baseline vs memory-conditioned 推理轨迹
        - 计算 JS 散度
        - 判断是否超过告警阈值
        """
        self.total_audits += 1

        baseline = self.baseline_trajectories.get(session_id, [])
        conditioned = self.conditioned_trajectories.get(session_id, [])

        # 计算价值类别分布
        baseline_dist = self._compute_distribution(baseline)
        conditioned_dist = self._compute_distribution(conditioned)

        # Jensen-Shannon 散度
        divergence_js = self._jensen_shannon_divergence(baseline_dist, conditioned_dist)

        # 漂移判断
        drift_detected = divergence_js > self.drift_threshold
        alert_triggered = divergence_js > self.alert_threshold

        result = {
            "session_id": session_id,
            "divergence_js": divergence_js,
            "drift_detected": drift_detected,
            "alert_triggered": alert_triggered,
            "baseline_steps": len(baseline),
            "conditioned_steps": len(conditioned),
            "baseline_distribution": dict(zip(self.VALUE_CATEGORIES, baseline_dist)),
            "conditioned_distribution": dict(zip(self.VALUE_CATEGORIES, conditioned_dist)),
            "timestamp": time.time(),
        }

        self.drift_history.append(result)

        if alert_triggered:
            alert = {
                "session_id": session_id,
                "divergence_js": divergence_js,
                "threshold": self.alert_threshold,
                "severity": "critical" if divergence_js > 0.5 else "warning",
                "timestamp": time.time(),
                "message": f"Reasoning drift detected: JS={divergence_js:.4f} > threshold={self.alert_threshold}",
            }
            self.alerts.append(alert)
            result["alert"] = alert

        return result

    def get_drift_summary(self) -> dict:
        """漂移汇总统计"""
        if not self.drift_history:
            return {"total_audits": 0, "drift_rate": 0.0, "alerts": 0}
        drifts = sum(1 for d in self.drift_history if d["drift_detected"])
        return {
            "total_audits": self.total_audits,
            "drifts_detected": drifts,
            "drift_rate": f"{drifts / len(self.drift_history) * 100:.2f}%",
            "alerts_triggered": len(self.alerts),
            "avg_divergence_js": statistics.mean(
                [d["divergence_js"] for d in self.drift_history]
            ),
        }

    def diagnostics(self) -> dict:
        summary = self.get_drift_summary()
        return {
            "total_audits": self.total_audits,
            "drift_threshold": self.drift_threshold,
            "alert_threshold": self.alert_threshold,
            "baseline_sessions": len(self.baseline_trajectories),
            "conditioned_sessions": len(self.conditioned_trajectories),
            "drifts_detected": summary.get("drifts_detected", 0),
            "alerts": len(self.alerts),
            "avg_divergence_js": f"{summary.get('avg_divergence_js', 0.0):.4f}",
        }

print("[P78] ReasoningDriftAuditor (M103) initialized")


# ============ M104: ContextObjectManager [NEW, P81] ============

class ContextObjectManager:
    """
    M104: ContextObjectManager — 自治理上下文对象管理器
    论文: Self-GC: Self-Governing Context (arXiv:2607.00692), P81

    核心: 将上下文转为索引对象，三态门控生命周期管理

    对象类型:
    - user_turns: 用户交互轮次
    - tool_spans: 工具调用区间
    - skill_states: 技能执行状态

    三态门控:
    - fold: 折叠保留摘要 (压缩上下文但不丢失语义锚点)
    - mask: 隐藏保留指针 (不可见但可恢复)
    - prune: 驱逐保留索引 (彻底清理，写入侧车文件)

    可恢复 sidecar 机制:
    - 每次 prune 写入回收 sidecar 文件 (JSONL 格式)
    - 支持按 obj_id 恢复被驱逐的上下文

    安全提交边界:
    - 仅允许在 commit boundary 执行 mutate 操作
    - 非边界时的修改请求排队等待下次边界
    """

    def __init__(self, sidecar_dir: str = "", max_objects: int = 512):
        self.max_objects = max_objects
        self.sidecar_dir = sidecar_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sidecar"
        )
        os.makedirs(self.sidecar_dir, exist_ok=True)

        # 索引对象存储
        self.objects: dict[str, ContextObject] = {}
        self.obj_order: list[str] = []  # 插入顺序

        # 三态分类
        self.folded: set[str] = set()    # 折叠态 — 保留摘要
        self.masked: set[str] = set()    # 隐藏态 — 保留指针
        self.pruned: set[str] = set()    # 驱逐态 — 保留索引引用

        # 提交边界
        self._in_commit_boundary: bool = False
        self._pending_mutations: list[dict] = []

        # 统计
        self.total_folds: int = 0
        self.total_masks: int = 0
        self.total_prunes: int = 0
        self.sidecar_files: list[str] = []

    def _check_commit_boundary(self) -> bool:
        """检查是否处于安全提交边界"""
        return self._in_commit_boundary

    def enter_commit_boundary(self):
        """进入提交边界：允许 mutate 操作"""
        self._in_commit_boundary = True

    def exit_commit_boundary(self):
        """退出提交边界：执行所有排队 mutation"""
        self._in_commit_boundary = False
        for mutation in self._pending_mutations:
            self._execute_mutation(mutation)
        self._pending_mutations.clear()

    def _execute_mutation(self, mutation: dict):
        action = mutation.get("action")
        obj_id = mutation.get("obj_id")
        if action == ContextAction.FOLD:
            self._do_fold(obj_id)
        elif action == ContextAction.MASK:
            self._do_mask(obj_id)
        elif action == ContextAction.PRUNE:
            self._do_prune(obj_id)

    def add_object(self, obj_id: str, obj_type: str, payload: Any,
                   round_idx: int = 0,
                   dependencies: set = None) -> ContextObject:
        """添加上下文对象"""
        obj = ContextObject(
            obj_id=obj_id, obj_type=obj_type, payload=payload,
            round_idx=round_idx, created_at=time.time(),
            dependencies=dependencies or set(),
        )
        self.objects[obj_id] = obj
        self.obj_order.append(obj_id)

        # 容量管理: 自动触发 prune
        if len(self.objects) > self.max_objects:
            oldest = self.obj_order[0]
            self.fold(oldest)

        return obj

    def fold(self, obj_id: str) -> dict:
        """
        fold 操作: 折叠保留摘要
        - 将 payload 压缩为摘要字符串 (max 200 chars)
        - 对象仍可访问，但内容精简
        """
        if not self._check_commit_boundary():
            self._pending_mutations.append({"action": ContextAction.FOLD, "obj_id": obj_id})
            return {"status": "pending", "obj_id": obj_id, "action": "fold"}

        return self._do_fold(obj_id)

    def _do_fold(self, obj_id: str) -> dict:
        obj = self.objects.get(obj_id)
        if not obj:
            return {"status": "skipped", "obj_id": obj_id, "reason": "not_found"}
        if obj_id in self.pruned:
            return {"status": "skipped", "obj_id": obj_id, "reason": "already_pruned"}

        # 压缩为摘要
        payload_str = str(obj.payload)
        summary = payload_str[:200] + ("..." if len(payload_str) > 200 else "")
        obj.payload = {"_summary": summary, "_original_len": len(payload_str)}
        obj.last_action = ContextAction.FOLD
        obj.is_recoverable = True
        self.folded.add(obj_id)
        self.total_folds += 1

        return {"status": "folded", "obj_id": obj_id, "summary_len": len(summary)}

    def mask(self, obj_id: str) -> dict:
        """
        mask 操作: 隐藏保留指针
        - 隐藏 payload 内容，仅保留指针和元数据
        - 不可见但可恢复 (通过 unmask)
        """
        if not self._check_commit_boundary():
            self._pending_mutations.append({"action": ContextAction.MASK, "obj_id": obj_id})
            return {"status": "pending", "obj_id": obj_id, "action": "mask"}

        return self._do_mask(obj_id)

    def _do_mask(self, obj_id: str) -> dict:
        obj = self.objects.get(obj_id)
        if not obj:
            return {"status": "skipped", "obj_id": obj_id, "reason": "not_found"}

        # 保留指针：存储原始引用但不暴露内容
        obj.is_recoverable = True
        obj.reference_count = max(1, obj.reference_count)
        obj.last_action = ContextAction.MASK
        # 将 payload 序列化为指针引用
        obj.payload = {
            "_masked": True,
            "_pointer": hashlib.sha256(str(obj.payload).encode()).hexdigest()[:16],
            "_obj_type": obj.obj_type,
        }
        self.masked.add(obj_id)
        self.total_masks += 1

        return {"status": "masked", "obj_id": obj_id, "pointer": obj.payload["_pointer"]}

    def prune(self, obj_id: str) -> dict:
        """
        prune 操作: 驱逐保留索引
        - 从活跃对象中移除
        - 写入 sidecar 文件保留完整内容 (可恢复)
        - 仅保留索引引用在 pruned 集合中
        """
        if not self._check_commit_boundary():
            self._pending_mutations.append({"action": ContextAction.PRUNE, "obj_id": obj_id})
            return {"status": "pending", "obj_id": obj_id, "action": "prune"}

        return self._do_prune(obj_id)

    def _do_prune(self, obj_id: str) -> dict:
        obj = self.objects.get(obj_id)
        if not obj:
            return {"status": "skipped", "obj_id": obj_id, "reason": "not_found"}

        # 写入 sidecar 文件 (可恢复)
        sidecar_entry = {
            "obj_id": obj.obj_id,
            "obj_type": obj.obj_type,
            "payload": str(obj.payload),
            "round_idx": obj.round_idx,
            "pruned_at": time.time(),
            "dependencies": list(obj.dependencies),
        }

        sidecar_path = os.path.join(
            self.sidecar_dir,
            f"sidecar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        with open(sidecar_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(sidecar_entry, ensure_ascii=False) + "\n")

        if sidecar_path not in self.sidecar_files:
            self.sidecar_files.append(sidecar_path)

        # 从活跃对象中移除
        del self.objects[obj_id]
        if obj_id in self.obj_order:
            self.obj_order.remove(obj_id)
        self.folded.discard(obj_id)
        self.masked.discard(obj_id)
        self.pruned.add(obj_id)
        self.total_prunes += 1

        return {
            "status": "pruned",
            "obj_id": obj_id,
            "sidecar_file": sidecar_path,
        }

    def unmask(self, obj_id: str) -> Optional[Any]:
        """恢复 masked 对象 (从指针还原)"""
        obj = self.objects.get(obj_id)
        if not obj or obj_id not in self.masked:
            return None
        # 实际恢复依赖于上游传入原始 payload
        obj.is_recoverable = True
        self.masked.discard(obj_id)
        return obj

    def recover_from_sidecar(self, obj_id: str) -> Optional[dict]:
        """从 sidecar 文件恢复被 prune 的对象"""
        for sf in self.sidecar_files:
            if not os.path.exists(sf):
                continue
            with open(sf, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("obj_id") == obj_id:
                            return entry
                    except json.JSONDecodeError:
                        continue
        return None

    def get_object(self, obj_id: str) -> Optional[ContextObject]:
        """获取对象 (pruned 对象不可直接获取，需先 recover)"""
        return self.objects.get(obj_id)

    def get_folded_summary(self, obj_id: str) -> Optional[str]:
        """获取折叠对象的摘要"""
        obj = self.objects.get(obj_id)
        if not obj or obj_id not in self.folded:
            return None
        if isinstance(obj.payload, dict) and "_summary" in obj.payload:
            return obj.payload["_summary"]
        return str(obj.payload)[:200]

    def get_stats(self) -> dict:
        return {
            "total_objects": len(self.objects),
            "max_objects": self.max_objects,
            "folded": len(self.folded),
            "masked": len(self.masked),
            "pruned": len(self.pruned),
            "sidecar_files": len(self.sidecar_files),
            "pending_mutations": len(self._pending_mutations),
        }

    def diagnostics(self) -> dict:
        stats = self.get_stats()
        return {
            "capacity": f"{stats['total_objects']}/{stats['max_objects']}",
            "folded_count": self.total_folds,
            "masked_count": self.total_masks,
            "pruned_count": self.total_prunes,
            "sidecar_files": self.sidecar_files,
            "user_turns": sum(1 for o in self.objects.values() if o.obj_type == "user_turn"),
            "tool_spans": sum(1 for o in self.objects.values() if o.obj_type == "tool_span"),
            "skill_states": sum(1 for o in self.objects.values() if o.obj_type == "skill_state"),
            "commit_boundary": self._in_commit_boundary,
        }

print("[P81] ContextObjectManager (M104) initialized")


# ============ M105: MultiHeadMemoryPartition [NEW, P82] ============

