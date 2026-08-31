# -*- coding: utf-8 -*-
"""trinity/brain/multi_agent_coordination.py — 多 Agent 协调（EXECUTION 234）。

借鉴 BMAM（ACL 2026：Brain-inspired Multi-Agent Memory Framework）/
Mycelium（协调层）——多 Agent 协作执行：任务分派 + 记忆仲裁。

社会认知最后一块：理解（ToM）+ 学习（观察/传染）+ **协作（执行）**。
Trinity 现在：
  coordinate(agents, task): 任务分派（按 ToM 画像匹配）→ 汇总
  memory_arbitration(): 记忆仲裁（多 Agent 冲突/重复解决）
"""
import os
import sys
import json


def coordinate(agents: list, task: str) -> dict:
    """任务协作：按 ToM 画像分派任务（匹配 Agent 特长）。"""
    assignment = []
    for a in agents[:4]:
        # 用 ToM 推断 agent 画像（知识/活跃）
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity.brain.theory_of_mind import infer_agent
            st = infer_agent(a)
            state = st.get("mental_state", "未知")
            knowledge = st.get("knowledge", 0)
        except Exception:
            state = "未知"
            knowledge = 0
        # 分派：知识多 → 主任务；知识少 → 辅助/学习
        role = "lead" if knowledge > 100 else ("support" if knowledge > 10 else "learn")
        assignment.append({"agent": a, "role": role, "state": state,
                           "task_part": f"{task}（{role}部分）"})
    return {"assignment": assignment, "task": task, "agents": len(agents)}


def memory_arbitration(memory_id: str, candidates: list) -> dict:
    """记忆仲裁：多 Agent 对同一记忆的冲突解决（投票/权重）。"""
    if not candidates:
        return {"resolved": False, "note": "无候选"}
    # 简单仲裁：按 agent 信誉加权投票
    votes = {"accept": 0, "reject": 0, "detail": []}
    for c in candidates:
        agent = c.get("agent", "?")
        verdict = c.get("verdict", "accept")
        weight = c.get("weight", 1.0)
        votes[verdict] += weight
        votes["detail"].append({"agent": agent, "verdict": verdict, "weight": weight})
    resolved = "accept" if votes["accept"] >= votes["reject"] else "reject"
    return {"memory_id": memory_id, "resolved": resolved,
            "accept_votes": round(votes["accept"], 2),
            "reject_votes": round(votes["reject"], 2),
            "detail": votes["detail"]}


def coordination_report() -> dict:
    """协调状态报告。"""
    return {"note": "多 Agent 协调可用：分派（ToM 匹配）+ 仲裁（信誉加权投票）"}
