#!/usr/bin/env python3
"""_routers_cognition.py — 认知主体层端点（2026-09，EXECUTION 105.21）

- POST /cognition/think  思考：目标 → 记忆检索 → LLM 推理 → 建议 + 记忆回写
- POST /cognition/act    行动规划：目标 → 技能匹配 → 行动计划

sync def（FastAPI 线程池；LLM 调用不阻塞事件循环）。
"""

import time

from fastapi import APIRouter, Body

from trinity.cognition.engine import think, act_plan

router = APIRouter()


@router.post("/cognition/think")
def cognition_think(
    goal: str = Body(...),
    session_id: str = Body(None),
    top_k: int = Body(5, ge=1, le=10),
):
    """认知主体思考：目标 + 记忆认知循环 → LLM 推理 → 决策建议。"""
    t0 = time.time()
    result = think(goal, session_id=session_id, top_k=top_k)
    result["latency_s"] = round(time.time() - t0, 2)
    return result


@router.post("/cognition/act")
def cognition_act(
    goal: str = Body(...),
    top_k: int = Body(3, ge=1, le=5),
):
    """认知主体行动规划：目标 → 程序性记忆（技能）匹配 → 行动计划。"""
    return act_plan(goal, top_k=top_k)



@router.post("/cognition/chat")
def cognition_chat(
    message: str = Body(...),
    session_id: str = Body("default"),
):
    """认知主体对话：消息 → 工作记忆/记忆注入 → LLM 响应 → 回写。"""
    from trinity.cognition.dialogue import chat
    return chat(message, session_id=session_id)


@router.post("/cognition/execute")
def cognition_execute(
    goal: str = Body(...),
    session_id: str = Body("default"),
):
    """认知主体行动（认知域安全执行）：检索/技能/自查 → 结论 → 经历回写。"""
    from trinity.cognition.actor import execute
    return execute(goal, session_id=session_id)
