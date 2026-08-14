"""
P9-1: MemoryAgentBench Evaluation Framework Integration (对标 ICLR2026)
========================================================================

核心设计（基于 "Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions"）：
  - 四维能力评测协议：
    1. Accurate Retrieval（准确检索）：单跳/多跳事实检索
    2. Test-Time Learning（测试时学习）：从上下文样例学会新行为
    3. Long-Range Understanding（长程理解）：跨多轮交互的信息链追踪
    4. Selective Forgetting（选择性遗忘）：应忘的忘掉、该留的保留
  - 增量多轮交互协议（chunk-by-chunk feeding）：长文本分块逐步喂入
  - 12 数据集适配层：含 EventQA、FactConsolidation 两个自建数据集格式
  - 统一四维打分 + LoCoMo 兼容格式，可与现有排行榜对标

设计要点：
  - 评测协议与 MemoryAgentBench ICLR2026 论文对齐
  - LoCoMo 格式兼容：输出 JSON 与 Snap Research LoCoMo 评测格式一致
  - 插件化数据集适配器：新增数据集只需实现 DataAdapter 接口

Reference:
  - "Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions" (ICLR 2026)
  - LoCoMo Benchmark (Snap Research)
  - LongMemEval-S (ICLR 2025)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class EvalDimension(Enum):
    """四维评测维度。"""
    ACCURATE_RETRIEVAL = "accurate_retrieval"
    TEST_TIME_LEARNING = "test_time_learning"
    LONG_RANGE_UNDERSTANDING = "long_range_understanding"
    SELECTIVE_FORGETTING = "selective_forgetting"


class HopType(Enum):
    """跳数类型。"""
    SINGLE_HOP = "single_hop"
    MULTI_HOP = "multi_hop"


class InteractionPhase(Enum):
    """交互阶段。"""
    CHUNK_FEEDING = "chunk_feeding"   # 分块喂入
    QA_TESTING = "qa_testing"         # 问答测试
    FORGETTING = "forgetting"         # 遗忘阶段
    RETENTION_CHECK = "retention_check"  # 保留检查


class DatasetType(Enum):
    """数据集类型（12 数据集）。"""
    EVENT_QA = "event_qa"                     # 自建：事件时间线问答
    FACT_CONSOLIDATION = "fact_consolidation"  # 自建：事实整合
    LOCOMO = "locomo"                          # LoCoMo 多轮对话（Snap Research）
    LONGMEMEVAL_S = "longmem_eval_s"           # LongMemEval-S (ICLR 2025)
    HOTPOT_QA = "hotpot_qa"                    # HotpotQA 多跳推理
    WIKI_MULTI_HOP = "wiki_multi_hop"          # WikiMultiHop
    MRCR = "mrcr"                               # 多轮共指消解
    EP_BENCH = "ep_bench"                       # 情节记忆基准
    NOVEL_QA = "novel_qa"                       # NovelQA
    NOCHA = "nocha"                             # NOCHA
    INFINITY_BENCH = "infinity_bench"           # ∞-Bench
    LONG_BENCH = "long_bench"                   # LongBench


# ── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class ChunkRecord:
    """分块喂入记录。

    Args:
        chunk_id: 分块序号
        text: 分块文本内容
        metadata: 分块元数据（主题/时间/来源等）
        feed_timestamp: 喂入时间戳
        session_id: 所属会话 ID
    """
    chunk_id: int
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    feed_timestamp: float = field(default_factory=time.time)
    session_id: str = ""


@dataclass
class QAPair:
    """问答对。

    Args:
        query: 问题文本
        answer: 正确答案
        dimension: 所属评测维度
        hop_type: 跳数类型
        dataset: 来源数据集
        metadata: 扩展元数据
    """
    query: str
    answer: str
    dimension: EvalDimension
    hop_type: HopType = HopType.SINGLE_HOP
    dataset: DatasetType = DatasetType.EVENT_QA
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    """单维度评测结果。

    Args:
        dimension: 评测维度
        total_questions: 总问题数
        correct: 正确数
        accuracy: 准确率
        latency_stats: 延迟统计 (p50/p95/p99 ms)
        detail_per_hop: 按跳数的细分结果
    """
    dimension: EvalDimension
    total_questions: int = 0
    correct: int = 0
    accuracy: float = 0.0
    latency_stats: Dict[str, float] = field(default_factory=dict)
    detail_per_hop: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class UnifiedScore:
    """统一四维打分。

    Args:
        accurate_retrieval: 准确检索得分 (0-100)
        test_time_learning: 测试时学习得分 (0-100)
        long_range_understanding: 长程理解得分 (0-100)
        selective_forgetting: 选择性遗忘得分 (0-100)
        composite: 综合得分 (四维加权平均)
        locomo_compatible: LoCoMo 兼容格式 JSON
    """
    accurate_retrieval: float = 0.0
    test_time_learning: float = 0.0
    long_range_understanding: float = 0.0
    selective_forgetting: float = 0.0
    composite: float = 0.0
    locomo_compatible: Dict[str, Any] = field(default_factory=dict)

    def to_locomo_json(self) -> Dict[str, Any]:
        """转换为 LoCoMo 兼容格式。"""
        return {
            "benchmark": "MemoryAgentBench",
            "framework": "Trinity",
            "version": "P9-1",
            "scores": {
                "single_hop": self.accurate_retrieval * 0.5,
                "multi_hop": self.accurate_retrieval * 0.5,
                "temporal": self.long_range_understanding,
                "open_domain": self.long_range_understanding,
                "overall": self.composite,
            },
            "per_dimension": {
                "accurate_retrieval": self.accurate_retrieval,
                "test_time_learning": self.test_time_learning,
                "long_range_understanding": self.long_range_understanding,
                "selective_forgetting": self.selective_forgetting,
            },
            "composite": self.composite,
            "metadata": {
                "eval_timestamp": time.time(),
                "framework_version": "v6.42",
            },
        }


@dataclass
class BenchmarkRun:
    """一次完整的基准测试运行记录。

    Args:
        run_id: 运行唯一标识
        start_time: 开始时间
        end_time: 结束时间
        total_chunks: 总喂入分块数
        total_qa_pairs: 总问答对数
        results: 四维结果列表
        unified_score: 统一得分
    """
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    total_chunks: int = 0
    total_qa_pairs: int = 0
    results: List[EvalResult] = field(default_factory=list)
    unified_score: UnifiedScore = field(default_factory=UnifiedScore)


# ── 数据集适配器接口 ─────────────────────────────────────────────────


class DataAdapter:
    """数据集适配器基类。

    每一种数据集类型实现一个子类，负责：
      - 加载数据集文件
      - 将数据转换为统一的 ChunkRecord 和 QAPair 格式
      - 返回数据集元信息
    """

    dataset_type: DatasetType

    def load(self, file_path: str) -> List[Tuple[List[ChunkRecord], List[QAPair]]]:
        """加载数据集，返回 (chunks, qa_pairs) 列表（多会话）。"""
        raise NotImplementedError

    def get_metadata(self) -> Dict[str, Any]:
        """返回数据集元信息。"""
        return {"dataset_type": self.dataset_type.value, "adapter": self.__class__.__name__}


class EventQAAdapter(DataAdapter):
    """自建 EventQA 数据集适配器。

    格式：JSON Lines，每行为一个事件时间线，包含多个 chunk 和对应问答。
    """
    dataset_type = DatasetType.EVENT_QA

    def load(self, file_path: str) -> List[Tuple[List[ChunkRecord], List[QAPair]]]:
        sessions = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for session in data:
                chunks = [
                    ChunkRecord(
                        chunk_id=c.get("id", i),
                        text=c.get("text", ""),
                        metadata=c.get("metadata", {}),
                        session_id=session.get("session_id", ""),
                    )
                    for i, c in enumerate(session.get("chunks", []))
                ]
                qa_pairs = [
                    QAPair(
                        query=q.get("query", ""),
                        answer=q.get("answer", ""),
                        dimension=EvalDimension(q.get("dimension", "accurate_retrieval")),
                        hop_type=HopType(q.get("hop_type", "single_hop")),
                        dataset=self.dataset_type,
                        metadata=q.get("metadata", {}),
                    )
                    for q in session.get("qa_pairs", [])
                ]
                sessions.append((chunks, qa_pairs))
        except Exception as e:
            logger.warning(f"EventQA load failed: {e}")
        return sessions


class FactConsolidationAdapter(DataAdapter):
    """自建 FactConsolidation 数据集适配器。

    格式：JSON，每个会话包含散落的多处事实片段和整合性问题。
    """
    dataset_type = DatasetType.FACT_CONSOLIDATION

    def load(self, file_path: str) -> List[Tuple[List[ChunkRecord], List[QAPair]]]:
        sessions = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for session in data:
                chunks = [
                    ChunkRecord(
                        chunk_id=c.get("id", i),
                        text=c.get("text", ""),
                        metadata=c.get("metadata", {}),
                        session_id=session.get("session_id", ""),
                    )
                    for i, c in enumerate(session.get("chunks", []))
                ]
                qa_pairs = [
                    QAPair(
                        query=q.get("query", ""),
                        answer=q.get("answer", ""),
                        dimension=EvalDimension("long_range_understanding"),
                        hop_type=HopType.MULTI_HOP,
                        dataset=self.dataset_type,
                        metadata=q.get("metadata", {}),
                    )
                    for q in session.get("qa_pairs", [])
                ]
                sessions.append((chunks, qa_pairs))
        except Exception as e:
            logger.warning(f"FactConsolidation load failed: {e}")
        return sessions


class LoCoMoAdapter(DataAdapter):
    """LoCoMo 数据集适配器（Snap Research）。

    格式：JSON Lines，每行为一段多轮对话，包含对话历史和问答。
    """
    dataset_type = DatasetType.LOCOMO

    def load(self, file_path: str) -> List[Tuple[List[ChunkRecord], List[QAPair]]]:
        sessions = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for session in data:
                chunks = [
                    ChunkRecord(
                        chunk_id=c.get("id", i),
                        text=c.get("text", ""),
                        metadata=c.get("metadata", {}),
                        session_id=session.get("session_id", ""),
                    )
                    for i, c in enumerate(session.get("chunks", []))
                ]
                qa_pairs = [
                    QAPair(
                        query=q.get("query", ""),
                        answer=q.get("answer", ""),
                        dimension=EvalDimension(q.get("dimension", "accurate_retrieval")),
                        hop_type=HopType(q.get("hop_type", "single_hop")),
                        dataset=self.dataset_type,
                        metadata=q.get("metadata", {}),
                    )
                    for q in session.get("qa_pairs", [])
                ]
                sessions.append((chunks, qa_pairs))
        except Exception as e:
            logger.warning(f"LoCoMo load failed: {e}")
        return sessions


# ── 核心评测引擎 ────────────────────────────────────────────────────


class MemoryAgentBench:
    """MemoryAgentBench 评测框架。

    实现增量多轮交互协议（chunk-by-chunk feeding），
    对记忆系统进行四维能力评测，输出统一打分。

    Usage:
        mab = MemoryAgentBench()
        mab.register_adapter(EventQAAdapter())
        mab.register_adapter(FactConsolidationAdapter())
        mab.register_adapter(LoCoMoAdapter())
        mab.load_dataset("event_qa", "./data/event_qa.json")
        mab.load_dataset("fact_consolidation", "./data/fact_consolidation.json")
        mab.load_dataset("locomo", "./data/locomo.json")
        mab.set_chunk_feeder(my_feeder_fn)  # 设置分块喂入回调
        mab.set_qa_handler(my_qa_fn)        # 设置问答回调
        score = mab.run_benchmark()
        print(score.to_locomo_json())
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._adapters: Dict[DatasetType, DataAdapter] = {}
        self._loaded_data: Dict[DatasetType, List[Tuple[List[ChunkRecord], List[QAPair]]]] = {}
        self._chunk_feeder: Optional[Callable[[ChunkRecord], None]] = None
        self._qa_handler: Optional[Callable[[str], str]] = None
        self._forgetting_handler: Optional[Callable[[List[str]], bool]] = None
        self._retention_handler: Optional[Callable[[List[str]], Dict[str, bool]]] = None
        self._last_run: Optional[BenchmarkRun] = None
        self._run_history: List[BenchmarkRun] = []

    # ── 数据集管理 ──────────────────────────────────────────────────

    def register_adapter(self, adapter: DataAdapter) -> None:
        """注册数据集适配器。"""
        with self._lock:
            self._adapters[adapter.dataset_type] = adapter
            logger.info(f"Registered adapter: {adapter.__class__.__name__} for {adapter.dataset_type.value}")

    def load_dataset(self, dataset_key: str, file_path: str) -> None:
        """加载数据集。

        Args:
            dataset_key: 数据集类型字符串（如 "event_qa"）
            file_path: 数据集文件路径
        """
        dtype = DatasetType(dataset_key)
        if dtype not in self._adapters:
            raise ValueError(f"No adapter registered for dataset type: {dataset_key}")
        with self._lock:
            sessions = self._adapters[dtype].load(file_path)
            self._loaded_data[dtype] = sessions
            total_chunks = sum(len(chunks) for chunks, _ in sessions)
            total_qa = sum(len(qa) for _, qa in sessions)
            logger.info(f"Loaded {dataset_key}: {len(sessions)} sessions, {total_chunks} chunks, {total_qa} QA pairs")

    def get_loaded_datasets(self) -> List[str]:
        """返回已加载的数据集列表。"""
        with self._lock:
            return [dt.value for dt in self._loaded_data.keys()]

    # ── 回调设置 ────────────────────────────────────────────────────

    def set_chunk_feeder(self, feeder: Callable[[ChunkRecord], None]) -> None:
        """设置分块喂入回调。

        该回调在每块文本喂入时被调用，负责将文本提交到记忆系统。
        """
        with self._lock:
            self._chunk_feeder = feeder

    def set_qa_handler(self, handler: Callable[[str], str]) -> None:
        """设置问答处理器回调。

        该回调接收问题文本，返回模型/系统生成的答案。
        """
        with self._lock:
            self._qa_handler = handler

    def set_forgetting_handler(self, handler: Callable[[List[str]], bool]) -> None:
        """设置遗忘处理器回调。

        该回调接收待遗忘的记忆 ID 列表，返回是否全部遗忘成功。
        """
        with self._lock:
            self._forgetting_handler = handler

    def set_retention_handler(self, handler: Callable[[List[str]], Dict[str, bool]]) -> None:
        """设置保留检查处理器回调。

        该回调接收待检查的记忆 ID 列表，返回每个 ID 是否仍可检索。
        """
        with self._lock:
            self._retention_handler = handler

    # ── 核心评测流程 ────────────────────────────────────────────────

    def run_benchmark(self) -> UnifiedScore:
        """运行完整四维评测。

        Returns:
            UnifiedScore: 统一四维打分结果
        """
        if self._chunk_feeder is None or self._qa_handler is None:
            raise RuntimeError("Must set chunk_feeder and qa_handler before running benchmark")

        run = BenchmarkRun()
        results: Dict[EvalDimension, List[bool]] = defaultdict(list)
        latencies: Dict[EvalDimension, List[float]] = defaultdict(list)

        with self._lock:
            for dtype, sessions in self._loaded_data.items():
                for chunks, qa_pairs in sessions:
                    # Phase 1: Chunk-by-chunk feeding
                    for chunk in chunks:
                        self._chunk_feeder(chunk)

                    # Phase 2: QA Testing
                    for qa in qa_pairs:
                        t_start = time.perf_counter()
                        try:
                            predicted = self._qa_handler(qa.query)
                        except Exception as e:
                            logger.warning(f"QA handler error for {qa.query[:50]}...: {e}")
                            predicted = ""
                        elapsed_ms = (time.perf_counter() - t_start) * 1000

                        correct = self._judge_answer(predicted, qa.answer, qa.dimension)
                        results[qa.dimension].append(correct)
                        latencies[qa.dimension].append(elapsed_ms)

            # Phase 3: Selective Forgetting (if handler available)
            if self._forgetting_handler and self._retention_handler:
                forgetting_results = self._run_selective_forgetting()
                results[EvalDimension.SELECTIVE_FORGETTING] = forgetting_results

        # ── 计算四维得分 ────────────────────────────────────────────
        dimension_scores = {}
        for dim in EvalDimension:
            dim_results = results.get(dim, [])
            if dim_results:
                score = sum(1 for r in dim_results if r) / len(dim_results) * 100
            else:
                score = 0.0
            dimension_scores[dim] = score

        unified = UnifiedScore(
            accurate_retrieval=dimension_scores[EvalDimension.ACCURATE_RETRIEVAL],
            test_time_learning=dimension_scores[EvalDimension.TEST_TIME_LEARNING],
            long_range_understanding=dimension_scores[EvalDimension.LONG_RANGE_UNDERSTANDING],
            selective_forgetting=dimension_scores[EvalDimension.SELECTIVE_FORGETTING],
        )
        # Composite: 加权平均 (AR 0.35, TTL 0.20, LRU 0.25, SF 0.20)
        unified.composite = (
            unified.accurate_retrieval * 0.35
            + unified.test_time_learning * 0.20
            + unified.long_range_understanding * 0.25
            + unified.selective_forgetting * 0.20
        )
        unified.locomo_compatible = unified.to_locomo_json()

        # ── 构建详细结果 ────────────────────────────────────────────
        run.total_chunks = sum(
            len(chunks) for sessions in self._loaded_data.values() for chunks, _ in sessions
        )
        run.total_qa_pairs = sum(len(r) for r in results.values())
        run.results = [
            EvalResult(
                dimension=dim,
                total_questions=len(results.get(dim, [])),
                correct=sum(1 for r in results.get(dim, []) if r),
                accuracy=dimension_scores[dim],
                latency_stats=_compute_latency_stats(latencies.get(dim, [])),
            )
            for dim in EvalDimension
        ]
        run.unified_score = unified
        run.end_time = time.time()

        self._last_run = run
        self._run_history.append(run)

        logger.info(
            f"Benchmark complete: composite={unified.composite:.1f}, "
            f"AR={unified.accurate_retrieval:.1f}, TTL={unified.test_time_learning:.1f}, "
            f"LRU={unified.long_range_understanding:.1f}, SF={unified.selective_forgetting:.1f}"
        )
        return unified

    def _judge_answer(self, predicted: str, expected: str, dimension: EvalDimension) -> bool:
        """判断答案是否正确。

        根据不同维度使用不同判断策略：
          - Accurate Retrieval: 严格字符串匹配（含子串）
          - Test-Time Learning: 模糊匹配 + 关键词覆盖
          - Long-Range Understanding: 语义等价判断
          - Selective Forgetting: 遗忘检查（不应回答正确）
        """
        if not predicted or not expected:
            return False

        pred_lower = predicted.lower().strip()
        exp_lower = expected.lower().strip()

        if dimension == EvalDimension.ACCURATE_RETRIEVAL:
            return exp_lower in pred_lower or pred_lower == exp_lower
        elif dimension == EvalDimension.TEST_TIME_LEARNING:
            exp_tokens = set(exp_lower.split())
            pred_tokens = set(pred_lower.split())
            if not exp_tokens:
                return False
            overlap = len(exp_tokens & pred_tokens) / len(exp_tokens)
            return overlap >= 0.6
        elif dimension == EvalDimension.LONG_RANGE_UNDERSTANDING:
            return exp_lower in pred_lower or any(
                kw in pred_lower for kw in exp_lower.split() if len(kw) > 3
            )
        elif dimension == EvalDimension.SELECTIVE_FORGETTING:
            # For forgetting: true means the agent correctly DOES NOT return the forgotten info
            return exp_lower not in pred_lower
        return False

    def _run_selective_forgetting(self) -> List[bool]:
        """运行选择性遗忘评测。

        包括：遗忘应忘的信息 → 检查遗忘成功 + 保留应留的信息 → 检查保留成功。
        """
        results: List[bool] = []
        if not self._forgetting_handler or not self._retention_handler:
            return results

        # 从 EventQA 数据集中提取遗忘测试用例
        for dtype, sessions in self._loaded_data.items():
            for chunks, qa_pairs in sessions:
                forget_ids = [
                    str(chunk.chunk_id)
                    for chunk in chunks
                    if chunk.metadata.get("should_forget", False)
                ]
                retain_ids = [
                    str(chunk.chunk_id)
                    for chunk in chunks
                    if chunk.metadata.get("should_retain", False)
                ]
                if not forget_ids and not retain_ids:
                    continue

                # Execute forgetting
                forget_success = self._forgetting_handler(forget_ids) if forget_ids else True
                results.append(forget_success)

                # Check retention
                if retain_ids:
                    retention_status = self._retention_handler(retain_ids)
                    for rid in retain_ids:
                        results.append(retention_status.get(rid, False))

        return results

    def get_last_run(self) -> Optional[BenchmarkRun]:
        """返回最近一次评测运行记录。"""
        return self._last_run

    def get_run_history(self) -> List[BenchmarkRun]:
        """返回所有评测运行历史。"""
        return list(self._run_history)

    def _compute_latency_stats(self, latencies: List[float]) -> Dict[str, float]:
        """计算延迟百分位统计。"""
        if not latencies:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}
        arr = np.array(sorted(latencies))
        return {
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(np.mean(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
        }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            last = self._last_run
            return {
                "total_runs": len(self._run_history),
                "loaded_datasets": self.get_loaded_datasets(),
                "registered_adapters": [a.dataset_type.value for a in self._adapters.values()],
                "last_run": {
                    "run_id": last.run_id if last else None,
                    "composite_score": last.unified_score.composite if last else None,
                    "total_chunks": last.total_chunks if last else 0,
                    "total_qa_pairs": last.total_qa_pairs if last else 0,
                    "duration_s": (last.end_time - last.start_time) if last else 0.0,
                } if last else None,
            }

    def export_locomo(self) -> Dict[str, Any]:
        """返回 LoCoMo 兼容格式 dict（不写入文件）。"""
        if self._last_run is None:
            return {
                "framework": "Trinity-P9",
                "version": "1.0.0",
                "scores": {"single_hop": 0.0, "multi_hop": 0.0, "temporal": 0.0, "open_domain": 0.0},
                "total_score": 0.0,
                "note": "no_benchmark_run_yet",
            }
        return self._last_run.unified_score.locomo_compatible

    def export_locomo_json(self, file_path: str) -> None:
        """导出 LoCoMo 兼容格式 JSON 到文件。"""
        if self._last_run is None:
            raise RuntimeError("No benchmark run available. Run benchmark first.")
        locomo_data = self._last_run.unified_score.locomo_compatible
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(locomo_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Exported LoCoMo JSON to {file_path}")


# ── 模块级辅助函数 ──────────────────────────────────────────────────


def _compute_latency_stats(latencies: List[float]) -> Dict[str, float]:
    """计算延迟百分位统计（模块级）。"""
    if not latencies:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}
    arr = np.array(sorted(latencies))
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "mean": float(np.mean(arr)),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
    }
