# -*- coding: utf-8 -*-
"""federation.py — 多实例联邦第一步（2026-08-27 方向4）。

export_pack: 按 agent/category 导出记忆 JSON 包（内容解密、含元数据/审计哈希）。
import_pack: 另一实例导入（幂等：content_hash 去重）。
"""
import os
import sys
import json
import time
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


class Federation:
    """记忆联邦：实例间记忆导出/导入。"""

    def __init__(self, adapter: str = "sqlite"):
        from trinity import Trinity
        self._mem = Trinity(adapter=adapter)

    def export_pack(self, agent_ids: Optional[List[str]] = None,
                    categories: Optional[List[str]] = None,
                    limit: int = 2000) -> Dict[str, Any]:
        """导出记忆包（JSON 可序列化）。"""
        rows = self._mem._adapter.get_all_memories(limit=limit, offset=0)
        items = []
        for r in rows:
            if r.get("status") != "active":
                continue
            if agent_ids and r.get("agent_id") not in agent_ids:
                continue
            if categories and (r.get("category") or "") not in categories:
                continue
            items.append({
                "content": r.get("content") or "",
                "category": r.get("category"),
                "tags": r.get("tags") or [],
                "importance": r.get("importance"),
                "agent_id": r.get("agent_id"),
                "created_at": r.get("created_at"),
                "content_hash": r.get("content_hash") or r.get("sha256_hash"),
                "memory_layer": r.get("memory_layer"),
            })
        return {"format": "trinity-memory-pack-v1", "exported_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime()), "count": len(items), "items": items}

    def import_pack(self, pack: Dict[str, Any], source: str = "federation") -> int:
        """导入记忆包（幂等：content_hash 已存在则跳过）。返回新增数。"""
        items = (pack or {}).get("items") or []
        added = 0
        skipped = 0
        conn = self._mem._adapter._conn
        for it in items:
            h = it.get("content_hash")
            if h:
                dup = conn.execute(
                    "SELECT 1 FROM memories WHERE content_hash=? AND status='active' LIMIT 1",
                    (h,)).fetchone()
                if dup:
                    skipped += 1
                    continue
            self._mem.ingest(
                it.get("content") or "",
                category=it.get("category") or "general",
                tags=it.get("tags") or [],
                importance=float(it.get("importance") or 0.5),
                agent_id=it.get("agent_id") or "federation-agent",
                postprocess=False,
            )
            added += 1
        return added
