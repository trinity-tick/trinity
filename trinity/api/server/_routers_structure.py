#!/usr/bin/env python3
"""
Trinity REST API Server — DSH structure layer routes (/structure/*).

Endpoints are registered inside a try/except so a missing structure_store
dependency degrades gracefully (no routes mounted), exactly like the
monolith did.
"""

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


try:
    from trinity.structure_store import (
        structure_stats as _api_structure_stats,
        structure_sessions as _api_structure_sessions,
        structure_query as _api_structure_query,
        structure_sync as _api_structure_sync,
        goal_upsert as _api_goal_upsert,
        goal_list as _api_goal_list,
        schedule_upsert as _api_schedule_upsert,
        schedule_list as _api_schedule_list,
    )

    @router.get("/structure/stats", tags=["Structure"])
    def structure_stats():
        return _api_structure_stats()

    @router.get("/structure/sessions", tags=["Structure"])
    def structure_sessions(limit: int = 200):
        return _api_structure_sessions()

    @router.get("/structure/events", tags=["Structure"])
    def structure_events(
        session_id: str = "", type: str = "", agent_id: str = "",
        limit: int = 200,
    ):
        """查询 DSH 会话事件流（可回放轨迹）。"""
        return _api_structure_query({
            "session_id": session_id or None,
            "type": type or None,
            "agent_id": agent_id or None,
            "limit": limit,
        })

    @router.get("/structure/goals", tags=["Structure"])
    def structure_goals(limit: int = 100):
        return _api_goal_list()

    @router.get("/structure/schedules", tags=["Structure"])
    def structure_schedules(limit: int = 100):
        return _api_schedule_list()

    @router.post("/structure/sync", tags=["Structure"])
    def structure_sync(body: dict):
        """写入 DSH 结构（会话 + 事件流 + todos + headers）——与 worker
        structure_sync 同语义，供外部系统/脚本直接同步结构。"""
        return _api_structure_sync(body)

    @router.post("/structure/goals", tags=["Structure"])
    def structure_goal_upsert(body: dict):
        """写入/更新一个结构 goal。"""
        return _api_goal_upsert(body)

    @router.post("/structure/schedules", tags=["Structure"])
    def structure_schedule_upsert(body: dict):
        """写入/更新一个结构 schedule。"""
        return _api_schedule_upsert(body)

    logger.info("Structure endpoints mounted at /structure/* (DSH structure layer)")
except Exception as _struct_err:  # pragma: no cover
    logger.warning("Structure endpoints not mounted: %s", _struct_err)


