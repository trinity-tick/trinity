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
                    limit: int = 10000) -> Dict[str, Any]:
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
            _tg = r.get("tags") or []
            if isinstance(_tg, str):
                try:
                    _tg = json.loads(_tg) if _tg.strip().startswith("[") else [_tg]
                except Exception:
                    _tg = [_tg]
            items.append({
                "content": r.get("content") or "",
                "category": r.get("category"),
                "tags": _tg,
                "importance": r.get("importance"),
                "agent_id": r.get("agent_id"),
                "created_at": r.get("created_at"),
                "content_hash": r.get("content_hash") or r.get("sha256_hash"),
                "memory_layer": r.get("memory_layer"),
            })
        return {"format": "trinity-memory-pack-v1", "exported_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime()), "count": len(items), "items": items}

    def import_pack(self, pack: Dict[str, Any], source: str = "federation") -> int:
        """导入记忆包（幂等：content_hash 已存在则跳过；同 hash 异内容→冲突标记）。
        2026-08-27（一致性）：返回 {added, skipped, conflicts}。"""
        items = (pack or {}).get("items") or []
        added = 0
        skipped = 0
        conflicts = 0
        conn = self._mem._adapter._conn
        for it in items:
            h = it.get("content_hash")
            if h:
                dup = conn.execute(
                    "SELECT memory_id, content FROM memories WHERE content_hash=? AND status='active' LIMIT 1",
                    (h,)).fetchone()
                if dup:
                    # 2026-08-27: 同 hash 异内容 → 冲突标记（content_hash 相同但内容被改）
                    if str(dup["content"] or "") != str(it.get("content") or ""):
                        cg = "fed-conflict-" + str(h)[:12]
                        conn.execute(
                            "UPDATE memories SET conflict_group_id=? WHERE memory_id=?",
                            (cg, dup["memory_id"]))
                        conflicts += 1
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
        conn.commit()
        return {"added": added, "skipped": skipped, "conflicts": conflicts}

    def push_remote(self, target_base: str, pack: Dict[str, Any],
                    timeout: float = 30.0, token: Optional[str] = None) -> int:
        """2026-08-27 (cross-instance): pack items -> target REST /memories."""
        import urllib.request as _ur
        import json as _json
        n = 0
        url = target_base.rstrip("/") + "/v1/memories"
        for it in (pack or {}).get("items") or []:
            body = {"content": it.get("content") or "",
                    "category": it.get("category") or "general",
                    "tags": it.get("tags") or [],
                    "importance": float(it.get("importance") or 0.5)}
            _hdrs = {"Content-Type": "application/json"}
            _tok = token or os.environ.get("TRINITY_API_KEY", "")
            if _tok:
                _hdrs["Authorization"] = "Bearer " + _tok
            req = _ur.Request(url, data=_json.dumps(body).encode("utf-8"), headers=_hdrs)
            try:
                with _ur.urlopen(req, timeout=timeout) as resp:
                    if resp.status < 300:
                        n += 1
            except Exception:
                continue
        return n
