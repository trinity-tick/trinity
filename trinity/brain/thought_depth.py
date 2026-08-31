# -*- coding: utf-8 -*-
"""trinity/brain/thought_depth.py — 思考深度调节（EXECUTION 292，大脑化）。

借鉴 BIFROST（2026：Adaptive Decision Framework for Regulating
Depth of Thought）——自适应调节思考深度（任务复杂度×资源 →
浅/中/深——不浪费不浅尝）。

与快慢决策（248 路径选择）互补：快慢=路径；本模块=深度调节。
Trinity 现在：
  regulate_depth(complexity, resources): 深度调节（动态）
"""
import os
import sys
import json


def regulate_depth(task_complexity: float = 0.5, resources: float = 0.5) -> dict:
    """思考深度调节：复杂度 × 资源 → 深度档。"""
    # 深度 = 复杂度 0.6 + 资源 0.4
    depth_score = task_complexity * 0.6 + resources * 0.4
    if depth_score >= 0.7:
        depth = "deep"
        detail = "多轮推理+证据检索+验证"
    elif depth_score >= 0.4:
        depth = "medium"
        detail = "标准推理+快速校验"
    else:
        depth = "shallow"
        detail = "直觉响应（低复杂度任务）"
    return {"complexity": round(task_complexity, 2),
            "resources": round(resources, 2),
            "depth_score": round(depth_score, 2),
            "depth": depth, "approach": detail,
            "note": f"思考深度：{depth}（{'深思考' if depth=='deep' else '浅处理' if depth=='shallow' else '标准'}）"}


def adaptive_depth(task: str) -> dict:
    """自适应深度：任务关键词评估复杂度 → 调节。"""
    complexity = 0.5
    # 复杂度线索（风险/多步/不确定）
    if any(w in task for w in ("迁移", "重构", "设计", "故障", "升级", "优化")):
        complexity += 0.25
    if any(w in task for w in ("删除", "清空", "覆盖")):
        complexity += 0.2
    if any(w in task for w in ("查询", "查看", "报告", "简单")):
        complexity -= 0.15
    complexity = min(1.0, max(0.1, complexity))
    return regulate_depth(complexity, resources=0.6)
