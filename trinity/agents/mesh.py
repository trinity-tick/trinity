# -*- coding: utf-8 -*-
"""mesh.py — AgentMesh 委托机制（2026-08-27 方向B）。

记忆即协作总线：delegation 记忆类型，原子状态机 pending->claimed->done/expired。
create/claim/complete 事件通知（delegation.created/claimed）——automation 规则可响应。
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
    """多 agent 委托总线：创建/认领/完成 委托（状态机 pending->claimed->done/expired）。"""

    def __init__(self, adapter: str = "sqlite"):
        from trinity import Trinity
        self._mem = Trinity(adapter=adapter)

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        try:
            from trinity.automation import emit as _emit
            _emit(event, payload)
        except Exception:
            pass

    def _expire_stale(self) -> int:
        """2026-08-27: pending 超时自动回收 -> expired。返回过期数。"""
        import json as _json
        n = 0
        conn = self._mem._adapter._conn
        rows = conn.execute(
            "SELECT memory_id, metadata FROM memories WHERE category=? AND status='active'",
            (CATEGORY,)).fetchall()
        for row in rows:
            d = _parse_meta(row["metadata"]).get("delegation") or {}
            if d.get("status") != "pending":
                continue
            try:
                if time.time() > float(d.get("expires_at") or 0):
                    d["status"] = "expired"
                    d["expired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
                    conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                                 (_json.dumps({"delegation": d}, ensure_ascii=False),
                                  row["memory_id"]))
                    n += 1
            except Exception:
                continue
        if n:
            conn.commit()
            self._emit("delegation.expired", {"count": n})
        return n

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
            "[delegation] " + from_agent + " -> " + to_agent + ": " + task,
            agent_id=from_agent, category=CATEGORY, metadata=meta,
            importance=importance, postprocess=False)
        self._emit("delegation.created", {"from": from_agent, "to": to_agent,
                                          "task": task,
                                          "delegation_id": r.get("memory_id", "")})
        return r.get("memory_id", "")

    def _load(self, delegation_id: str) -> Optional[Dict[str, Any]]:
        return self._mem._adapter.get_memory(delegation_id)

    def claim(self, delegation_id: str, agent: str) -> bool:
        """认领（原子：仅 pending 且未过期可认领）。"""
        self._expire_stale()
        import json as _json
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
                conn = self._mem._adapter._conn
                conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                             (_json.dumps(meta, ensure_ascii=False), delegation_id))
                conn.commit()
                self._emit("delegation.claimed", {"delegation_id": delegation_id,
                                                  "agent": agent})
                return True
            except Exception:
                return False

    def complete(self, delegation_id: str, agent: str, result: str) -> bool:
        """完成（原子：仅 claimed 且认领人是本人）。"""
        import json as _json
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
                conn = self._mem._adapter._conn
                conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                             (_json.dumps(meta, ensure_ascii=False), delegation_id))
                conn.commit()
                self._emit("delegation.completed", {"delegation_id": delegation_id,
                                                    "agent": agent})
                return True
            except Exception:
                return False

    def inbox(self, agent: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """收件箱：给 agent 的委托（按状态过滤）。"""
        self._expire_stale()
        res = self._mem.search(query="delegation", mode="keyword", top_k=50,
                               agent_id=None, category=CATEGORY,
                               include_docs=True)
        out = []
        for r in res.get("results", []):
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

    def decompose(self, from_agent: str, to_agent: str, parent_task: str,
                 subtasks: List[str], ttl_hours: float = 24.0) -> List[str]:
        """2026-08-27 (multi-agent): split big task into subtasks (parent link)."""
        ids = []
        import json as _json
        conn = self._mem._adapter._conn
        for i, st in enumerate(subtasks):
            did = self.create(from_agent, to_agent,
                              "[" + str(i + 1) + "/" + str(len(subtasks)) + "] " + st,
                              ttl_hours=ttl_hours)
            rec = self._mem._adapter.get_memory(did)
            meta = _parse_meta(rec.get("metadata"))
            d = meta.get("delegation") or {}
            d["parent"] = parent_task
            d["subtask_index"] = i
            conn.execute("UPDATE memories SET metadata=? WHERE memory_id=?",
                         (_json.dumps(meta, ensure_ascii=False), did))
            conn.commit()
            ids.append(did)
        return ids

    def agent_quota(self, agent: str, max_active: int = 5) -> bool:
        """2026-08-27 (quota): agent active delegations (pending+claimed) limit."""
        self._expire_stale()
        active = self.inbox(agent, "pending") + self.inbox(agent, "claimed")
        return len(active) < max_active

