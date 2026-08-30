#!/usr/bin/env python3
"""trinity/brain/working_memory.py — 会话工作记忆与注意机制（2026-09，EXECUTION 105.6）

认知依据：大脑工作记忆容量受限（7±2，Miller 1956），由【注意】门控——
高注意项驻留、低注意项被驱逐；检索时工作记忆中的项获得注意优先。

实现：进程内会话级缓冲（TTL 过期）——
  - 容量：默认 7（可配，Miller 7±2）；
  - 注意权重 attention = 0.5*recency + 0.3*importance + 0.2*log(1+hits)；
  - 驱逐：容量满时按 attention 驱逐最低项；
  - 检索命中（touch）提升注意（模拟注意回响）。

线程安全（FastAPI 多线程）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.brain.wm")

DEFAULT_CAPACITY = 7
DEFAULT_TTL = 3600  # 1 小时未触碰过期


class WorkingMemory:
    """容量受限的会话工作记忆（进程内单例）。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, ttl: float = DEFAULT_TTL):
        self._capacity = max(2, min(capacity, 9))  # Miller 7±2 边界
        self._ttl = ttl
        self._lock = threading.Lock()
        # session_id -> OrderedDict[key -> {content, ts, hits, importance}]
        self._sessions: Dict[str, OrderedDict] = {}

    def _attention(self, item: Dict[str, Any], now: float) -> float:
        age_h = max(0.0, (now - item["ts"]) / 3600.0)
        recency = 1.0 / (1.0 + age_h)
        importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
        hits = float(item.get("hits", 0))
        return 0.5 * recency + 0.3 * importance + 0.2 * min(1.0, hits / 5.0)

    def push(self, session_id: str, key: str, content: str,
             importance: float = 0.5) -> Dict[str, Any]:
        """写入（或刷新）一条工作记忆；容量满时驱逐注意最低项。"""
        now = time.time()
        with self._lock:
            buf = self._sessions.setdefault(session_id, OrderedDict())
            if key in buf:
                old = buf.pop(key)
                importance = max(float(importance), float(old.get("importance", 0.5)))
            buf[key] = {
                "content": str(content)[:1000],
                "ts": now,
                "hits": 0,
                "importance": importance,
            }
            # 驱逐：容量超限时移除 attention 最低项
            while len(buf) > self._capacity:
                evict_key = min(
                    buf, key=lambda k: self._attention(buf[k], now))
                del buf[evict_key]
            return {"session_id": session_id, "key": key, "size": len(buf),
                    "capacity": self._capacity}

    def touch(self, session_id: str, key: str) -> bool:
        """检索命中：刷新时间 + 注意回响。"""
        now = time.time()
        with self._lock:
            buf = self._sessions.get(session_id)
            if not buf or key not in buf:
                return False
            item = buf.pop(key)
            item["ts"] = now
            item["hits"] = item.get("hits", 0) + 1
            buf[key] = item
            return True

    def get(self, session_id: str, top_k: Optional[int] = None,
            expire: bool = True) -> List[Dict[str, Any]]:
        """按注意权重降序返回工作记忆（含 attention 分数）。"""
        now = time.time()
        with self._lock:
            buf = self._sessions.get(session_id)
            if not buf:
                return []
            items = []
            for key, item in buf.items():
                if expire and now - item["ts"] > self._ttl:
                    continue
                items.append({
                    "key": key,
                    "content": item["content"],
                    "attention": round(self._attention(item, now), 3),
                    "hits": item.get("hits", 0),
                    "importance": item.get("importance", 0.5),
                    "age_s": round(now - item["ts"], 1),
                })
            items.sort(key=lambda x: -x["attention"])
            if top_k:
                items = items[:top_k]
            return items

    def keys(self, session_id: str) -> List[str]:
        with self._lock:
            buf = self._sessions.get(session_id)
            return list(buf.keys()) if buf else []

    def clear(self, session_id: str) -> int:
        with self._lock:
            buf = self._sessions.pop(session_id, None)
            return len(buf) if buf else 0


# 进程级单例（API 进程共享）
_WM: Optional[WorkingMemory] = None
_WM_LOCK = threading.Lock()


def get_working_memory() -> WorkingMemory:
    global _WM
    if _WM is None:
        with _WM_LOCK:
            if _WM is None:
                _WM = WorkingMemory()
    return _WM
