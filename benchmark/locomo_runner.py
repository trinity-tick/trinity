#!/usr/bin/env python3
"""
LoCoMo Benchmark Runner — P2-1 标准基准评测模块

LoCoMo (Long Context Memory) 标准评测：
  基于多会话长对话场景，评估记忆系统的 Recall@K / Precision@K / MRR。

评测类别（对齐 LoCoMo 论文）:
  - single-session-user      单会话用户事实回忆
  - single-session-assistant 单会话助手回复回忆
  - multi-session-reasoning  跨会话推理
  - temporal-reasoning       时间线推理
  - knowledge-update         知识更新检测
  - preference-tracking      偏好追踪

Usage:
  python locomo_runner.py \
      --test-set benchmark/locomo_test_set.json \
      --output benchmark/locomo_report.md

  或作为模块导入:
  from benchmark.locomo_runner import LoCoMoEvaluator

  evaluator = LoCoMoEvaluator()
  evaluator.run_eval(retriever, "benchmark/locomo_test_set.json", top_k=5)
  evaluator.generate_report("benchmark/locomo_report.md")
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 常量 ────────────────────────────────────────────────────────────────

CATEGORIES = [
    "single-session-user",
    "single-session-assistant",
    "multi-session-reasoning",
    "temporal-reasoning",
    "knowledge-update",
    "preference-tracking",
]

CATEGORY_LABELS_ZH = {
    "single-session-user": "单会话用户事实",
    "single-session-assistant": "单会话助手回复",
    "multi-session-reasoning": "跨会话推理",
    "temporal-reasoning": "时间线推理",
    "knowledge-update": "知识更新检测",
    "preference-tracking": "偏好追踪",
}

DEFAULT_TEST_SET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "locomo_test_set.json",
)


# ============================================================
# 数据集生成器 — 模拟 LoCoMo 格式的多会话长对话评测集
# ============================================================

def generate_locomo_test_set(
    output_path: str,
    num_sessions: int = 5,
    turns_per_session: int = 8,
    questions_per_session: int = 4,
    total_questions: int = 50,
    seed: int = 42,
) -> str:
    """生成模拟的 LoCoMo 评测数据集。

    LoCoMo 的核心场景：用户与助手跨越多个会话进行交互，
    评测记忆系统在跨会话、知识更新、时间推理等维度上的检索能力。

    参数:
        output_path: 输出 JSON 文件路径。
        num_sessions: 会话数量。
        turns_per_session: 每会话对话轮数。
        questions_per_session: 每会话问题数。
        total_questions: 总问题数（questions_per_session 优先）。
        seed: 随机种子。

    返回:
        输出文件路径。
    """
    random.seed(seed)

    # ── 场景模板 ────────────────────────────────────────────
    # 模拟真实的多会话对话：工作汇报、旅行规划、技术讨论、购物决策、健康咨询

    session_templates = [
        {
            "session_id": "s1",
            "title": "Q3 工作汇报准备",
            "persona": "项目经理 Alex",
            "turns": [
                ("用户", "我们要准备 Q3 的工作汇报了，上周华东区的毛利率是多少？"),
                ("助手", "根据上周数据，华东区毛利率为 38.2%，环比上升 1.5 个百分点。"),
                ("用户", "那华南区呢？我记得他们上个月遇到了供应链问题。"),
                ("助手", "华南区上周毛利率为 32.7%，确实受芯片短缺影响环比下降 3.1%。建议在汇报中突出供应链优化方案。"),
                ("用户", "好的，帮我把这个优化方案的关键点列出来。"),
                ("助手", "方案关键点：1) 建立双供应商体系 2) 安全库存从 2 周提升到 4 周 3) 与物流商协商优先配送协议。"),
                ("用户", "另外，华东区新签的客户「星辰科技」合同金额是多少？"),
                ("助手", "星辰科技的合同金额为 480 万元，分三期支付，首期 200 万已到账。"),
            ],
        },
        {
            "session_id": "s2",
            "title": "产品路线图讨论",
            "persona": "产品总监 Sarah",
            "turns": [
                ("用户", "下周的产品评审会要确定 v6.8 的发布范围，目前已经完成了哪些功能？"),
                ("助手", "v6.8 已完成的模块：Federated Memory Query (P1-4)、增量 KG 更新 (P1-3)、主动预取 (P1-1)。正在开发：自主分页 (P1-2)、Web Dashboard (P2-3)。"),
                ("用户", "P1-2 的自主分页是谁在负责？预计什么时候提测？"),
                ("助手", "P1-2 自主分页由 David 负责，预计 8 月 15 日提测。风险点：上下文窗口预算算法需要更多边界测试。"),
                ("用户", "需要加资源吗？David 上周不是说内存管理模块的衰减算法有 bug？"),
                ("助手", "衰减算法的 bug 已于 8 月 3 日修复。目前不需要加资源，但建议从 QA 组借调一人做专项测试。"),
                ("用户", "这个 bug 的修复方案是什么？会不会影响已有的记忆数据？"),
                ("助手", "修复方案：将 ImportanceDecay 的半衰期计算从 float32 改为 float64，修复了累积舍入误差。不会影响已有数据，仅在重新计算衰减时应用新精度。"),
            ],
        },
        {
            "session_id": "s3",
            "title": "客户需求变更处理",
            "persona": "客户成功经理 Lisa",
            "turns": [
                ("用户", "星辰科技的陈总刚打电话来，说他们希望合同增加一个数据导出的定制功能。"),
                ("助手", "数据导出功能复杂度评估：约 15 人天，涉及 CSV/Excel/PDF 三种格式。是否需要在现有合同框架内处理？"),
                ("用户", "是的，430 万的合同额度内消化。他们最迟什么时候要？"),
                ("助手", "根据之前的沟通记录，星辰科技的上线日期是 9 月 15 日，数据导出功能需要在 9 月 1 日前交付。"),
                ("用户", "时间很紧。他们之前提到的 API 集成需求怎么样了？"),
                ("助手", "API 集成需求已于 7 月 25 日交付 v1.0，目前运行稳定。陈总对 API 文档的清晰度表示满意。"),
                ("用户", "那就好。把数据导出的排期发给 David，优先级 P0。"),
                ("助手", "已记录：数据导出功能 P0 优先级，目标 9 月 1 日交付，分配到 David。同时通知了陈总预计排期。"),
            ],
        },
        {
            "session_id": "s4",
            "title": "技术架构评审",
            "persona": "架构师 David",
            "turns": [
                ("用户", "联邦查询模块的 RRF 融合算法，我们需要验证在大规模 Agent 下的性能。有多少个 Agent 参与了上次的压测？"),
                ("助手", "上次压测（8 月 1 日）共有 12 个 Agent 参与，RRF 融合延迟 P99 为 87ms。目标是在 50 个 Agent 下保持 P99 < 200ms。"),
                ("用户", "当前 RRF 排名融合的 k 参数用的是多少？"),
                ("助手", "当前默认 rrf_k=60，这是 LoCoMo 论文推荐的标准值。我们在上次 benchmark 中对比了 k=30/60/90，k=60 时 Recall@5 最高（0.874）。"),
                ("用户", "好的，那增量 KG 更新的 diff 算法，对于 10 万节点的图，compute_diff 延迟是多少？"),
                ("助手", "10 万节点下 compute_diff 延迟 P50=1.2s, P95=3.8s。瓶颈在 properties 深度比较，已计划用属性哈希指纹优化到 P95<1s。"),
            ],
        },
        {
            "session_id": "s5",
            "title": "季度复盘与规划",
            "persona": "CTO 王总",
            "turns": [
                ("用户", "回顾一下，我们 P1 阶段完成了哪些优化项？"),
                ("助手", "P1 完成项：P1-1 主动预取模块、P1-2 自主分页模块、P1-3 增量 KG 更新、P1-4 联邦记忆查询。全部已通过验证。"),
                ("用户", "P2 目前的进度呢？"),
                ("助手", "P2 进度：P2-1 LoCoMo 基准评测（进行中）、P2-2 记忆压缩评估（待开始）、P2-3 Web Dashboard（待开始）、P2-4 记忆回放训练（待开始）、P2-5 流式摄入（待开始）。"),
                ("用户", "P1 的整体投入产出比怎么样？"),
                ("助手", "P1 总投入 22 人天，产出：Recall@5 从 0.72 提升到 0.87，P99 延迟从 450ms 降到 180ms，记忆换入换出命中率 91%。RoI 非常正面。"),
                ("用户", "那星辰科技的项目，他们满意吗？"),
                ("助手", "星辰科技项目：合同 480 万，已交付 API 集成和核心平台，数据导出功能排期到 9 月 1 日。陈总对进度表示满意，正在洽谈二期合作。"),
            ],
        },
    ]

    # ── 生成问题 ────────────────────────────────────────────

    question_templates: List[Tuple[str, str, str, str]] = []

    # 单会话用户事实
    for sess in session_templates:
        for turn in sess["turns"]:
            if turn[0] == "用户":
                question_templates.append((
                    f"在{sess['title']}的对话中，用户问了什么问题？",
                    turn[1],
                    "single-session-user",
                    sess["session_id"],
                ))

    # 单会话助手回复
    for sess in session_templates:
        for i, turn in enumerate(sess["turns"]):
            if turn[0] == "助手" and len(turn[1]) > 30:
                question_templates.append((
                    f"在{sess['title']}中，助手回复了什么内容？",
                    turn[1][:80] + "...",  # truncated as key phrase
                    "single-session-assistant",
                    sess["session_id"],
                ))

    # 跨会话推理
    cross_session_questions = [
        ("星辰科技的项目总合同金额是多少？", "480 万元", "multi-session-reasoning", "s1,s3,s5"),
        ("涉及芯片短缺问题的会话有哪些？", "Q3 工作汇报准备（华南区供应链问题）", "multi-session-reasoning", "s1"),
        ("David 负责了哪些 P1/P2 优化项？", "P1-2 自主分页、P0 数据导出", "multi-session-reasoning", "s2,s3,s4"),
        ("联邦查询模块的 RRF 算法参数 k 当前取值是多少，Recall@5 是多少？", "k=60, Recall@5=0.874", "multi-session-reasoning", "s4"),
        ("P1 阶段总共完成了哪四个模块？", "主动预取、自主分页、增量KG更新、联邦记忆查询", "multi-session-reasoning", "s5"),
        ("衰减算法的 bug 是什么时候修复的，修复方案是什么？", "8月3日修复，float32改float64消除累积舍入误差", "multi-session-reasoning", "s2"),
        ("星辰科技 API 集成是什么时候交付的？", "7 月 25 日交付 v1.0", "multi-session-reasoning", "s2,s3"),
        ("华南区毛利率下降的原因是什么？", "芯片短缺导致供应链问题，环比下降 3.1%", "multi-session-reasoning", "s1"),
    ]
    question_templates.extend([(q, a, c, sid) for q, a, c, sid in cross_session_questions])

    # 时间线推理
    temporal_questions = [
        ("P1-2 自主分页的提测日期是什么时候？", "8 月 15 日", "temporal-reasoning", "s2"),
        ("数据导出功能的交付截止日期是什么时候？", "9 月 1 日", "temporal-reasoning", "s3"),
        ("上一次联邦查询压测是什么时候？", "8 月 1 日", "temporal-reasoning", "s4"),
        ("星辰科技的上线日期是哪天？", "9 月 15 日", "temporal-reasoning", "s3"),
        ("v6.8 的衰减算法 bug 修复日期？", "8 月 3 日", "temporal-reasoning", "s2"),
    ]
    question_templates.extend([(q, a, c, sid) for q, a, c, sid in temporal_questions])

    # 知识更新
    knowledge_update_questions = [
        ("星辰科技的合同金额最初是 480 万还是 430 万？", "480 万为合同总金额，430 万为合同额度（需在额度内消化新增功能）", "knowledge-update", "s1,s3"),
        ("v6.8 的 P1-2 模块开发状态是已完成还是进行中？", "进行中，预计 8 月 15 日提测", "knowledge-update", "s2"),
        ("David 负责的衰减算法 bug 是否已修复？", "已于 8 月 3 日修复", "knowledge-update", "s2"),
    ]
    question_templates.extend([(q, a, c, sid) for q, a, c, sid in knowledge_update_questions])

    # 偏好追踪
    preference_questions = [
        ("陈总对哪项交付最满意？", "API 文档的清晰度", "preference-tracking", "s3"),
        ("用户更倾向于用哪种方式处理新增功能？", "在现有合同额度内消化而非新增合同", "preference-tracking", "s3"),
        ("CTO 王总对 P1 的投入产出比评价是什么？", "非常正面，RoI 很高", "preference-tracking", "s5"),
    ]
    question_templates.extend([(q, a, c, sid) for q, a, c, sid in preference_questions])

    # ── 随机采样 ────────────────────────────────────────────
    random.shuffle(question_templates)
    selected = question_templates[:total_questions]

    # ── 构建 LoCoMo 格式 ─────────────────────────────────────

    dataset = {
        "metadata": {
            "benchmark": "LoCoMo",
            "version": "1.0",
            "description": "模拟多会话长对话记忆评测数据集",
            "generated_at": datetime.now().isoformat(),
            "num_sessions": num_sessions,
            "turns_per_session": turns_per_session,
            "total_turns": sum(len(s["turns"]) for s in session_templates),
            "total_questions": len(selected),
            "categories": CATEGORIES,
        },
        "sessions": [
            {
                "session_id": s["session_id"],
                "title": s["title"],
                "persona": s["persona"],
                "turns": [
                    {"speaker": t[0], "text": t[1], "turn_id": f"{s['session_id']}_t{j}"}
                    for j, t in enumerate(s["turns"])
                ],
            }
            for s in session_templates
        ],
        "questions": [
            {
                "question_id": f"q{i}",
                "question": item[0],
                "answer": item[1],
                "category": item[2],
                "session_ids": item[3],
            }
            for i, item in enumerate(selected)
        ],
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    print(f"[LoCoMo] 已生成评测集: {output_path}")
    print(f"  - 会话: {num_sessions} | 总轮次: {dataset['metadata']['total_turns']}")
    print(f"  - 问题: {len(selected)} 题")
    cat_counts: Dict[str, int] = defaultdict(int)
    for q in selected:
        cat_counts[q[2]] += 1
    for cat in CATEGORIES:
        print(f"    {CATEGORY_LABELS_ZH.get(cat, cat)}: {cat_counts[cat]}")

    return output_path


# ============================================================
# LoCoMoEvaluator — 评测引擎
# ============================================================

class LoCoMoEvaluator:
    """LoCoMo 标准基准评测器。

    评测 Recall@K / Precision@K / MRR 三项核心指标，
    按类别分组输出详细报告。

    使用方式::

        evaluator = LoCoMoEvaluator()
        results = evaluator.run_eval(
            retriever=my_retriever,
            test_set_path="benchmark/locomo_test_set.json",
            top_k=5,
        )
        evaluator.generate_report("benchmark/locomo_report.md")
    """

    def __init__(self):
        self._results: Dict[str, Any] = {}
        self._category_stats: Dict[str, Dict[str, float]] = {}
        self._detail_rows: List[Dict[str, Any]] = []
        self._elapsed: float = 0.0

    # ── 主评测入口 ──────────────────────────────────────────

    def run_eval(
        self,
        retriever: Any,
        test_set_path: str,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """运行 LoCoMo 基准评测。

        参数:
            retriever: 记忆检索器实例，必须有 search(query, top_k) 方法。
            test_set_path: LoCoMo 评测集 JSON 文件路径。
            top_k: Recall@K 和 Precision@K 的 K 值。

        返回:
            评测结果字典，包含 overall/ per-category/ per-question 三个层级。
        """
        # 加载评测集
        if not os.path.exists(test_set_path):
            raise FileNotFoundError(
                f"评测集文件不存在: {test_set_path}\n"
                f"请先运行 generate_locomo_test_set() 生成数据集。"
            )

        with open(test_set_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)

        questions = dataset["questions"]
        sessions = dataset.get("sessions", [])

        # 构建 ground truth 索引：answer → session 文本映射
        session_texts: Dict[str, str] = {}
        for sess in sessions:
            texts = []
            for turn in sess.get("turns", []):
                texts.append(f"[{turn['speaker']}] {turn['text']}")
            session_texts[sess["session_id"]] = "\n".join(texts)

        # ── 逐题评测 ──────────────────────────────────────
        self._detail_rows = []
        cat_scores: Dict[str, Dict[str, List[float]]] = {
            cat: {"recall": [], "precision": [], "mrr": []} for cat in CATEGORIES
        }

        t_start = time.perf_counter()

        for i, q in enumerate(questions):
            qid = q.get("question_id", f"q{i}")
            question = q["question"]
            ground_truth = q["answer"]
            category = q.get("category", "single-session-user")

            # 检索
            try:
                retrieved = retriever.search(question, top_k=top_k)
            except Exception as e:
                print(f"[LoCoMo] 检索失败 qid={qid}: {e}")
                retrieved = []

            # 评分
            try:
                scores = self._score_answer(retrieved, ground_truth, top_k)
            except Exception as e:
                print(f"[LoCoMo] 评分失败 qid={qid}: {e}")
                scores = {"recall": 0.0, "precision": 0.0, "mrr": 0.0}

            # 记录
            row = {
                "question_id": qid,
                "question": question,
                "answer": ground_truth,
                "category": category,
                "top_k": top_k,
                "num_retrieved": len(retrieved),
                "recall": scores["recall"],
                "precision": scores["precision"],
                "mrr": scores["mrr"],
            }
            self._detail_rows.append(row)

            if category in cat_scores:
                cat_scores[category]["recall"].append(scores["recall"])
                cat_scores[category]["precision"].append(scores["precision"])
                cat_scores[category]["mrr"].append(scores["mrr"])

        self._elapsed = time.perf_counter() - t_start

        # ── 汇总 ──────────────────────────────────────────
        all_recall = [r["recall"] for r in self._detail_rows]
        all_precision = [r["precision"] for r in self._detail_rows]
        all_mrr = [r["mrr"] for r in self._detail_rows]

        def safe_mean(arr: List[float]) -> float:
            return sum(arr) / len(arr) if arr else 0.0

        self._results = {
            "benchmark": "LoCoMo",
            "test_set": test_set_path,
            "top_k": top_k,
            "num_questions": len(questions),
            "elapsed_seconds": round(self._elapsed, 3),
            "overall": {
                f"Recall@{top_k}": round(safe_mean(all_recall), 4),
                f"Precision@{top_k}": round(safe_mean(all_precision), 4),
                "MRR": round(safe_mean(all_mrr), 4),
            },
        }

        self._category_stats = {}
        for cat in CATEGORIES:
            cs = cat_scores[cat]
            if cs["recall"]:
                self._category_stats[cat] = {
                    f"Recall@{top_k}": round(safe_mean(cs["recall"]), 4),
                    f"Precision@{top_k}": round(safe_mean(cs["precision"]), 4),
                    "MRR": round(safe_mean(cs["mrr"]), 4),
                    "count": len(cs["recall"]),
                }

        self._results["by_category"] = self._category_stats
        self._results["details"] = self._detail_rows

        # 打印摘要
        ov = self._results["overall"]
        print(
            f"[LoCoMo] 评测完成: {len(questions)} 题, "
            f"Recall@{top_k}={ov[f'Recall@{top_k}']:.4f}, "
            f"Precision@{top_k}={ov[f'Precision@{top_k}']:.4f}, "
            f"MRR={ov['MRR']:.4f}, "
            f"耗时 {self._elapsed:.1f}s"
        )

        return self._results

    # ── 评分逻辑 ────────────────────────────────────────────

    def _score_answer(
        self,
        retrieved: List[Any],
        ground_truth: str,
        top_k: int,
    ) -> Dict[str, float]:
        """计算 Recall@K / Precision@K / MRR。

        Recall@K:     ground_truth 中的关键词是否在 top-K 结果中出现。
        Precision@K:  top-K 结果中有多少与 ground_truth 相关。
        MRR:          Reciprocal Rank — 第一个相关结果的排名的倒数。

        参数:
            retrieved: 检索器返回的结果列表（每条应为 dict 或带 content/text 属性）。
            ground_truth: 标准答案文本。
            top_k: K 值。

        返回:
            包含 recall / precision / mrr 的字典。
        """
        gt_keywords = self._extract_keywords(ground_truth)

        relevant_at_k = 0
        reciprocal_rank = 0.0

        for rank_idx, doc in enumerate(retrieved[:top_k]):
            doc_text = self._get_doc_text(doc)
            is_relevant = self._is_relevant(doc_text, gt_keywords)

            if is_relevant:
                relevant_at_k += 1
                if reciprocal_rank == 0.0:
                    reciprocal_rank = 1.0 / (rank_idx + 1)

        recall = 1.0 if relevant_at_k > 0 else 0.0  # 至少命中一个关键词即算召回
        precision = relevant_at_k / min(len(retrieved), top_k) if retrieved else 0.0
        mrr = reciprocal_rank

        return {
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "mrr": round(mrr, 4),
        }

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        """从答案文本中提取关键 token 用于匹配。"""
        import re

        # 中英文数字混合分词
        tokens: List[str] = []

        # 提取中文词组（2-4 字）
        cn_words = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
        tokens.extend(cn_words)

        # 提取英文单词/数字
        en_words = re.findall(r"[A-Za-z0-9._%+-]+", text)
        tokens.extend(en_words)

        # 专有名词合并
        merged: List[str] = []
        skip = set()
        for i, tok in enumerate(tokens):
            if i in skip:
                continue
            # 数字+单位合并 (e.g. "480" + "万")
            if i + 1 < len(tokens) and re.match(r"^\d+", tok):
                next_tok = tokens[i + 1]
                if next_tok in ("万", "亿", "月", "日", "年", "人天", "ms", "s", "%"):
                    merged.append(tok + next_tok)
                    skip.add(i + 1)
                    continue
            merged.append(tok)

        return [t.lower() for t in merged if len(t) >= 2]

    @staticmethod
    def _get_doc_text(doc: Any) -> str:
        """从检索文档中提取文本。"""
        if isinstance(doc, dict):
            return doc.get("content", doc.get("text", ""))
        if hasattr(doc, "content"):
            return str(getattr(doc, "content", ""))
        if hasattr(doc, "text"):
            return str(getattr(doc, "text", ""))
        return str(doc)

    @staticmethod
    def _is_relevant(doc_text: str, gt_keywords: List[str]) -> bool:
        """判断检索文档是否与 ground truth 相关。"""
        doc_lower = doc_text.lower()
        # 至少命中 60% 的关键词或至少 2 个关键词
        hits = sum(1 for kw in gt_keywords if kw in doc_lower)
        threshold = max(2, len(gt_keywords) * 0.6) if gt_keywords else 1
        return hits >= threshold

    # ── 报告生成 ────────────────────────────────────────────

    def generate_report(self, output_path: str) -> str:
        """生成 Markdown 格式的 LoCoMo 评测报告。

        参数:
            output_path: 输出 Markdown 文件路径。

        返回:
            输出文件路径。
        """
        if not self._results:
            raise RuntimeError("请先运行 run_eval() 再进行报告生成。")

        r = self._results
        ov = r["overall"]
        top_k = r["top_k"]
        nq = r["num_questions"]

        lines: List[str] = []
        lines.append("# LoCoMo Benchmark 评测报告")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**评测集**: `{r['test_set']}`")
        lines.append(f"**问题总数**: {nq}")
        lines.append(f"**Top-K**: {top_k}")
        lines.append(f"**总耗时**: {r['elapsed_seconds']:.2f}s")
        lines.append("")

        # ── 总体指标 ──────────────────────────────────────
        lines.append("## 总体指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| Recall@{top_k} | **{ov[f'Recall@{top_k}']:.4f}** |")
        lines.append(f"| Precision@{top_k} | **{ov[f'Precision@{top_k}']:.4f}** |")
        lines.append(f"| MRR | **{ov['MRR']:.4f}** |")
        lines.append("")

        # ── 按类别 ────────────────────────────────────────
        lines.append("## 按类别明细")
        lines.append("")
        lines.append("| 类别 | 题目数 | Recall@K | Precision@K | MRR |")
        lines.append("|------|--------|----------|-------------|-----|")
        for cat in CATEGORIES:
            cs = self._category_stats.get(cat, {})
            if not cs:
                continue
            label = CATEGORY_LABELS_ZH.get(cat, cat)
            lines.append(
                f"| {label} | {cs['count']} | "
                f"{cs[f'Recall@{top_k}']:.4f} | "
                f"{cs[f'Precision@{top_k}']:.4f} | "
                f"{cs['MRR']:.4f} |"
            )
        lines.append("")

        # ── 题目详情（Top-5 最佳 + Top-5 最差）───────────
        lines.append("## 题目详情")
        lines.append("")

        # 按 recall 排序
        sorted_rows = sorted(self._detail_rows, key=lambda x: (-x["recall"], -x["mrr"]))

        # Best 5
        lines.append("### 最佳 5 题")
        lines.append("")
        lines.append("| ID | 问题 | 答案 | 类别 | Recall | MRR |")
        lines.append("|----|------|------|------|--------|-----|")
        for row in sorted_rows[:5]:
            lines.append(
                f"| {row['question_id']} | "
                f"{row['question'][:40]} | "
                f"{row['answer'][:30]} | "
                f"{CATEGORY_LABELS_ZH.get(row['category'], row['category'])} | "
                f"{row['recall']:.4f} | "
                f"{row['mrr']:.4f} |"
            )
        lines.append("")

        # Worst 5
        lines.append("### 最差 5 题")
        lines.append("")
        lines.append("| ID | 问题 | 答案 | 类别 | Recall | MRR |")
        lines.append("|----|------|------|------|--------|-----|")
        for row in reversed(sorted_rows[-5:]):
            lines.append(
                f"| {row['question_id']} | "
                f"{row['question'][:40]} | "
                f"{row['answer'][:30]} | "
                f"{CATEGORY_LABELS_ZH.get(row['category'], row['category'])} | "
                f"{row['recall']:.4f} | "
                f"{row['mrr']:.4f} |"
            )
        lines.append("")

        # ── 分析 ──────────────────────────────────────────
        lines.append("## 分析")
        lines.append("")

        # 找出最弱类别
        weakest_cat = None
        weakest_score = 1.0
        for cat, cs in self._category_stats.items():
            score = cs.get(f"Recall@{top_k}", 1.0)
            if score < weakest_score:
                weakest_score = score
                weakest_cat = cat

        if weakest_cat:
            lines.append(
                f"- **最弱类别**: {CATEGORY_LABELS_ZH.get(weakest_cat, weakest_cat)} "
                f"(Recall@{top_k}={weakest_score:.4f}) — 建议针对性优化对应记忆检索策略"
            )

        # RRF 参数建议
        rrf_note = ""
        if ov[f"Recall@{top_k}"] < 0.7:
            rrf_note = (
                "整体 Recall 偏低，建议：(1) 增大 RRF k 参数；"
                "(2) 增加检索 top_k；"
                "(3) 启用 query expansion 多路召回。"
            )
        elif ov[f"Recall@{top_k}"] < 0.85:
            rrf_note = (
                "Recall 处于中等水平，建议：(1) 针对弱类别增加主动预取；"
                "(2) 优化关键词分词粒度。"
            )
        else:
            rrf_note = "Recall 表现良好，可按当前配置继续 P2 后续优化。"
        lines.append(f"- **建议**: {rrf_note}")
        lines.append("")

        lines.append("---")
        lines.append("*报告由 LoCoMoEvaluator (P2-1) 自动生成*")
        lines.append("")

        report_text = "\n".join(lines)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report_text)

        print(f"[LoCoMo] 报告已输出: {output_path}")
        return output_path


# ============================================================
# 自评运行入口
# ============================================================

class MockRetriever:
    """模拟检索器 — 基于 session text 的简单关键词检索。"""

    def __init__(self, sessions: List[Dict[str, Any]]):
        self._docs: List[Dict[str, str]] = []
        for sess in sessions:
            for turn in sess.get("turns", []):
                self._docs.append({
                    "content": f"[{turn['speaker']}] {turn['text']}",
                    "session_id": sess["session_id"],
                })

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, str]]:
        query_lower = query.lower()
        scored: List[Tuple[float, Dict[str, str]]] = []

        for doc in self._docs:
            content_lower = doc["content"].lower()
            # 简单重叠词数评分
            words = query_lower.split()
            score = sum(1 for w in words if w in content_lower)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: -x[0])
        return [doc for _, doc in scored[:top_k]]


def main():
    """自评入口：生成数据集 → 运行评测 → 输出报告。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Step 1: 生成/加载数据集
    test_set_path = os.path.join(script_dir, "locomo_test_set.json")
    if not os.path.exists(test_set_path):
        print("[LoCoMo] 未找到现有数据集，正在生成...")
        generate_locomo_test_set(test_set_path, total_questions=50)
    else:
        print(f"[LoCoMo] 使用现有数据集: {test_set_path}")

    # Step 2: 加载数据集，构建 MockRetriever
    with open(test_set_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    retriever = MockRetriever(dataset.get("sessions", []))

    # Step 3: 运行评测
    evaluator = LoCoMoEvaluator()
    evaluator.run_eval(retriever, test_set_path, top_k=5)

    # Step 4: 生成报告
    report_path = os.path.join(script_dir, "locomo_report.md")
    evaluator.generate_report(report_path)

    print(f"\n[LoCoMo] 自评完成，报告: {report_path}")


if __name__ == "__main__":
    main()
