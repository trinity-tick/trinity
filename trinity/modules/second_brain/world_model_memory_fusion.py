"""
P17-8: World Model Memory Fusion
=================================

对标世界模型+记忆闭环 — 预演→验证→差距学习持续进化。

设计要点：
  - 预演→验证→差距学习三级流水线：世界模型预测 → 真实环境比对 → 误差写入记忆
  - 环境理解偏差自动写入记忆：检测预测误差，提取 knowledge gap，修正世界模型
  - 持续修正→环境认知进化：增量学习，避免灾难性遗忘
  - 领域自适应：7 领域差异感知，不同领域独立的误差容忍度和学习率

核心组件：
  - WorldModelPredictor:     世界模型预演，基于当前记忆生成环境预测
  - RealityComparator:       预测 vs 真实自动对比，量化偏差
  - GapLearningPipeline:     差距学习流水线，误差→记忆修正→环境认知进化
  - DomainAdaptiveController: 7 领域差异感知自适应控制
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ============================================================================
# Enums
# ============================================================================

class DomainName(Enum):
    """7 领域枚举（与 language_world_model 保持一致）。"""
    MCP = "mcp"
    SEARCH = "search"
    TERMINAL = "terminal"
    SWE = "swe"
    WEB = "web"
    OS = "os"
    ANDROID = "android"


class FusionPhase(Enum):
    """融合阶段。"""
    PREDICT = "predict"        # 预演
    COMPARE = "compare"        # 验证对比
    LEARN = "learn"            # 差距学习
    UPDATE = "update"          # 记忆更新


class DeviationSeverity(Enum):
    """偏差严重度。"""
    NEGLIGIBLE = "negligible"  # 可忽略
    MINOR = "minor"            # 轻微
    MAJOR = "major"            # 重大
    CRITICAL = "critical"      # 关键（需立即修正）


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class Prediction:
    """单条世界模型预测。"""
    prediction_id: str
    domain: DomainName
    predicted_state: Dict[str, Any]
    confidence: float
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class RealitySnapshot:
    """真实环境快照。"""
    snapshot_id: str
    domain: DomainName
    actual_state: Dict[str, Any]
    source: str = "environment"
    timestamp: float = field(default_factory=time.time)


@dataclass
class DeviationReport:
    """偏差报告（预测 vs 真实对比）。"""
    report_id: str
    prediction_id: str
    domain: DomainName
    matched_fields: List[str] = field(default_factory=list)
    deviated_fields: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    overall_deviation_score: float = 0.0
    severity: DeviationSeverity = DeviationSeverity.MINOR
    knowledge_gaps: List[str] = field(default_factory=list)
    suggested_corrections: List[str] = field(default_factory=list)


@dataclass
class CorrectionRecord:
    """修正记录。"""
    record_id: str
    domain: DomainName
    deviation_report_id: str
    old_knowledge: str
    new_knowledge: str
    applied: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class DomainProfile:
    """领域自适应画像。"""
    domain: DomainName
    tolerance: float = 0.1            # 误差容忍度
    learning_rate: float = 0.01       # 渐进学习率
    correction_count: int = 0
    last_correction: float = 0.0
    stability_score: float = 1.0      # 稳定性（越高越稳定）


# ============================================================================
# Domain Defaults
# ============================================================================

DOMAIN_DEFAULTS: Dict[DomainName, Tuple[float, float]] = {
    DomainName.MCP: (0.05, 0.02),       # 低容忍，高学习率（工具输出需精确）
    DomainName.SEARCH: (0.15, 0.005),   # 高容忍（搜索结果波动大）
    DomainName.TERMINAL: (0.05, 0.01),  # 低容忍
    DomainName.SWE: (0.10, 0.01),
    DomainName.WEB: (0.20, 0.003),      # 高容忍（网页变化频繁）
    DomainName.OS: (0.10, 0.008),
    DomainName.ANDROID: (0.12, 0.005),
}


# ============================================================================
# Core Components
# ============================================================================

class WorldModelPredictor:
    """世界模型预演：基于当前记忆做出环境预测。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.predictions: List[Prediction] = []
        self.world_model: Dict[str, Any] = {}

    def predict(self, domain: DomainName, context: Dict[str, Any], confidence: float = 0.7) -> Prediction:
        """基于当前记忆和上下文，预测环境状态。"""
        with self._lock:
            # 模拟预测：基于世界模型知识库生成 expected 状态
            predicted_state: Dict[str, Any] = {"domain": domain.value, "context_keys": list(context.keys())}

            # 从世界模型中检索领域知识
            domain_key = f"domain_{domain.value}"
            cached_knowledge = self.world_model.get(domain_key, {})

            for k, v in context.items():
                if k in cached_knowledge:
                    predicted_state[k] = cached_knowledge[k]
                else:
                    predicted_state[k] = v  # 无先验时信任输入

            prediction = Prediction(
                prediction_id=str(uuid.uuid4())[:8],
                domain=domain,
                predicted_state=predicted_state,
                confidence=confidence,
                context=context,
            )
            self.predictions.append(prediction)
            return prediction

    def update_knowledge(self, domain: DomainName, key: str, value: Any):
        """更新世界模型中的领域知识。"""
        with self._lock:
            domain_key = f"domain_{domain.value}"
            self.world_model.setdefault(domain_key, {})[key] = value

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            domain_dist = defaultdict(int)
            for p in self.predictions:
                domain_dist[p.domain.value] += 1
            return {
                "total_predictions": len(self.predictions),
                "domain_distribution": dict(domain_dist),
                "world_model_fields": sum(len(v) for v in self.world_model.values()),
            }


