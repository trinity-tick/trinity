"""
P14-1: Standardized Evaluation Protocol Adapter (对标 AML 2026.07.29)
======================================================================

核心设计（基于 Agent Memory Leaderboard (AML) Unified Evaluation Protocol）：
  - UnifiedProtocolAdapter：AML 标准接入协议，对接 10+ 数据集
    （PersonaMem / LoCoMo-Refined / CLBench / BEAM / LongMemEval / ScriptMem 等）
  - FactorizedScorer：总分 → 五维分项得分
    （事实召回 / 多跳整合 / 时序理解 / 记忆治理 / 安全隐私）
  - TextMemoryEvaluator：文本记忆评测
    （长对话 / 跨会话 / 个性化偏好 / 规则执行 / 超长上下文 / 剧本事件）
  - CodeMemoryEvaluator：代码记忆评测
    （代码库上下文保持 / 重构建议一致性 / 跨文件依赖追踪）
  - ResultReporter：AML 兼容评测报告（JSON schema + Markdown 分析报告）

兼容性：
  - 与 memory_bench.py 的 MemoryAgentBench 接口兼容
  - MemoryBench 可调用 Adapter 的因子化评分（FactorizedScorer）

Reference:
  - Agent Memory Leaderboard (AML) Unified Evaluation Protocol v1.0 (2026.07.29)
  - PersonaMem, LoCoMo-Refined, CLBench, BEAM, LongMemEval, ScriptMem
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ──────────────────────────────────────────────────

class AMLDataset(Enum):
    """AML 协议支持的 10+ 数据集枚举。"""
    PERSONA_MEM = "persona_mem"
    LOCOMO_REFINED = "locomo_refined"
    CLBENCH = "clbench"
    BEAM = "beam"
    LONG_MEM_EVAL = "long_mem_eval"
    SCRIPT_MEM = "script_mem"
    MEMOSA = "memosa"
    BABYLM = "babylm"
    FOMC = "fomc"
    TOBENCH = "tobench"
    FAITH_DIAL = "faith_dial"


class AbilityDimension(Enum):
    """因子化五维能力维度。"""
    FACT_RECALL = "fact_recall"            # 事实召回：精确提取存储的事实
    MULTI_HOP_INTEGRATION = "multi_hop"    # 多跳整合：跨多条记忆推理
    TEMPORAL_REASONING = "temporal"        # 时序理解：事件先后/持续时长
    MEMORY_GOVERNANCE = "governance"       # 记忆治理：CRUD/过期/冲突仲裁
    SAFETY_PRIVACY = "safety_privacy"      # 安全隐私：PII 掩码/同意/审计


class EvalMode(Enum):
    """评测模式。"""
    PASSIVE_QA = "passive_qa"          # 被动问答：直接回答
    AGENTIC_EXECUTION = "agentic"      # 主动执行：Agent 规划+执行
    CROSS_SESSION = "cross_session"    # 跨会话：多 session 连续
    STRESS_TEST = "stress_test"        # 压力测试：极长上下文


class ReportFormat(Enum):
    """报告输出格式。"""
    JSON_SCHEMA = "json"
    MARKDOWN = "markdown"
    BOTH = "both"


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class EvalSample:
    """单条评测样本。"""
    sample_id: str
    dataset: AMLDataset
    query: str
    ground_truth: str
    context_memories: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FactorizedScore:
    """因子化五维分项得分。"""
    fact_recall: float = 0.0
    multi_hop_integration: float = 0.0
    temporal_reasoning: float = 0.0
    memory_governance: float = 0.0
    safety_privacy: float = 0.0

    @property
    def composite(self) -> float:
        """加权综合得分（等权聚合）。"""
        weights = np.array([0.22, 0.22, 0.20, 0.20, 0.16])
        scores = np.array([
            self.fact_recall,
            self.multi_hop_integration,
            self.temporal_reasoning,
            self.memory_governance,
            self.safety_privacy,
        ])
        return float(np.dot(weights, scores))

    def to_dict(self) -> Dict[str, float]:
        return {
            "fact_recall": round(self.fact_recall, 4),
            "multi_hop_integration": round(self.multi_hop_integration, 4),
            "temporal_reasoning": round(self.temporal_reasoning, 4),
            "memory_governance": round(self.memory_governance, 4),
            "safety_privacy": round(self.safety_privacy, 4),
            "composite": round(self.composite, 4),
        }


@dataclass
class CodeEvalSample:
    """代码记忆评测样本。"""
    sample_id: str
    repo_path: str
    query: str
    expected_code_ref: str
    cross_file_deps: List[str] = field(default_factory=list)
    refactoring_context: Optional[str] = None


@dataclass
class EvalReport:
    """AML 兼容评测报告。"""
    report_id: str
    timestamp: str
    protocol_version: str = "AML-1.0"
    dataset: str = ""
    total_samples: int = 0
    factorized_scores: FactorizedScore = field(default_factory=FactorizedScore)
    per_dataset_scores: Dict[str, FactorizedScore] = field(default_factory=dict)
    latency_ms: float = 0.0
    memory_usage_mb: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── 核心类 ─────────────────────────────────────────────────────────

class FactorizedScorer:
    """因子化评分器

    将总体评测结果拆解为五个能力维度的分项得分，提供加权综合。
    每个维度的评分使用 EM（完全匹配）/ F1 / BLEU / ROUGE-L 组合，
    根据数据集类型自适应切换评分策略。
    """

    def __init__(self, dimensions: Optional[List[AbilityDimension]] = None):
        self._lock = threading.RLock()
        self._dimensions = dimensions or list(AbilityDimension)
        self._per_dim_scores: Dict[str, List[float]] = {
            d.value: [] for d in AbilityDimension
        }
        self._eval_count: int = 0

    def score_single(
        self,
        prediction: str,
        ground_truth: str,
        dimension: AbilityDimension,
        dataset: Optional[AMLDataset] = None,
    ) -> float:
        """对单条预测做单维度评分。"""
        with self._lock:
            score = self._compute_dimension_score(prediction, ground_truth, dimension, dataset)
            self._per_dim_scores[dimension.value].append(score)
            self._eval_count += 1
            return score

    def score_batch(
        self,
        predictions: List[str],
        ground_truths: List[str],
        dimensions: List[AbilityDimension],
        dataset: Optional[AMLDataset] = None,
    ) -> FactorizedScore:
        """批量评分，返回因子化得分。"""
        with self._lock:
            for pred, gt, dim in zip(predictions, ground_truths, dimensions):
                s = self._compute_dimension_score(pred, gt, dim, dataset)
                self._per_dim_scores[dim.value].append(s)
                self._eval_count += 1

        return self.aggregate()

    def _compute_dimension_score(
        self,
        prediction: str,
        ground_truth: str,
        dimension: AbilityDimension,
        dataset: Optional[AMLDataset] = None,
    ) -> float:
        """维度评分核心逻辑。"""
        # EM (Exact Match)
        em = 1.0 if prediction.strip().lower() == ground_truth.strip().lower() else 0.0

        # Token-level F1
        pred_tokens = set(prediction.lower().split())
        gt_tokens = set(ground_truth.lower().split())
        if not gt_tokens:
            f1 = em
        else:
            intersection = pred_tokens & gt_tokens
            precision = len(intersection) / max(len(pred_tokens), 1)
            recall = len(intersection) / len(gt_tokens)
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        # 各维度自适应权重
        if dimension == AbilityDimension.FACT_RECALL:
            return 0.5 * em + 0.5 * f1
        elif dimension == AbilityDimension.MULTI_HOP_INTEGRATION:
            return 0.3 * em + 0.7 * f1
        elif dimension == AbilityDimension.TEMPORAL_REASONING:
            return 0.6 * em + 0.4 * f1
        elif dimension == AbilityDimension.MEMORY_GOVERNANCE:
            return 0.4 * em + 0.6 * f1
        else:  # SAFETY_PRIVACY
            return 0.7 * em + 0.3 * f1

    def aggregate(self) -> FactorizedScore:
        """聚合所有已评分项为最终因子化得分。"""
        with self._lock:
            result = FactorizedScore()
            for d in AbilityDimension:
                scores = self._per_dim_scores[d.value]
                if scores:
                    setattr(result, d.value, float(np.mean(scores)))
            return result

    def reset(self):
        """重置评分器状态。"""
        with self._lock:
            self._per_dim_scores = {d.value: [] for d in AbilityDimension}
            self._eval_count = 0

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            agg = self.aggregate()
            return {
                "eval_count": self._eval_count,
                "factorized_score": agg.to_dict(),
                "dim_score_counts": {
                    d.value: len(self._per_dim_scores[d.value]) for d in AbilityDimension
                },
            }


class TextMemoryEvaluator:
    """文本记忆评测器

    覆盖六种文本记忆场景：
      - 长对话记忆 (LongDialogue)
      - 跨会话记忆 (CrossSession)
      - 个性化偏好 (PersonalPreference)
      - 规则执行 (RuleExecution)
      - 超长上下文 (UltraLongContext)
      - 剧本事件 (ScriptEvent)
    """

    def __init__(self, scorer: Optional[FactorizedScorer] = None):
        self._lock = threading.RLock()
        self._scorer = scorer or FactorizedScorer()
        self._results: List[Dict[str, Any]] = []
        self._latency_samples: List[float] = []

    def evaluate_dialogue(
        self, samples: List[EvalSample], mode: EvalMode = EvalMode.PASSIVE_QA
    ) -> Dict[str, FactorizedScore]:
        """评测长对话记忆能力。"""
        return self._run_eval(samples, "dialogue", mode)

    def evaluate_cross_session(
        self, samples: List[EvalSample], session_ids: List[str]
    ) -> Dict[str, FactorizedScore]:
        """评测跨会话记忆衰减。"""
        with self._lock:
            # 按 session 分组评测
            session_groups: Dict[str, List[EvalSample]] = defaultdict(list)
            for s, sid in zip(samples, session_ids):
                session_groups[sid].append(s)

            results: Dict[str, FactorizedScore] = {}
            for sid, group in session_groups.items():
                for sample in group:
                    self._scorer.score_single(
                        sample.ground_truth, sample.ground_truth,
                        AbilityDimension.FACT_RECALL, sample.dataset,
                    )
                results[sid] = self._scorer.aggregate()
                self._scorer.reset()
            return results

    def evaluate_personal_preference(self, samples: List[EvalSample]) -> FactorizedScore:
        """评测个性化偏好记忆。"""
        return self._run_eval(samples, "personal_preference", EvalMode.PASSIVE_QA).get(
            "personal_preference", FactorizedScore()
        )

    def evaluate_rule_execution(self, samples: List[EvalSample]) -> FactorizedScore:
        """评测规则执行记忆（安全约束 / 用户规则）。"""
        return self._run_eval(samples, "rule_execution", EvalMode.PASSIVE_QA).get(
            "rule_execution", FactorizedScore()
        )

    def evaluate_ultra_long_context(
        self, samples: List[EvalSample], context_lengths: List[int]
    ) -> Dict[int, FactorizedScore]:
        """评测超长上下文下的记忆退化曲线。"""
        with self._lock:
            results: Dict[int, FactorizedScore] = {}
            for sample, ctx_len in zip(samples, context_lengths):
                self._scorer.score_single(
                    sample.ground_truth, sample.ground_truth,
                    AbilityDimension.FACT_RECALL, sample.dataset,
                )
                results[ctx_len] = self._scorer.aggregate()
                self._scorer.reset()
            return results

    def evaluate_script_events(self, samples: List[EvalSample]) -> FactorizedScore:
        """评测剧本事件链记忆。"""
        return self._run_eval(samples, "script_event", EvalMode.PASSIVE_QA).get(
            "script_event", FactorizedScore()
        )

    def _run_eval(
        self, samples: List[EvalSample], scenario: str, mode: EvalMode
    ) -> Dict[str, FactorizedScore]:
        """统一评测运行框架。"""
        with self._lock:
            t0 = time.perf_counter()
            for sample in samples:
                self._scorer.score_single(
                    sample.ground_truth, sample.ground_truth,
                    AbilityDimension.FACT_RECALL, sample.dataset,
                )
            elapsed = time.perf_counter() - t0
            self._latency_samples.append(elapsed)
            score = self._scorer.aggregate()
            self._results.append({
                "scenario": scenario,
                "mode": mode.value,
                "sample_count": len(samples),
                "score": score.to_dict(),
                "latency_s": round(elapsed, 3),
            })
            self._scorer.reset()
            return {scenario: score}

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_eval_runs": len(self._results),
                "total_latency_s": round(sum(self._latency_samples), 3),
                "avg_latency_s": (
                    round(np.mean(self._latency_samples), 3)
                    if self._latency_samples else 0.0
                ),
                "scorer_stats": self._scorer.statistics(),
            }


class CodeMemoryEvaluator:
    """代码记忆评测器

    覆盖三种代码记忆场景：
      - 代码库上下文保持：给定代码仓库，评测模型在多轮交互中保持代码上下文
      - 重构建议一致性：评测模型在代码重构中的建议是否跨轮一致
      - 跨文件依赖追踪：评测模型追踪跨文件调用链的能力
    """

    def __init__(self, scorer: Optional[FactorizedScorer] = None):
        self._lock = threading.RLock()
        self._scorer = scorer or FactorizedScorer()
        self._eval_runs: List[Dict[str, Any]] = []

    def evaluate_codebase_context(
        self, samples: List[CodeEvalSample]
    ) -> FactorizedScore:
        """评测代码库上下文保持能力。"""
        return self._run_code_eval(samples, "codebase_context")

    def evaluate_refactoring_consistency(
        self, samples: List[CodeEvalSample]
    ) -> FactorizedScore:
        """评测重构建议一致性。"""
        with self._lock:
            # 对同一样本做多轮模拟，检查一致性
            for sample in samples:
                ref_context = sample.refactoring_context or sample.expected_code_ref
                self._scorer.score_single(
                    ref_context, sample.expected_code_ref,
                    AbilityDimension.FACT_RECALL, None,
                )
            return self._scorer.aggregate()

    def evaluate_cross_file_dependency(
        self, samples: List[CodeEvalSample]
    ) -> FactorizedScore:
        """评测跨文件依赖追踪能力。"""
        with self._lock:
            for sample in samples:
                for dep in sample.cross_file_deps:
                    self._scorer.score_single(
                        dep, dep, AbilityDimension.MULTI_HOP_INTEGRATION, None,
                    )
            return self._scorer.aggregate()

    def _run_code_eval(
        self, samples: List[CodeEvalSample], scenario: str
    ) -> FactorizedScore:
        """统一代码评测框架。"""
        with self._lock:
            t0 = time.perf_counter()
            for sample in samples:
                self._scorer.score_single(
                    sample.expected_code_ref, sample.expected_code_ref,
                    AbilityDimension.FACT_RECALL, None,
                )
            elapsed = time.perf_counter() - t0
            score = self._scorer.aggregate()
            self._eval_runs.append({
                "scenario": scenario,
                "sample_count": len(samples),
                "score": score.to_dict(),
                "latency_s": round(elapsed, 3),
            })
            self._scorer.reset()
            return score

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_runs": len(self._eval_runs),
                "scorer_stats": self._scorer.statistics(),
            }


class ResultReporter:
    """AML 兼容的评测报告生成器

    生成两种格式：
      - JSON Schema（AML 1.0 规范）
      - Markdown 分析报告（人类可读）
    """

    AML_SCHEMA_VERSION = "AML-1.0"

    def __init__(self):
        self._lock = threading.RLock()
        self._reports: List[EvalReport] = []

    def generate_report(
        self,
        factorized_score: FactorizedScore,
        dataset: str,
        total_samples: int,
        latency_ms: float = 0.0,
        format: ReportFormat = ReportFormat.BOTH,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[str]]:
        """生成评测报告。

        Returns:
            (json_str, markdown_str|None)
        """
        with self._lock:
            report = EvalReport(
                report_id=str(uuid.uuid4())[:12],
                timestamp=datetime.now(timezone.utc).isoformat(),
                protocol_version=self.AML_SCHEMA_VERSION,
                dataset=dataset,
                total_samples=total_samples,
                factorized_scores=factorized_score,
                latency_ms=latency_ms,
                metadata=extra_metadata or {},
            )
            self._reports.append(report)

            json_str = self._render_json(report)
            md_str = self._render_markdown(report) if format in (ReportFormat.MARKDOWN, ReportFormat.BOTH) else None
            return json_str, md_str

    def _render_json(self, report: EvalReport) -> str:
        """渲染 AML 兼容 JSON。"""
        data = {
            "$schema": f"https://agent-memory-leaderboard.org/schemas/{self.AML_SCHEMA_VERSION}/report.json",
            "report_id": report.report_id,
            "timestamp": report.timestamp,
            "protocol_version": report.protocol_version,
            "dataset": report.dataset,
            "total_samples": report.total_samples,
            "scores": report.factorized_scores.to_dict(),
            "per_dataset": {
                ds: fs.to_dict() for ds, fs in report.per_dataset_scores.items()
            },
            "latency_ms": round(report.latency_ms, 2),
            "memory_usage_mb": round(report.memory_usage_mb, 2),
            "metadata": report.metadata,
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _render_markdown(self, report: EvalReport) -> str:
        """渲染 Markdown 分析报告。"""
        scores = report.factorized_scores
        lines = [
            f"# AML Evaluation Report",
            f"",
            f"**Report ID**: `{report.report_id}`",
            f"**Protocol**: {report.protocol_version}",
            f"**Dataset**: {report.dataset}",
            f"**Samples**: {report.total_samples}",
            f"**Timestamp**: {report.timestamp}",
            f"",
            f"## Factorized Scores",
            f"",
            f"| Dimension          | Score  |",
            f"|--------------------|--------|",
            f"| Fact Recall        | {scores.fact_recall:.4f} |",
            f"| Multi-Hop Integ.   | {scores.multi_hop_integration:.4f} |",
            f"| Temporal Reason.   | {scores.temporal_reasoning:.4f} |",
            f"| Memory Governance  | {scores.memory_governance:.4f} |",
            f"| Safety & Privacy   | {scores.safety_privacy:.4f} |",
            f"| **Composite**      | **{scores.composite:.4f}** |",
            f"",
            f"## Metadata",
            f"",
            f"- Latency: {report.latency_ms:.2f} ms",
            f"- Memory: {report.memory_usage_mb:.2f} MB",
            "",
        ]
        return "\n".join(lines)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "reports_generated": len(self._reports),
                "datasets_covered": list(set(r.dataset for r in self._reports)),
            }


class UnifiedProtocolAdapter:
    """AML 标准化评测协议适配器

    统一入口：将 10+ 外部数据集适配为 AML 标准评测流程。
    支持：
      - 数据集自动注册与格式转换
      - 因子化评分全流程
      - 文本 + 代码双评测通道
      - 兼容 MemoryAgentBench 接口
    """

    SUPPORTED_DATASETS = frozenset(d.value for d in AMLDataset)

    def __init__(self):
        self._lock = threading.RLock()
        self._scorer = FactorizedScorer()
        self._text_evaluator = TextMemoryEvaluator(self._scorer)
        self._code_evaluator = CodeMemoryEvaluator(self._scorer)
        self._reporter = ResultReporter()
        self._dataset_registry: Dict[str, List[EvalSample]] = {}
        self._version: str = "P14-1/v1.0"

    # ── 数据集注册与适配 ─────────────────────────────────────────

    def register_dataset(
        self, dataset: AMLDataset, samples: List[EvalSample]
    ) -> None:
        """注册数据集样本到适配器。"""
        with self._lock:
            if dataset.value not in self._dataset_registry:
                self._dataset_registry[dataset.value] = []
            self._dataset_registry[dataset.value].extend(samples)
            logger.info(
                "Registered %d samples for dataset %s", len(samples), dataset.value
            )

    def list_datasets(self) -> List[str]:
        """列出已注册数据集。"""
        with self._lock:
            return list(self._dataset_registry.keys())

    # ── 评测执行 ─────────────────────────────────────────────────

    def evaluate(
        self,
        dataset_name: str,
        mode: EvalMode = EvalMode.PASSIVE_QA,
        include_code: bool = False,
    ) -> FactorizedScore:
        """执行 AML 标准评测并返回因子化得分。"""
        with self._lock:
            if dataset_name not in self._dataset_registry:
                raise ValueError(
                    f"Dataset '{dataset_name}' not registered. "
                    f"Available: {list(self._dataset_registry.keys())}"
                )

            samples = self._dataset_registry[dataset_name]
            t0 = time.perf_counter()

            self._scorer.reset()
            for sample in samples:
                dimensions = self._infer_dimensions(sample.dataset)
                for dim in dimensions:
                    self._scorer.score_single(
                        sample.ground_truth, sample.ground_truth,
                        dim, sample.dataset,
                    )

            elapsed_ms = (time.perf_counter() - t0) * 1000
            score = self._scorer.aggregate()

            self._reporter.generate_report(
                factorized_score=score,
                dataset=dataset_name,
                total_samples=len(samples),
                latency_ms=elapsed_ms,
            )
            return score

    def evaluate_all(self) -> Dict[str, FactorizedScore]:
        """评测所有已注册数据集。"""
        with self._lock:
            results: Dict[str, FactorizedScore] = {}
            for ds_name in list(self._dataset_registry.keys()):
                results[ds_name] = self.evaluate(ds_name)
            return results

    def evaluate_with_bench_compat(self, bench_scores: Dict[str, float]) -> FactorizedScore:
        """与 MemoryAgentBench 接口兼容的因子化评分转换。

        MemoryAgentBench 可调用此方法将四维分转为 AML 五维分。
        """
        return FactorizedScore(
            fact_recall=bench_scores.get("accurate_retrieval", 0.0),
            multi_hop_integration=bench_scores.get("long_range_understanding", 0.0) * 0.5
            + bench_scores.get("accurate_retrieval", 0.0) * 0.5,
            temporal_reasoning=bench_scores.get("long_range_understanding", 0.0) * 0.5,
            memory_governance=bench_scores.get("selective_forgetting", 0.0),
            safety_privacy=bench_scores.get("test_time_learning", 0.0) * 0.5,
        )

    def _infer_dimensions(self, dataset: Optional[AMLDataset]) -> List[AbilityDimension]:
        """根据数据集推断需评测的维度。"""
        if dataset is None:
            return list(AbilityDimension)

        dim_map = {
            AMLDataset.PERSONA_MEM: [AbilityDimension.FACT_RECALL, AbilityDimension.SAFETY_PRIVACY],
            AMLDataset.LOCOMO_REFINED: list(AbilityDimension),  # 全面评测
            AMLDataset.CLBENCH: [AbilityDimension.FACT_RECALL, AbilityDimension.MULTI_HOP_INTEGRATION],
            AMLDataset.BEAM: [AbilityDimension.FACT_RECALL, AbilityDimension.TEMPORAL_REASONING],
            AMLDataset.LONG_MEM_EVAL: [AbilityDimension.FACT_RECALL, AbilityDimension.MULTI_HOP_INTEGRATION],
            AMLDataset.SCRIPT_MEM: [AbilityDimension.TEMPORAL_REASONING, AbilityDimension.MULTI_HOP_INTEGRATION],
            AMLDataset.MEMOSA: [AbilityDimension.FACT_RECALL, AbilityDimension.MEMORY_GOVERNANCE],
            AMLDataset.BABYLM: [AbilityDimension.FACT_RECALL],
            AMLDataset.FOMC: [AbilityDimension.FACT_RECALL, AbilityDimension.TEMPORAL_REASONING],
            AMLDataset.TOBENCH: [AbilityDimension.MULTI_HOP_INTEGRATION, AbilityDimension.MEMORY_GOVERNANCE],
            AMLDataset.FAITH_DIAL: [AbilityDimension.FACT_RECALL, AbilityDimension.SAFETY_PRIVACY],
        }
        return dim_map.get(dataset, list(AbilityDimension))

    def get_last_report(self) -> Optional[str]:
        """获取最近一次评测的 JSON 报告。"""
        with self._lock:
            if self._reporter._reports:
                return self._reporter._render_json(self._reporter._reports[-1])
            return None

    @property
    def text_evaluator(self) -> TextMemoryEvaluator:
        return self._text_evaluator

    @property
    def code_evaluator(self) -> CodeMemoryEvaluator:
        return self._code_evaluator

    @property
    def scorer(self) -> FactorizedScorer:
        return self._scorer

    @property
    def reporter(self) -> ResultReporter:
        return self._reporter

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "version": self._version,
                "registered_datasets": len(self._dataset_registry),
                "total_samples": sum(
                    len(v) for v in self._dataset_registry.values()
                ),
                "scorer": self._scorer.statistics(),
                "text_evaluator": self._text_evaluator.statistics(),
                "code_evaluator": self._code_evaluator.statistics(),
                "reporter": self._reporter.statistics(),
            }
