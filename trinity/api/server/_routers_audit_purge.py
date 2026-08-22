#!/usr/bin/env python3
"""
Trinity REST API Server — audit event purge (GDPR 删除权) routes.

DELETE /audit/events/{event_id}: 物理删除单条审计记录，并写入一条
action='PURGE' 的审计记录（记录被删事件 id、操作者、时间戳）以留痕。

依赖 adapter 的写入能力（write_audit_log + 链式 checksum）；删除本身用
该库最小 SQL（adapter 无现成 audit_log 删除函数），事务/commit 风格对齐
现有 _audit 模块（写锁 + 立即 commit + 失败 rollback）。

本 router 由主代理在 api/server/__init__.py 统一挂载（勿在本模块收集挂载）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from ._deps import _live_memory as get_memory

logger = logging.getLogger(__name__)

router = APIRouter()

_HEADER_AGENT = "X-Agent-ID"


@router.delete("/audit/events/{event_id}", tags=["DCSA Audit"],
               summary="删除单条审计事件（GDPR 删除权）")
async def purge_audit_event(event_id: str, request: Request) -> Dict[str, Any]:
    """物理删除指定 id 的审计记录，并写入一条 action='PURGE' 审计留痕。

    - 不存在 → 404
    - 空/非法 event_id → 400（容错，不触发数据库操作）
    - 数据库删除失败 → 500 + 日志
    """
    event_id = (event_id or "").strip()
    if not event_id:
        raise HTTPException(status_code=400,
                            detail="event_id 不能为空")

    mem = get_memory()
    adapter = getattr(mem, "_adapter", None)
    if adapter is None:
        logger.error("purge_audit_event: adapter 不可用，无法删除审计事件 %s", event_id)
        raise HTTPException(status_code=500,
                            detail="存储适配器不可用")

    conn = getattr(adapter, "_conn", None)
    if conn is None:
        logger.error("purge_audit_event: 数据库连接不可用，无法删除审计事件 %s", event_id)
        raise HTTPException(status_code=500,
                            detail="数据库未连接")

    operator = request.headers.get(_HEADER_AGENT) or "unknown"
    purged_at = datetime.now(timezone.utc).isoformat()

    # ── 删除（最小 SQL；写锁 + 立即 commit + 失败 rollback）─────────────
    try:
        with adapter._write_lock:
            cursor = conn.execute("DELETE FROM audit_log WHERE id = ?", (event_id,))
            if cursor.rowcount == 0:
                # 未找到：回滚（无写事务亦无害），返回 404
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise KeyError(event_id)
            conn.commit()
    except KeyError:
        raise HTTPException(status_code=404,
                            detail=f"Audit event {event_id} not found") from None
    except Exception as exc:  # noqa: BLE001
        logger.exception("purge_audit_event: 删除审计事件 %s 失败", event_id)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=500,
                            detail=f"删除审计事件失败: {type(exc).__name__}") from exc

    # ── 写 PURGE 审计留痕（链式 checksum，立即 commit）──────────────────
    try:
        adapter.write_audit_log(
            memory_id=None,
            action="PURGE",
            agent_id=operator,
            details={
                "purged_event_id": event_id,
                "purged_at": purged_at,
            },
        )
    except Exception:  # noqa: BLE001
        # 删除已成功，留痕失败仅记日志（不因此回滚删除或 500）
        logger.exception(
            "purge_audit_event: 事件 %s 已删除，但 PURGE 留痕写入失败", event_id)

    return {
        "status": "ok",
        "purged_event_id": event_id,
        "action": "PURGE",
        "operator": operator,
        "purged_at": purged_at,
    }