class RealityComparator:
    """预测 vs 真实自动对比，量化偏差。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.deviations: List[DeviationReport] = []

    def compare(self, prediction: Prediction, reality: RealitySnapshot) -> DeviationReport:
        with self._lock:
            report = DeviationReport(
                report_id=str(uuid.uuid4())[:8],
                prediction_id=prediction.prediction_id,
                domain=prediction.domain,
            )

            predicted = prediction.predicted_state
            actual = reality.actual_state

            all_keys = set(predicted.keys()) | set(actual.keys())
            total_fields = max(len(all_keys), 1)

            for key in all_keys:
                p_val = predicted.get(key)
                a_val = actual.get(key)
                if p_val == a_val:
                    report.matched_fields.append(key)
                else:
                    report.deviated_fields[key] = (p_val, a_val)

            match_ratio = len(report.matched_fields) / total_fields
            report.overall_deviation_score = round(1.0 - match_ratio, 4)

            # 严重度判定
            if report.overall_deviation_score < 0.05:
                report.severity = DeviationSeverity.NEGLIGIBLE
            elif report.overall_deviation_score < 0.15:
                report.severity = DeviationSeverity.MINOR
            elif report.overall_deviation_score < 0.35:
                report.severity = DeviationSeverity.MAJOR
            else:
                report.severity = DeviationSeverity.CRITICAL

            # 提取知识缺口
            for field, (p, a) in report.deviated_fields.items():
                report.knowledge_gaps.append(f"Field '{field}': predicted={p}, actual={a}")

            report.suggested_corrections = [
                f"Update domain '{prediction.domain.value}' field '{field}' from {p} to {a}"
                for field, (p, a) in report.deviated_fields.items()
            ]

            self.deviations.append(report)
            return report

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            severity_counts = defaultdict(int)
            avg_deviation = 0.0
            for d in self.deviations:
                severity_counts[d.severity.value] += 1
                avg_deviation += d.overall_deviation_score
            n = max(len(self.deviations), 1)
            return {
                "total_comparisons": len(self.deviations),
                "avg_deviation": round(avg_deviation / n, 4),
                "severity_breakdown": dict(severity_counts),
            }


class GapLearningPipeline:
    """差距学习流水线：误差→记忆修正→环境认知进化。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.corrections: List[CorrectionRecord] = []
        self.learned_patterns: Dict[str, int] = defaultdict(int)  # pattern → count

    def learn(
        self,
        deviation: DeviationReport,
        predictor: WorldModelPredictor,
        apply: bool = True,
    ) -> List[CorrectionRecord]:
        with self._lock:
            records: List[CorrectionRecord] = []
            for gap in deviation.suggested_corrections:
                # 解析修正建议
                record = CorrectionRecord(
                    record_id=str(uuid.uuid4())[:8],
                    domain=deviation.domain,
                    deviation_report_id=deviation.report_id,
                    old_knowledge="",
                    new_knowledge=gap,
                    applied=False,
                )

                if apply and deviation.severity != DeviationSeverity.NEGLIGIBLE:
                    # 从 gap 中提取 field 和 value，更新世界模型
                    # gap 格式: "Update domain '{domain}' field '{field}' from {old} to {new}"
                    parts = gap.split("'")
                    if len(parts) >= 5:
                        field_name = parts[3]
                        new_value = parts[-2] if len(parts) > 6 else parts[-1].strip()
                        predictor.update_knowledge(deviation.domain, field_name, new_value)
                        record.applied = True

                    self.learned_patterns[gap[:60]] += 1

                self.corrections.append(record)
                records.append(record)

            return records

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_corrections": len(self.corrections),
                "applied_corrections": sum(1 for c in self.corrections if c.applied),
                "unique_patterns": len(self.learned_patterns),
            }


