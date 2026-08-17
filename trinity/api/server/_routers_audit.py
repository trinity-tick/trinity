#!/usr/bin/env python3
"""
Trinity REST API Server — audit / DCSA routes.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ._deps import _live_memory as get_memory
from ._models import AuditRunRequest, ConstitutionUpdateRequest

router = APIRouter()


@router.get("/audit/memories/{memory_id}")
async def audit_memory_trail(memory_id: str):
    """查看某条记忆的完整审计轨迹。"""
    mem = get_memory()
    trail = mem.get_audit_trail(memory_id)
    return {"memory_id": memory_id, "audit_trail": trail, "total_entries": len(trail)}


@router.get("/audit/agents/{agent_id}/replay")
async def audit_agent_replay(
    agent_id: str,
    start_time: Optional[str] = Query(None, description="ISO 格式起始时间"),
    end_time: Optional[str] = Query(None, description="ISO 格式结束时间"),
):
    """回放某Agent 在时间段内的所有操作。"""
    mem = get_memory()
    session = mem.replay_session(agent_id, start_time, end_time)
    return {
        "agent_id": agent_id,
        "time_range": {"start": start_time, "end": end_time},
        "operations": session,
        "total_operations": len(session),
    }


@router.get("/audit/integrity")
async def audit_integrity():
    """审计链完整性验证报告。"""
    mem = get_memory()
    result = mem.verify_integrity()
    return result


@router.get("/audit/summary")
async def audit_summary(
    start_time: Optional[str] = Query(None, description="ISO 格式起始时间"),
    end_time: Optional[str] = Query(None, description="ISO 格式结束时间"),
):
    """审计摘要：各操作计数、活跃Agent、峰值时段。"""
    mem = get_memory()
    result = mem.audit_summary(start_time, end_time)
    return result


@router.get("/audit/timeline")
async def audit_timeline(
    agent_id: Optional[str] = Query(None, description="Agent 标识"),
    limit: int = Query(50, description="最大返回条数"),
):
    """最近操作时间线。"""
    mem = get_memory()
    # 使用 replay_agent_session 或直接查 audit_log
    results = []
    if agent_id:
        session = mem.replay_session(agent_id)
        results = session[-limit:]
    return {"agent_id": agent_id, "timeline": results, "total_displayed": len(results)}


_dcsa_constitution = None
_dcsa_auditor = None


def _get_constitution():
    global _dcsa_constitution
    if _dcsa_constitution is None:
        from trinity.audit.constitution import ConstitutionalEngine
        _dcsa_constitution = ConstitutionalEngine()
        _dcsa_constitution.load_default_constitution()
    return _dcsa_constitution


def _get_auditor():
    global _dcsa_auditor
    if _dcsa_auditor is None:
        from trinity.audit.auditor import Auditor
        mem = get_memory()
        _dcsa_auditor = Auditor(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _dcsa_auditor


def _get_diagnostics_count() -> int:
    """Get total memory count from Trinity diagnostics (degraded-safe)."""
    try:
        mem = get_memory()
        if hasattr(mem, 'diagnostics'):
            diag = mem.diagnostics()
            if isinstance(diag, dict):
                return diag.get("memory_count", 0)
    except Exception:
        pass
    return 0


@router.post("/audit/run", tags=["DCSA Audit"], summary="执行双循环审计")
async def dcsa_audit_run(req: AuditRunRequest):
    """执行一次双循环审计（executor + auditor）。"""
    auditor = _get_auditor()
    result = auditor.audit_action({"agent_id": req.agent_id, "task": req.task})
    return result


@router.get("/audit/runs", tags=["DCSA Audit"], summary="审计运行历史")
async def dcsa_audit_runs(agent_id: str = None, limit: int = 50):
    """审计运行历史列表。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_audit_history"):
        if agent_id:
            return {"runs": mem._adapter.get_audit_history(agent_id, limit)}
        return {"runs": []}
    return {"runs": [], "error": "no adapter"}


@router.get("/audit/runs/{run_id}", tags=["DCSA Audit"], summary="审计运行详情")
async def dcsa_audit_run_detail(run_id: str):
    """单次审计详情（含合理性数据包）。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_audit_run"):
        result = mem._adapter.get_audit_run(run_id)
        if result:
            return result
        raise HTTPException(status_code=404, detail=f"Audit run {run_id} not found")
    return {"error": "no adapter"}


@router.get("/audit/violations", tags=["DCSA Audit"], summary="违规趋势查询")
async def dcsa_violations(agent_id: str = None, limit: int = 100):
    """违规趋势查询。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_violation_trends"):
        trends = mem._adapter.get_violation_trends(agent_id, limit)
        return {"violations": trends, "total": len(trends)}
    return {"violations": [], "total": 0, "error": "no adapter"}


@router.get("/audit/constitution", tags=["DCSA Audit"], summary="查看宪法不变式")
async def dcsa_get_constitution():
    """查看当前宪法不变式列表。"""
    ce = _get_constitution()
    return {"invariants": ce.list_invariants()}


@router.put("/audit/constitution", tags=["DCSA Audit"], summary="更新宪法不变式")
async def dcsa_update_constitution(req: ConstitutionUpdateRequest):
    """添加或替换宪法不变式。"""
    ce = _get_constitution()
    from trinity.audit.constitution import Severity
    sev_map = {"low": Severity.LOW, "medium": Severity.MEDIUM,
               "high": Severity.HIGH, "critical": Severity.CRITICAL}
    sev = sev_map.get(req.severity, Severity.MEDIUM)
    ce.add_invariant(name=req.name, rule=req.rule,
                      severity=sev)
    return {"status": "ok", "total": len(ce.list_invariants())}


@router.get("/audit/metrics", tags=["DCSA Audit"], summary="DCSA-EJP 六项指标")
async def dcsa_metrics():
    """DCSA-EJP 六项指标实时值。"""
    auditor = _get_auditor()
    return auditor.get_metrics()


