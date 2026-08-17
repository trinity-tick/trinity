#!/usr/bin/env python3
"""
Trinity REST API Server — Marvis A2A adapter routes (/a2a/marvis/*).
"""

from typing import Any, Dict

from fastapi import APIRouter

from ._deps import _live_memory as get_memory
from ._models import MarvisAgentRegisterRequest, MarvisDispatchRequest
from ._routers_a2a import _get_a2a_registry, _get_a2a_task_manager
from ._routers_audit import _get_auditor, _get_diagnostics_count

router = APIRouter()


@router.post("/a2a/marvis/agents/register", tags=["Marvis Adapter"], summary="Marvis Agent 注册")
async def marvis_register_agent(req: MarvisAgentRegisterRequest):
    """Marvis 专用 Agent 注册 ——自动生成 AgentCard 并注册到联邦目录。
    为Marvis 生态下的子 Agent 一站式完成）    1) A2A AgentCard 生成与签名    2) 能力注册到全局 CapabilityRegistry
    3) 多锚点身份初始化（identity 四类锚点）    """
    from trinity.a2a.agent_card import AgentCard, SkillDef, generate_card, sign_card

    skills = [
        SkillDef(
            name=f"{req.agent_id}-{cap}",
            description=cap,
            input_schema={},
            output_schema={},
        )
        for cap in req.capabilities
    ] or [SkillDef(name=f"{req.agent_id}-default", description="Default capability")]

    card = AgentCard(
        agent_id=req.agent_id,
        name=req.agent_name,
        description=req.metadata.get("description", f"Marvis sub-agent: {req.agent_name}"),
        version="8.2.0",
        capabilities=req.capabilities,
        endpoints=[f"/a2a/agents/{req.agent_id}"],
        skills=skills,
        input_modes=["text", "json"],
        output_modes=["text", "json", "stream"],
        security_level=req.metadata.get("security_level", "standard"),
    )
    sign_card(card)

    reg = _get_a2a_registry()
    result = reg.register_agent(card)
    return {"status": "registered", "agent_id": req.agent_id, "card": card.to_dict(), "registry_result": result}


@router.post("/a2a/marvis/dispatch", tags=["Marvis Adapter"], summary="Marvis 任务调度")
async def marvis_dispatch(req: MarvisDispatchRequest):
    """Marvis dispatch 语义的A2A 调度端点。
    接收 Marvis 的全局目标 + 当前任务上下文，创建 A2A 任务
    并保留Marvis 特有字段（global_goal / current_task / memory_ids）    以便下游 A2A→Marvis 响应重构。    """
    tm = _get_a2a_task_manager()

    enriched_payload = {
        "marvis_global_goal": req.global_goal,
        "marvis_current_task": req.current_task,
        "marvis_memory_ids": req.memory_ids,
        "marvis_context": req.context_dict,
        "priority": req.priority,
        "inner_payload": req.payload,
    }

    task_result = tm.create_task(req.from_agent, req.to_agent, enriched_payload)
    return {
        "task_id": task_result.task_id,
        "status": task_result.status,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "created_at": task_result.created_at,
    }


@router.get("/a2a/marvis/snapshot", tags=["Marvis Adapter"], summary="全局记忆快照")
async def marvis_global_snapshot():
    """全局记忆快照 ——为Marvis 提供跨Agent 全局决策视图。
    聚合所有注册Agent 的：身份画像 / 记忆统计 / 最近任务/ 信任评分。    """
    from datetime import datetime, timezone

    reg = _get_a2a_registry()
    tm = _get_a2a_task_manager()

    agents = reg.list_all_agents()
    tasks_raw = tm.list_tasks(limit=10)

    # Aggregate trust scores per agent
    trust_scores: Dict[str, Any] = {}
    for agent in agents:
        agent_id = agent.get("agent_id", "")
        if agent_id:
            try:
                mem = get_memory()
                auditor = _get_auditor()
                metrics = auditor.get_metrics()
                violation_count = metrics.get("violation_count", 0)
                trust_scores[agent_id] = {
                    "overall_score": round(max(0.0, 1.0 - 0.05 * violation_count), 4),
                    "violation_count": violation_count,
                    "aedy": metrics.get("aedy", 0.0),
                    "jpc": metrics.get("jpc", 0.0),
                    "mcr": metrics.get("mcr", 0.0),
                    "tsad": metrics.get("tsad", 0.0),
                    "edq": metrics.get("edq", 0.0),
                    "last_audited": metrics.get("last_audited", ""),
                }
            except Exception:
                trust_scores[agent_id] = {"overall_score": 1.0, "violation_count": 0}

    return {
        "agent_count": len(agents),
        "total_memories": _get_diagnostics_count(),
        "sub_agent_profiles": {a.get("agent_id", ""): a for a in agents},
        "recent_tasks": tasks_raw if isinstance(tasks_raw, list) else tasks_raw.get("tasks", []),
        "trust_scores": trust_scores,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/a2a/marvis/agents/{name}/trust", tags=["Marvis Adapter"], summary="Agent 信任评分")
async def marvis_agent_trust(name: str):
    """Agent 信任评分 ——基于 DCSA-EJP 六轴指标的复合信任分。
    返回 AEDY / JPC / MCR / TSAD / EDQ 各轴分数 + 综合信任分。    """
    try:
        auditor = _get_auditor()
        metrics = auditor.get_metrics()
        violation_count = metrics.get("violation_count", 0)
        overall = round(max(0.0, 1.0 - 0.05 * violation_count), 4)
        return {
            "agent_name": name,
            "overall_score": overall,
            "aedy": metrics.get("aedy", 0.0),
            "jpc": metrics.get("jpc", 0.0),
            "mcr": metrics.get("mcr", 0.0),
            "tsad": metrics.get("tsad", 0.0),
            "edq": metrics.get("edq", 0.0),
            "violation_count": violation_count,
            "last_audited": metrics.get("last_audited", ""),
        }
    except Exception as e:
        return {
            "agent_name": name,
            "overall_score": 1.0,
            "error": str(e),
            "aedy": 0.0, "jpc": 0.0, "mcr": 0.0, "tsad": 0.0, "edq": 0.0,
            "violation_count": 0,
            "last_audited": "",
        }


