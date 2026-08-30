# -*- coding: utf-8 -*-
"""trinity/brain/cognition_pipeline.py — 认知编排层（2026-09，EXECUTION 165）

从"散落机制"到"固定管线"：定义检索认知阶段顺序（观测版——不改变
行为，只记录每阶段执行状态/耗时，供可观测与后续重排）。

管线阶段（大脑认知顺序）：
  1. context    连续状态/会话身份（我是谁/我在关注什么）
  2. affect     情绪偏置（情绪如何调制）
  3. graph      图谱增强（联想网络）
  4. confidence 置信度标注（我知道多少）
  5. prediction 预测误差（我猜中多少）
  6. hebbian    权重强化（我学到了什么）

search_hybrid 调用 run_pipeline 生成观测报告（零行为影响）。
"""
import os
import time


STAGES = ["context", "affect", "graph", "confidence", "prediction", "hebbian"]


def run_pipeline(client, query: str, results: list, stage_flags: dict) -> dict:
    """观测各认知阶段的状态/耗时，返回管线报告。

    client: Trinity 客户端（用于读取状态）
    stage_flags: {stage: bool}——该阶段是否在本次检索中生效
    TRINITY_COGNITION_STAGES=context,affect 可指定启用子集（行为化开关）。
    """
    _enabled = os.environ.get("TRINITY_COGNITION_STAGES", "").strip()
    if _enabled:
        _set = {s.strip() for s in _enabled.split(",") if s.strip()}
        for _s in STAGES:
            if _s not in _set:
                stage_flags[_s] = False
    report = {"stages": {}, "active": 0}
    t0 = time.time()
    for stage in STAGES:
        st = time.time()
        status = "active" if stage_flags.get(stage) else "bypass"
        if stage == "context" and getattr(client, "_last_query", None):
            status = "active"
        if stage == "affect" and getattr(client, "_emo_bias", None):
            status = "active"
        if stage == "graph" and getattr(client, "_last_graph", None):
            status = "active"
        report["stages"][stage] = {
            "status": status,
            "ms": round((time.time() - st) * 1000, 2),
        }
        if status == "active":
            report["active"] += 1
    report["total_ms"] = round((time.time() - t0) * 1000, 2)
    report["results"] = len(results)
    return report
