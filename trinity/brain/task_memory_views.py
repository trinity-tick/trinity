# -*- coding: utf-8 -*-
"""trinity/brain/task_memory_views.py — 任务记忆视图（EXECUTION 311）。

借鉴 MemPrism（2026：Task-Conditioned Relational Memory Views）——
不同任务看到不同的记忆组织（任务条件化视角——按任务切换
记忆的突出维度）。

与 7 层（统一视图）互补：7 层=全景；本模块=任务视角。
Trinity 现在：
  view_for(task): 任务视图（任务类型→记忆组织视角）
"""
import os
import sys
import json


# 任务-视图映射（突出维度）
TASK_VIEWS = {
    "decision": {"focus": ["semantic", "episodic"], "organize": "证据优先",
                 "note": "决策视图：证据+经验优先"},
    "planning": {"focus": ["episodic", "prospective"], "organize": "时间线优先",
                 "note": "规划视图：历史+未来意图优先"},
    "reflection": {"focus": ["autobiographical", "metamemory"], "organize": "自我优先",
                   "note": "反思视图：自我+元认知优先"},
    "social": {"focus": ["social", "theory_of_mind"], "organize": "他人优先",
               "note": "社交视图：社会记忆+他人画像优先"},
    "learning": {"focus": ["semantic", "procedural"], "organize": "规律优先",
                 "note": "学习视图：知识+技能优先"},
}


def view_for(task: str) -> dict:
    """任务视图：识别任务类型 → 记忆组织视角。"""
    # 任务类型识别
    task_type = "decision"
    if any(w in task for w in ("规划", "计划", "安排", "未来")):
        task_type = "planning"
    elif any(w in task for w in ("反思", "自省", "总结", "评估")):
        task_type = "reflection"
    elif any(w in task for w in ("协作", "对话", "他人", "社交")):
        task_type = "social"
    elif any(w in task for w in ("学习", "掌握", "理解", "知识")):
        task_type = "learning"
    view = TASK_VIEWS.get(task_type, TASK_VIEWS["decision"])
    return {"task": str(task)[:30], "task_type": task_type, **view}


def view_switch(task: str) -> dict:
    """视图切换（任务→视角变化）。"""
    v = view_for(task)
    return {"switched": True, "view": v,
            "note": f"任务『{task[:20]}』→ {v['organize']}"}
