#!/usr/bin/env python3
"""
Trinity REST API Server — audit / DCSA routes.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from ._deps import _live_memory as get_memory
from ._models import AuditRunRequest, ConstitutionUpdateRequest
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13, AST）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

router = APIRouter()


@router.get("/audit/memories/{memory_id}")
async def audit_memory_trail(memory_id: str):
    """查看某条记忆的完整审计轨迹。

    2026-10-02（外部复评 §3.2）：**未知 id 必须 404，不许 200 + 空链**。
    修复前本端点对**任何** id 都返回 `200 {"audit_trail": [], "total_entries": 0}`，
    于是"这条记忆不存在"与"这条记忆存在但没有审计行"在响应里**长得一模一样**
    （实测：`/agents/memory/write` 写进聚合池的条目、以及任意随机 id，都得到 200+空链，
    而 `/audit/prov/{同一个 id}` 给的是 404 —— 两个端点自相矛盾）。

    判定方向是 **fail-open 到 200**：只有在**确证不存在**（adapter 支持 `get_memory`
    且返回空）时才 404；取不到 adapter / 探测抛错一律维持原行为（不制造假 404）。
    """
    mem = get_memory()
    trail = mem.get_audit_trail(memory_id)
    if not trail and not _memory_exists(mem, memory_id):
        raise HTTPException(status_code=404, detail="未找到记忆: " + memory_id)
    return {"memory_id": memory_id, "audit_trail": trail, "total_entries": len(trail)}


def _memory_exists(mem: Any, memory_id: str) -> bool:
    """记忆是否存在于存储（**只在能确证时**返回 False）。

    返回 True 的两种情形：① 真查到了；② 无法判定（无 adapter / 无 get_memory / 探测抛错）。
    这样本函数只会把"确证不存在"变成 404，不会把"没测"读成"不存在"（本仓 policy ①）。
    """
    adapter = getattr(mem, "_adapter", None) or getattr(mem, "adapter", None)
    getter = getattr(adapter, "get_memory", None)
    if not callable(getter):
        return True
    try:
        return bool(getter(memory_id))
    except Exception as exc:  # pragma: no cover - 取决于后端能力
        swallow(__name__, exc, detail="audit_memory_trail existence probe")
        return True


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
    except Exception:  # 629: 诊断不可用时回退计数 0(有意降级)
        swallow(__name__, None)
    return 0


#: REST 面可表达的动作上下文字段（顺序仅用于日志；默认值与 audit_action 的缺省同值）。
_AUDIT_CONTEXT_FIELDS = (
    "external_api_call", "data_egress_approved", "writes_to_shared", "shared_write_approved",
    "is_irreversible", "irreversible_verified", "action_type", "delete_approved",
    "policy_override", "override_reason", "override_logged", "source", "sink", "justification",
)


def build_audit_context(req: "AuditRunRequest") -> Dict[str, Any]:
    """把 REST 请求映射成 `Auditor.audit_action` 的动作上下文。

    **为什么需要（2026-09-27 实测，EXECUTION 本轮 ③）**：旧路由只传
    `{"agent_id", "task"}`，而 4 条宪法不变式的触发字段（`external_api_call` /
    `is_irreversible` / `policy_override` / `justification.uncertainty_level`）
    **全部不在请求模型里** ⇒ 无论 task 写什么，审计**恒 pass**。实测 5 个逐级变坏的
    用例（「未获批准把全部记忆外发第三方」「不可回滚地删除全部记忆」「绕过写入门禁
    静默覆盖策略」…）**overall 全为 pass、violations 全空** —— 那不是审计通过，是没审。

    映射规则：**假值（None / False / 空串 / 空容器）一律不进上下文** —— 因为
    `audit_action` 对每个字段都是 `.get(..., 默认)`，而这些默认值与「字段不存在」
    **语义完全相同**（实测：`packet` 走 `generate()`、`source_sink_check({}, {})`
    与缺省同判、`_update_metrics` 的 `is_irreversible` 同为假）。
    于是**默认请求**（只给 agent_id/task）产出的上下文与改动前**逐字段相同**
    （非侵入；判据 `tests/unit/test_dcsa_audit_context_mapping.py::test_默认请求的上下文与改动前逐字段一致`）。
    """
    ctx: Dict[str, Any] = {"agent_id": req.agent_id, "task": req.task}
    for _k in _AUDIT_CONTEXT_FIELDS:
        _v = getattr(req, _k, None)
        if not _v:
            continue
        ctx[_k] = _v
    return ctx


@router.post("/audit/run", tags=["DCSA Audit"], summary="执行双循环审计")
async def dcsa_audit_run(req: AuditRunRequest):
    """执行一次双循环审计（executor + auditor）。

    2026-09-27：动作上下文由 `build_audit_context()` 从请求映射（此前只传
    agent_id/task ⇒ 4 条不变式一条都触发不了，审计恒 pass，属橡皮图章）。
    """
    auditor = _get_auditor()
    result = auditor.audit_action(build_audit_context(req))
    return result


@router.get("/audit/runs", tags=["DCSA Audit"], summary="审计运行历史")
async def dcsa_audit_runs(agent_id: Optional[str] = None, limit: int = 50):
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
async def dcsa_violations(agent_id: Optional[str] = None, limit: int = 100):
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


