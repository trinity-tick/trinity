#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trinity REST API Server — context offload routes (/offload/*, 短期记忆 Mermaid 符号卸载).

挂载说明（主代理在 trinity/api/server/__init__.py 统一挂载）：
  本文件的路由路径已含完整前缀 `/offload/...`，`prefix` 变量约定为空串
  （与其它 _routers_* 一致，避免 include_router 二次加前缀）。两种挂法均可：

    # 方式 A（本仓惯例，扁平化注册，推荐）：
    from ._routers_offload import router as offload_router
    _register_router_routes(offload_router)

    # 方式 B（标准 include_router，空 prefix 不重复）：
    from ._routers_offload import router as offload_router, prefix as offload_prefix
    app.include_router(offload_router, prefix=offload_prefix)

端点：
    POST /offload/task            落盘一个任务的轨迹 + 生成画布/索引
    GET  /offload/canvas/{task_id} 返回 Mermaid 画布文本
    GET  /offload/node/{node_id}   返回某节点原文 + meta
    GET  /offload/search?q=        关键词检索（re 匹配），返回命中 node_id 列表

与其它 _routers_* 一致：import 失败时降级（不挂载路由），函数级 try/except 兜底。
"""

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()
prefix = ""


try:
    from trinity.memory.offload import (  # type: ignore
        drill_down as _offload_drill_down,
        get_canvas as _offload_get_canvas,
        offload_task as _offload_task,
        search_offload as _offload_search,
    )

    @router.post("/offload/task", tags=["Offload"])
    def offload_task(body: dict):
        """卸载一个任务轨迹（短期记忆 → 磁盘符号化记忆）。

        body: {task_id: str, entries: [{node_type?, summary?, content, ts?}...]}
        返回: {task_id, canvas_path, node_count, nodes:[...]}。
        同 task_id 重跑为覆盖写（按本次 entries 重建），返回同样结构。
        """
        task_id = (body or {}).get("task_id")
        if not task_id:
            raise HTTPException(status_code=400, detail="task_id required")
        entries = (body or {}).get("entries") or []
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="entries must be a list")
        result = _offload_task(task_id, entries)
        if not result.get("canvas_path"):
            # 画布路径为空说明落盘/生成失败（但已 try/except 兜底）
            raise HTTPException(status_code=500, detail="offload write failed")
        # 不回传整段 content，避免把已卸载的上下文重新灌回 API 响应
        compact = dict(result)
        if "nodes" in compact:
            compact["nodes"] = [
                {k: v for k, v in (n or {}).items() if k != "content"}
                for n in result.get("nodes", [])
            ]
        return compact

    @router.get("/offload/canvas/{task_id}", tags=["Offload"])
    def offload_canvas(task_id: str):
        """返回某 task 的 Mermaid 画布文本（graph LR 符号化记忆视图）。"""
        canvas = _offload_get_canvas(task_id)
        if canvas is None:
            raise HTTPException(status_code=404, detail=f"no canvas for task {task_id}")
        return {"task_id": task_id, "mermaid": canvas}

    @router.get("/offload/node/{node_id}", tags=["Offload"])
    def offload_node(node_id: str):
        """读回某节点的原文 + meta（drill_down）。node_id 形如 {task_id}:{seq}。"""
        node = _offload_drill_down(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"no node {node_id}")
        return node

    @router.get("/offload/search", tags=["Offload"])
    def offload_search(
        q: str = "",
        task_id: str = "",
        limit: int = 50,
    ):
        """在索引 + refs 上做关键词检索（re 匹配），返回命中 node_id 列表。"""
        if not q:
            raise HTTPException(status_code=400, detail="q required")
        hits = _offload_search(
            q, task_id=task_id if task_id else None, limit=min(max(int(limit), 1), 500)
        )
        return {"query": q, "count": len(hits), "results": hits}

    logger.info("Offload endpoints mounted at /offload/* (context offload)")
except Exception as _offload_err:  # pragma: no cover — 缺依赖时仅降级不阻断
    logger.warning("Offload endpoints not mounted: %s", _offload_err)
