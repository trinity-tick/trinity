# -*- coding: utf-8 -*-
"""mesh.py — AgentMesh 委托机制（2026-08-27 方向B）。

记忆即协作总线：delegation 记忆类型，原子状态机 pending->claimed->done。
"""
import os
import sys
import time
import threading
from typing import Any, Dict, List, Optional

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

CATEGORY = "delegation"
_LOCK = threading.RLock()


def _parse_meta(meta):
    """metadata 可能是 dict 或 JSON 字符串。"""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            import json
            d = json.loads(meta)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


class AgentMesh:
    """多 agent 委托总线：创建/认领/完成 委托（状态机 pending->claimed->done）。"""

    def __init__(self, adapter: str = "sqlite"):
        from trinity import Trinity
        self._mem = Trinity(adapter=adapter)

    def create(self, from_agent: str, to_agent: str, task: str,
               importance: float = 0.6, ttl_hours: float = 24.0) -> str:
        """创建委托（pending）。"""
        meta = {
            "delegation": {
                "status": "pending",
                "from": from_agent, "to": to_agent,
                "task": task, "claimant": None, "result": None,
                "expires_at": time.time() + ttl_hours * 3600,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            }
        }
        r = self._mem.ingest(
            f"[delegation] {from_agent} -> {to_agent}: {task}",
            agent_id=from_agent, category=CATEGORY, metadata=meta,
            importance=importance, postprocess=False)
        return r.get("memory_id", "")

    def _load(self, delegation_id: str) -> Optional[Dict[str, Any]]:
        rec = self._mem._adapter.get_memory(delegation_id)
        return rec

    def claim(self, delegation_id: str, agent: str) -> bool:
        """认领（原子：仅 pending 且未过期可认领）。"""
        with _LOCK:
            rec = self._load(delegation_id)
            if not rec:
                return False
            meta = _parse_meta(rec.get("metadata"))
            d = meta.get("delegation") or {}
            if d.get("status") != "pending":
                return False
            if time.time() > float(d.get("expires_at") or 0):
                return False
            d["status"] = "claimed"
            d["claimant"] = agent
            d["claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            try:
                import json as _json
                conn = self._mem._adapter._conn
                conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                             (_json.dumps(meta, ensure_ascii=False), delegation_id))
                conn.commit()
                return True
            except Exception:
                return False

    def complete(self, delegation_id: str, agent: str, result: str) -> bool:
        """完成（原子：仅 claimed 且认领人是本人）。"""
        with _LOCK:
            rec = self._load(delegation_id)
            if not rec:
                return False
            meta = _parse_meta(rec.get("metadata"))
            d = meta.get("delegation") or {}
            if d.get("status") != "claimed" or d.get("claimant") != agent:
                return False
            d["status"] = "done"
            d["result"] = result
            d["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            try:
                import json as _json
                conn = self._mem._adapter._conn
                conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                             (_json.dumps(meta, ensure_ascii=False), delegation_id))
                conn.commit()
                return True
            except Exception:
                return False

    def inbox(self, agent: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """收件箱：给 agent 的委托（按状态过滤）。"""
        res = self._mem.search(query="delegation", mode="keyword", top_k=50,
                               agent_id=None, category=CATEGORY,
                               include_docs=True)
        out = []
        for r in res.get("results", []):
            # 2026-08-27: search 结果不带 metadata——用 get_memory 读全量
            rec = self._mem._adapter.get_memory(r.get("memory_id")) or {}
            meta = _parse_meta(rec.get("metadata"))
            d = meta.get("delegation") or {}
            if not d:
                continue
            if d.get("to") != agent:
                continue
            if status and d.get("status") != status:
                continue
            out.append({"delegation_id": r.get("memory_id"),
                        "from": d.get("from"), "task": d.get("task"),
                        "status": d.get("status"), "claimant": d.get("claimant"),
                        "result": d.get("result")})
        return out
