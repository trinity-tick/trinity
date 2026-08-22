#!/usr/bin/env python3
"""
Trinity REST API Server — Persona 白盒画像路由 (/persona/*, /personas).

白盒 persona 画像层：把引擎库中 `category='proposition'` 且
`proposition_type='user_preference'`（可选 user_fact）的记忆按 persona 聚合，
渲染成 Markdown 画像（~/.trinity/personas/{persona_id}.md）。

路由风格对齐 _routers_memories.py：使用 APIRouter 独立文件，不修改
server/__init__.py；挂载由主代理统一处理。共享 PersonaEngine 惰性创建，
依赖 _deps.get_memory() 返回的引擎 adapter（读引擎库，绝不写大库）。
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from trinity.api.server._deps import _live_memory as get_memory
from trinity.memory.persona import PersonaEngine

logger = logging.getLogger(__name__)

router = APIRouter()

# 共享 PersonaEngine（惰性创建；可在测试中经 _set_persona_engine 覆盖）
_persona_engine: Optional[PersonaEngine] = None


def _set_persona_engine(engine: Optional[PersonaEngine]) -> None:
    """注入/重置共享 PersonaEngine（主要供单测隔离用临时目录）。"""
    global _persona_engine
    _persona_engine = engine


def _get_persona_engine() -> PersonaEngine:
    global _persona_engine
    if _persona_engine is None:
        # 复用 API 运行中引擎的 adapter（只读聚合命题，不另起后台线程）
        mem = get_memory()
        adapter = getattr(mem, "_adapter", None)
        _persona_engine = PersonaEngine(adapter=adapter)
    return _persona_engine


@router.get("/persona/{persona_id}", summary="读取 persona 画像（markdown 文本 + 条目数）")
async def read_persona(persona_id: str):
    """读取某 persona 的白盒画像 markdown 文本与条目数。

    画像不存在（从未重建）时返回 404。
    """
    engine = _get_persona_engine()
    text, count = engine.read_persona(persona_id)
    if not text:
        raise HTTPException(status_code=404, detail=f"persona {persona_id!r} not found")
    return {"persona_id": persona_id, "entry_count": count, "markdown": text}


@router.post("/persona/{persona_id}/rebuild", summary="全量重建 persona 画像")
async def rebuild_persona(persona_id: str):
    """从引擎库全量重建某人 persona 画像，返回条目数。"""
    engine = _get_persona_engine()
    count = engine.rebuild(persona_id)
    return {"persona_id": persona_id, "entry_count": count, "rebuilt": True}


@router.get("/personas", summary="枚举已有 persona 画像")
async def list_personas():
    """返回当前已落盘 persona 画像的 persona_id 列表。"""
    engine = _get_persona_engine()
    ids = engine.list_personas()
    return {"personas": ids, "total": len(ids)}
