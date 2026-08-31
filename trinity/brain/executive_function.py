# -*- coding: utf-8 -*-
"""trinity/brain/executive_function.py — 执行功能（EXECUTION 225，大脑化）。

借鉴 lex-executive-function——前额叶的最高控制层：
  抑制控制（忽略干扰）+ 工作记忆更新（刷新内容）+ 任务排序。

与注意力（选择注意）互补：注意力=感知选择；执行=认知控制。
Trinity 现在：
  inhibit(items): 抑制控制（低相关干扰过滤）
  update_wm(items): 工作记忆更新（过时/低价值替换）
  task_priority(tasks): 任务排序（按价值×紧急×可行性）
"""
import os
import sys
import json
import time


def inhibit(items: list, relevance_threshold: float = 0.4) -> dict:
    """抑制控制：过滤低相关干扰项。"""
    kept, inhibited = [], []
    for it in items:
        rel = float(it.get("relevance") or 0)
        if rel >= relevance_threshold:
            kept.append(it)
        else:
            inhibited.append(it)
    return {"kept": kept, "inhibited": inhibited,
            "inhibited_count": len(inhibited),
            "note": f"抑制了 {len(inhibited)} 个干扰项"}


def update_wm(items: list, max_size: int = 7, min_importance: float = 0.3) -> dict:
    """工作记忆更新：过时/低价值项替换（7±2 容量）。"""
    valid = [it for it in items if float(it.get("importance") or 0) >= min_importance]
    # 按重要度排序，保留 top max_size
    valid.sort(key=lambda x: float(x.get("importance") or 0), reverse=True)
    updated = valid[:max_size]
    dropped = valid[max_size:] + [it for it in items
                                  if float(it.get("importance") or 0) < min_importance]
    return {"updated": updated, "dropped": dropped,
            "dropped_count": len(dropped),
            "note": f"工作记忆更新为 {len(updated)} 项（丢弃 {len(dropped)} 项）"}


def task_priority(tasks: list) -> dict:
    """任务排序：按 价值×紧急×可行性 评分。"""
    scored = []
    for t in tasks:
        value = float(t.get("value") or 0.5)
        urgency = float(t.get("urgency") or 0.5)
        feasible = float(t.get("feasible") or 0.8)
        score = value * 0.4 + urgency * 0.4 + feasible * 0.2
        scored.append({"task": t.get("name", "?"), "score": round(score, 2),
                       "value": value, "urgency": urgency})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"ordered": scored, "top": scored[0]["task"] if scored else None}
