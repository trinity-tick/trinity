#!/usr/bin/env python3
"""
Trinity REST API Server — self-evolution routes (/evolution/*).
"""

from fastapi import APIRouter

from ._models import FeedbackRequest, MemoryAccessRequest

router = APIRouter()


_scheduler = None


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        from trinity.evolution import EvolutionScheduler
        _scheduler = EvolutionScheduler()
    return _scheduler


@router.post("/evolution/track-access", tags=["Self Evolution"],
          summary="记录记忆访问")
async def evolution_track_access(req: MemoryAccessRequest):
    """记录一次记忆访问事件供后续模式分析。"""
    get_scheduler().analyzer.track_access(
        memory_id=req.memory_id,
        agent_id=req.agent_id,
        action=req.action,
        context=req.context,
    )
    return {"status": "recorded", "memory_id": req.memory_id}


@router.get("/evolution/heatmap", tags=["Self Evolution"],
         summary="使用热力图")
async def evolution_heatmap(hours: int = 168):
    """返回指定时间窗口内的记忆使用热力图。"""
    heatmaps = get_scheduler().analyzer.get_heatmap(hours=hours)
    return {
        "window_hours": hours,
        "count": len(heatmaps),
        "heatmaps": [
            {
                "memory_id": h.memory_id,
                "total_accesses": h.total_accesses,
                "peak_hour": h.peak_hour,
                "peak_count": h.peak_count,
                "hourly_buckets": h.hourly_buckets,
            }
            for h in heatmaps
        ],
    }


@router.get("/evolution/hotspots", tags=["Self Evolution"],
         summary="热点记忆")
async def evolution_hotspots(window: int = 24):
    """返回指定窗口内的热点记忆。"""
    hotspots = get_scheduler().analyzer.analyze_hotspots(time_window_hours=window)
    return {
        "window_hours": window,
        "count": len(hotspots),
        "hotspots": [
            {
                "memory_id": h.memory_id,
                "access_count": h.access_count,
                "avg_interval_seconds": h.avg_interval_seconds,
                "burst_factor": h.burst_factor,
                "co_referenced": h.co_referenced,
                "pattern": h.pattern,
            }
            for h in hotspots
        ],
    }


@router.get("/evolution/patterns", tags=["Self Evolution"],
         summary="使用模式")
async def evolution_patterns():
    """检测并返回全局记忆使用模式。"""
    patterns = get_scheduler().analyzer.detect_patterns()
    return {
        "count": len(patterns),
        "patterns": [
            {
                "pattern_type": p.pattern_type,
                "memory_ids": p.memory_ids,
                "confidence": p.confidence,
                "description": p.description,
                "metadata": p.metadata,
            }
            for p in patterns
        ],
    }


@router.post("/evolution/feedback", tags=["Self Evolution"],
          summary="提交反馈")
async def evolution_feedback(req: FeedbackRequest):
    """提交对记忆质量的反馈评分。"""
    entry = get_scheduler().collector.record_feedback(
        memory_id=req.memory_id,
        agent_id=req.agent_id,
        rating=req.rating,
        comment=req.comment,
        context=req.context,
    )
    return {
        "feedback_id": entry.feedback_id,
        "memory_id": entry.memory_id,
        "rating": entry.rating,
        "status": "recorded",
    }


@router.get("/evolution/quality-alerts", tags=["Self Evolution"],
         summary="质量告警")
async def evolution_quality_alerts():
    """返回当前记忆质量告警列表。"""
    issues = get_scheduler().collector.detect_quality_issues()
    return {
        "count": len(issues),
        "issues": [
            {
                "memory_id": i.memory_id,
                "issue_type": i.issue_type,
                "severity": i.severity,
                "description": i.description,
                "suggestion": i.suggestion,
            }
            for i in issues
        ],
    }


@router.get("/evolution/suggestions", tags=["Self Evolution"],
         summary="变异建议")
async def evolution_suggestions():
    """返回待审的记忆变异建议列表。"""
    pending = get_scheduler().mutator.get_pending()
    return {
        "count": len(pending),
        "suggestions": [
            {
                "type": getattr(s, "type", "unknown"),
                "confidence": getattr(s, "confidence", 0.0),
                "reason": getattr(s, "reason", ""),
            }
            for s in pending
        ],
    }


@router.post("/evolution/suggestions/{suggestion_id}/apply", tags=["Self Evolution"],
          summary="应用建议")
async def evolution_apply_suggestion(suggestion_id: str):
    """手动应用指定变异建议。"""
    results = get_scheduler().mutator.auto_apply(["merge", "enrich", "split", "synthesis"])
    return {
        "suggestion_id": suggestion_id,
        "applied_count": len(results),
        "results": results,
    }


@router.post("/evolution/cycle/run", tags=["Self Evolution"],
          summary="手动触发进化周期")
async def evolution_cycle_run():
    """手动触发一次完整进化周期。"""
    result = get_scheduler().run_evolution_cycle()
    return {
        "cycle_id": result.cycle_id,
        "timestamp": result.timestamp,
        "status": result.status,
        "strategy_triggers": result.strategy_triggers,
        "applied_mutations": result.applied_mutations,
        "index_changes": result.index_changes,
        "quality_alerts": result.quality_alerts,
        "details": result.details,
    }


@router.get("/evolution/cycle/history", tags=["Self Evolution"],
         summary="进化历史")
async def evolution_cycle_history(limit: int = 20):
    """返回进化周期执行历史。"""
    history = get_scheduler().get_evolution_history(limit=limit)
    return {
        "count": len(history),
        "cycles": [
            {
                "cycle_id": h.cycle_id,
                "timestamp": h.timestamp,
                "status": h.status,
                "strategy_triggers": h.strategy_triggers,
                "applied_mutations": h.applied_mutations,
                "index_changes": h.index_changes,
                "quality_alerts": h.quality_alerts,
            }
            for h in history
        ],
    }


@router.get("/evolution/stats", tags=["Self Evolution"],
         summary="优化统计")
async def evolution_stats():
    """返回累计进化优化统计。"""
    stats = get_scheduler().optimizer.get_optimization_stats()
    return {
        "total_index_changes": stats.total_index_changes,
        "total_graph_reorgs": stats.total_graph_reorgs,
        "total_pruned": stats.total_pruned,
        "total_defrags": stats.total_defrags,
        "last_cycle": stats.last_cycle,
    }


