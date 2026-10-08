# -*- coding: utf-8 -*-
"""检索轨迹落盘（Mano-P save-trajectory 借鉴；2026-09-09）

借鉴来源
--------
Mano-P 客户端有 save-trajectory：每步保存截图与动作，用于事后复盘与离线调优。
Trinity 目前只有"反馈"（rl_feedback_journal.jsonl）而没有"轨迹"——不知道每条
查询各通道返回了什么、延迟多少、最终选了谁，因此无法做离线回放（Mano-P 三段式
里的 SFT -> 离线 RL -> 在线 RL，Trinity 缺中间那级）。

本模块补上轨迹：每条查询落一行 JSONL，供 scripts/replay_bandit.py 离线回放。

开关（默认 off）
----------------
  TRINITY_RETRIEVAL_TRACE=on
  TRINITY_RETRIEVAL_TRACE_FILE   默认 ~/.trinity/state/retrieval_traces.jsonl
  TRINITY_RETRIEVAL_TRACE_MAX_MB 单文件上限（默认 32，超出轮转为 .1）

轨迹行格式
----------
  {"ts": float, "query": str, "agent_id": str, "profile": str, "top_k": int,
   "channels": {"keyword": [["<id>", 1.23], ...], ...},
   "fused": [["<id>", 0.9], ...], "chosen": ["<id>", ...],
   "latency_ms": float, "meta": {...},
   # ── 2026-10-07（t88/B4·A5-04）**只加不改**：会话侧(pull)再获取计费 ──
   "pull_calls_delta": 1,                       # 本行 = 一次 pull 调用
   "reacquired_hit_ids": ["<id>", ...]}         # 本次取回里**此前已取回过**的条目

为什么加这两个字段
------------------
arXiv 2608.16370（2026-08-17）逐字："compression can increase an agent's interaction cost by
**forcing it to reacquire dropped state** while leaving completion statistically unchanged."
⇒ "省 token" 不能只看注入侧（t58 的 −71% 是**注入侧**口径）：**会话侧**要把"被迫再获取"计上。
本模块是那条腿的落点 ⇒ 在此**只加**两个字段（既有字段语义一字未改，老消费者不受影响）。
口径隔离：本字段属**会话侧(pull)**；与注入侧 `opening_surface_deliveries.jsonl` **禁止相加**。
离线计费见 `scripts/session_pull_ledger.py`（`pull_calls` / `reacquired_tokens`）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 兜底：退回原静默行为
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

logger = logging.getLogger("trinity.retrieval.trace")

DEFAULT_TRACE_FILE = os.path.join("~", ".trinity", "state", "retrieval_traces.jsonl")
DEFAULT_MAX_MB = 32.0

#: 2026-10-07（t88/B4·A5-04）：判定"再获取"的**滚动窗口**（最近 N 次取回的 id）。
#: 口径写死在这里，便于离线复算与核对（200 条 ≈ 本仓当前 24h 的 pull 量级）。
REACQUIRE_WINDOW = 200


def _env_flag(name: str, default: str = "off") -> bool:
    return os.environ.get(name, default).lower() in ("on", "1", "true", "yes")


def is_enabled() -> bool:
    """轨迹开关（默认 off）。"""
    return _env_flag("TRINITY_RETRIEVAL_TRACE")


def trace_file() -> str:
    return os.path.expanduser(os.environ.get("TRINITY_RETRIEVAL_TRACE_FILE", DEFAULT_TRACE_FILE))


def _max_bytes() -> int:
    try:
        mb = float(os.environ.get("TRINITY_RETRIEVAL_TRACE_MAX_MB", DEFAULT_MAX_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MB
    return int(max(1.0, mb) * 1024 * 1024)


def _norm_hits(hits: Any, limit: int = 50) -> List[List[Any]]:
    """把各种形态的命中结果归一化为 [[id, score], ...]。"""
    out: List[List[Any]] = []
    if not hits:
        return out
    for h in list(hits)[:limit]:
        if isinstance(h, dict):
            mid = h.get("memory_id") or h.get("id")
            score = h.get("score", h.get("similarity", 0.0))
        elif isinstance(h, (list, tuple)) and len(h) >= 2:
            mid, score = h[0], h[1]
        else:
            mid, score = h, 0.0
        if mid is None:
            continue
        try:
            score = round(float(score or 0.0), 6)
        except (TypeError, ValueError):
            score = 0.0
        out.append([str(mid), score])
    return out


# ── per-channel 归因（P0-3，2026-09-10 OpenViking 借鉴）───────────────
# HybridRetriever 的 breakdown 只有计数，审计日志只存 breakdown；唯一能复原
# "这条记忆被哪个通道捞出、该通道给了多少分"的信息，是挂在每条结果行上的
# 逐通道分数键（_hybrid_search.py:826-829、hybrid_retriever.py:739-779）。
CHANNEL_SCORE_KEYS = {
    "vector_score": "vector",
    "bm25_score": "bm25",
    "graph_score": "graph",
    "aggregator_score": "aggregator",
    "procedural_score": "procedural",
    "pagetree_score": "pagetree",
    "situation_score": "situation",
    "rerank_score": "rerank",
}


def channels_from_results(results: Any, limit: int = 200) -> Dict[str, List[List[Any]]]:
    """从结果行的逐通道分数键还原 per-channel 命中：{channel: [[id, score], ...]}。

    只保留分数 > 0 的命中并按分数降序；纯读取，不改变任何检索行为。
    """
    out: Dict[str, List[List[Any]]] = {}
    for r in list(results or [])[:limit]:
        if not isinstance(r, dict):
            continue
        mid = r.get("memory_id") or r.get("id")
        if not mid:
            continue
        for key, chan in CHANNEL_SCORE_KEYS.items():
            v = r.get(key)
            if isinstance(v, (int, float)) and v > 0:
                out.setdefault(chan, []).append([str(mid), round(float(v), 6)])
    for chan in out:
        out[chan].sort(key=lambda x: -x[1])
    return out


class RetrievalTracer:
    """JSONL 轨迹写入器（线程安全、可轮转）。"""

    def __init__(self, path: Optional[str] = None, max_bytes: Optional[int] = None):
        self.path = os.path.expanduser(path or trace_file())
        self.max_bytes = int(max_bytes if max_bytes is not None else _max_bytes())
        self._lock = threading.Lock()
        # 2026-10-07（t88/B4·A5-04）：滚动记住"最近取回过的 id"（只用于**计费**，不影响检索）
        self._recent_ids: List[str] = []
        self._recent_set: set = set()

    def _remember_locked(self, ids: List[str]) -> None:
        """把最近取回的 id 记进滚动窗口（只保留 `REACQUIRE_WINDOW` 条）。

        只增不减的集合会随进程寿命无界增长 ⇒ 这里按插入顺序裁剪（超窗即丢最旧；
        仍在窗口内的 id 不会被误丢：丢弃前先确认它在剩余列表里不再出现）。
        """
        self._recent_ids.extend(ids)
        self._recent_set.update(ids)
        while len(self._recent_ids) > REACQUIRE_WINDOW:
            old = self._recent_ids.pop(0)
            if old not in self._recent_ids:
                self._recent_set.discard(old)

    # ── 写入 ───────────────────────────────────────────────────────────
    def record(
        self,
        query: str,
        channels: Optional[Dict[str, Any]] = None,
        chosen: Optional[Sequence[str]] = None,
        fused: Optional[Any] = None,
        latency_ms: float = 0.0,
        agent_id: str = "",
        profile: str = "",
        top_k: int = 0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """落一行轨迹；失败不抛异常（观测路径不得影响主流程）。"""
        rec = {
            "ts": time.time(),
            "query": (query or "")[:500],
            "agent_id": agent_id,
            "profile": profile,
            "top_k": int(top_k or 0),
            "channels": {str(k): _norm_hits(v) for k, v in (channels or {}).items()},
            "fused": _norm_hits(fused),
            "chosen": [str(c) for c in (chosen or [])],
            "latency_ms": round(float(latency_ms), 3),
            "meta": meta or {},
        }
        try:
            with self._lock:
                # 2026-10-07（t88/B4·A5-04）**只加不改**：会话侧(pull)再获取计费两个字段。
                # `pull_calls_delta=1`（本行即一次 pull）；`reacquired_hit_ids` = 本次 `chosen`
                # 里**此前已取回过**的条目（滚动窗口 REACQUIRE_WINDOW 条）。既有字段不动。
                _chosen = rec["chosen"]
                rec["pull_calls_delta"] = 1
                rec["reacquired_hit_ids"] = [c for c in _chosen if c in self._recent_set]
                self._remember_locked(_chosen)
                self._rotate_locked()
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
            return True
        except OSError as e:
            logger.warning("trace record failed: %s", e)
            return False

    def _rotate_locked(self) -> None:
        try:
            if self.max_bytes > 0 and os.path.isfile(self.path):
                if os.path.getsize(self.path) >= self.max_bytes:
                    bak = self.path + ".1"
                    if os.path.isfile(bak):
                        os.remove(bak)
                    os.replace(self.path, bak)
        except OSError as _e:
            swallow(__name__, _e)

    # ── 读取 ───────────────────────────────────────────────────────────
    def iter_traces(self, limit: int = 0) -> Iterable[Dict[str, Any]]:
        """按写入顺序读取轨迹（跳过坏行）。"""
        if not os.path.isfile(self.path):
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
                    if limit and len(out) >= limit:
                        break
        except OSError as e:
            logger.warning("iter_traces failed: %s", e)
        return out

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        return list(self.iter_traces(limit=0))[-n:]

    def stats(self) -> Dict[str, Any]:
        rows = list(self.iter_traces())
        lat = [r.get("latency_ms", 0.0) for r in rows if isinstance(r.get("latency_ms"), (int, float))]
        chan_hits: Dict[str, int] = {}
        for r in rows:
            for c, hits in (r.get("channels") or {}).items():
                if hits:  # 只统计真正有命中的通道
                    chan_hits[c] = chan_hits.get(c, 0) + 1
        return {
            "count": len(rows),
            "file": self.path,
            "avg_latency_ms": round(sum(lat) / len(lat), 3) if lat else 0.0,
            "channels_seen": chan_hits,
        }

    def clear(self) -> None:
        with self._lock:
            try:
                if os.path.isfile(self.path):
                    os.remove(self.path)
            except OSError as e:
                logger.warning("trace clear failed: %s", e)


_TRACER: Optional[RetrievalTracer] = None


def get_tracer() -> Optional[RetrievalTracer]:
    """返回轨迹写入器；开关关闭时返回 None（调用方需判空）。"""
    global _TRACER
    if not is_enabled():
        return None
    if _TRACER is None:
        _TRACER = RetrievalTracer()
    return _TRACER


def trace_if_enabled(**kwargs: Any) -> bool:
    """便捷入口：开关关闭时静默返回 False。"""
    t = get_tracer()
    if t is None:
        return False
    return t.record(**kwargs)