class DomainAdaptiveController:
    """7 领域差异感知自适应控制。

    每个领域独立的误差容忍度、学习率、稳定性监测。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.profiles: Dict[DomainName, DomainProfile] = {
            d: DomainProfile(domain=d, tolerance=DOMAIN_DEFAULTS[d][0], learning_rate=DOMAIN_DEFAULTS[d][1])
            for d in DomainName
        }

    def should_correct(self, domain: DomainName, severity: DeviationSeverity) -> bool:
        """根据领域容忍度判断是否应触发修正。"""
        with self._lock:
            profile = self.profiles[domain]
            if severity == DeviationSeverity.NEGLIGIBLE:
                return False
            if severity == DeviationSeverity.CRITICAL:
                return True
            return severity.value in ("minor", "major") and profile.tolerance < 0.2

    def adapt(self, domain: DomainName, correct_count: int, stability_delta: float):
        """自适应调整领域参数。"""
        with self._lock:
            profile = self.profiles[domain]
            profile.correction_count += correct_count
            profile.last_correction = time.time()
            profile.stability_score = max(0.0, min(1.0, profile.stability_score + stability_delta))

            # 领域稳定时降低学习率（避免过度修正）
            if profile.stability_score > 0.8:
                profile.learning_rate = max(0.001, profile.learning_rate * 0.95)
            elif profile.correction_count > 20 and profile.stability_score < 0.4:
                profile.learning_rate = min(0.05, profile.learning_rate * 1.05)

    def get_profile(self, domain: DomainName) -> DomainProfile:
        with self._lock:
            return self.profiles[domain]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                d.value: {
                    "tolerance": p.tolerance,
                    "learning_rate": p.learning_rate,
                    "corrections": p.correction_count,
                    "stability": round(p.stability_score, 3),
                }
                for d, p in self.profiles.items()
            }


# ============================================================================
# Pipeline Orchestrator
# ============================================================================

class WorldModelMemoryFusionEngine:
    """世界模型-记忆融合总控。

    编排：预演 → 验证对比 → 差距学习 → 记忆更新 → 领域自适应。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.predictor = WorldModelPredictor()
        self.comparator = RealityComparator()
        self.learner = GapLearningPipeline()
        self.controller = DomainAdaptiveController()

    def run_cycle(
        self,
        domain: DomainName,
        context: Dict[str, Any],
        reality: RealitySnapshot,
    ) -> Dict[str, Any]:
        """执行一次完整融合循环。"""
        with self._lock:
            # Phase 1: Predict
            prediction = self.predictor.predict(domain, context)

            # Phase 2: Compare
            deviation = self.comparator.compare(prediction, reality)

            # Phase 3: Learn
            if self.controller.should_correct(domain, deviation.severity):
                corrections = self.learner.learn(deviation, self.predictor, apply=True)
                self.controller.adapt(domain, len(corrections),
                                      -0.02 if deviation.severity.value in ("MAJOR", "critical") else 0.01)
            else:
                corrections = []

            return {
                "prediction_id": prediction.prediction_id,
                "confidence": prediction.confidence,
                "deviation_score": deviation.overall_deviation_score,
                "severity": deviation.severity.value,
                "corrections_applied": sum(1 for c in corrections if c.applied),
                "knowledge_gaps": len(deviation.knowledge_gaps),
            }

    def statistics(self) -> Dict[str, Any]:
        return {
            "predictor": self.predictor.statistics(),
            "comparator": self.comparator.statistics(),
            "learner": self.learner.statistics(),
            "controller": self.controller.statistics(),
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P17-8 World Model Memory Fusion",
        "benchmark": "世界模型+记忆闭环",
        "classes": 5,
        "enums": 3,
        "dataclasses": 5,
        "key_pattern": "Predict→Compare→Learn Pipeline + Domain-Adaptive Control + Deviation-Driven Correction",
        "key_metric": "Closed-loop world model evolution across 7 domains",
        "thread_safe": True,
    }
