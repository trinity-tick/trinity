#!/usr/bin/env python3
"""
Trinity REST API Server — retrieval routes (/memory/search/*, /embeddings, /vector/*, /reason).
"""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from ._deps import _live_memory as get_memory
from ._models import (
    CrossModalSearchRequest,
    HybridSearchRequest,
    ImageByTextRequest,
    TextByImageRequest,
)
from ._observability import record_retrieval

# 2026-09-30：本模块此前**没有 logger**（只用 `swallow` 做静默失败治理）。
# 我加的看门狗必须**能喊出来** —— 池被重建是有运维意义的事件，
# 不能吞掉；否则"自愈发生过"这件事在日志里不可见。
import logging as _logging

_log = _logging.getLogger(__name__)

try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
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

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# 2026-09-29（用户授权 ③）：**检索请求级硬上限 + 解除事件循环阻塞**
#
# ## 实测来由（这条是本轮唯一"让服务被杀"的机制）
#
# `/memory/search/hybrid` 是 `async def`，却在**事件循环里同步**调用
# `mem.search_hybrid(...)` ⇒ 一次慢检索把**整个事件循环**堵住，于是：
#   · `/health` 实测 60 s 无响应；
#   · 监督器健康守卫判 `UNHEALTHY beyond grace` ⇒ **kill + restart**
#     （19:39:23 / 19:47:41 两次，见 `output/memory_stall_correlation.md`）；
#   · 单次查询观测到 221,159 ms 返回 / 302,002 ms 后连接被重置。
# 即"一个慢查询"= "整站不可用"。
#
# ## 处置三条（都有开关，都可回滚）
#
# 1. 引擎调用移入**有界线程池**（`TRINITY_SEARCH_POOL_SIZE`，默认 2）
#    ⇒ 事件循环立刻自由：健康检查与其它请求不再被一个慢查询拖死。
# 2. `asyncio.wait_for` + **硬上限**（`TRINITY_SEARCH_DEADLINE_S`，默认 20s）
#    ⇒ 超时返回 **503 + 可读降级体**，而不是挂死或静默空结果。
# 3. **不在池里排队**：池满立刻 503（`search_pool_saturated`）——
#    排队会把"挂死"换成"雪崩"。
#
# ## 如实记录的限度
#
# Python 无法安全杀线程 ⇒ 超时那一次引擎调用**仍在后台跑完**（用 `asyncio.shield`
# 保护它不被取消）；信号量只在它**真正结束**时释放 ⇒ 在途任务数上界 = 池大小，
# 不会无界堆积。这是"可用的硬超时"在同步引擎上的**最好形态**，不是"零成本取消"。
_SEARCH_POOL_SIZE = max(1, int(os.environ.get("TRINITY_SEARCH_POOL_SIZE", "2") or 2))
_SEARCH_DEADLINE_S = float(os.environ.get("TRINITY_SEARCH_DEADLINE_S", "20") or 20)
_SEARCH_POOL: Optional[ThreadPoolExecutor] = None
_SEARCH_SLOTS = threading.BoundedSemaphore(_SEARCH_POOL_SIZE)
_SEARCH_STATS = {"submitted": 0, "ok": 0, "timeout": 0, "saturated": 0, "error": 0}
#: 2026-10-06（复评 G5）：`/vector/search` 的**独立**计数。
#: 该端点刻意不占 `_SEARCH_SLOTS`（共享池默认仅 2 槽，慢向量检索会挤掉并发 hybrid），
#: 因此它的成功/超时**不该记进 `_SEARCH_STATS`**（会污染"有界检索池"的口径）；
#: 但"不占池"不等于"不用计量" —— 此前它的超时在任何 metric 里都看不见，
#: 运维无从知道它是否在频繁超时。故单开一份计数，由 `/metrics` 暴露。
_VECTOR_STATS = {"requests": 0, "ok": 0, "timeout": 0, "error": 0}
#: 2026-09-29（①‑A 第三次修正）：**在途检索计数**。给启动预热用 ——
#: 预热是"批量活"，绝不允许挡在请求前面（实测过：预热霸占串行 ONNX ⇒
#: 请求排队超硬上限 ⇒ 503 + 孤儿占槽 ⇒ 池满 ⇒ 搜索整体不可用）。
_SEARCH_INFLIGHT = {"n": 0}
_SEARCH_INFLIGHT_LOCK = threading.Lock()


def retrieval_health(data: dict, results: Any, stats: Optional[Dict[str, Any]] = None) -> dict:
    """**给"零结果"一个可分辨的来源**（2026-10-02 事故后加固）。

    事故背景（实测）：权威库损坏时 `POST /memory/search/hybrid` 返回
    **HTTP 200 + `results: []`**，调用方无法区分：
      (a) 检索面健康、确实没有相关记忆（**正确答案**）；
      (b) 检索面坏了（拿不到 adapter / 库打不开），却装作"没有"（**fail-open**）。
    后者是最危险的失败形态：上游会把**故障**读成**无证据**，进而继续编答案。

    ## 判定分两层，刻意不对等（纪律：不把特征当故障）

    · **硬故障** `no_adapter`：`vector_channel` 形如 `none:*` / `unknown:*`
      ⇒ adapter 根本拿不到 ⇒ 每个通道都必然为 0。因果明确，置 `ok=False`。
    · **特征信号** `all_channels_zero`（full 路由且各通道贡献全 0）：本次事故**会**
      呈现这个形状，但它**同样**是"健康面 + 查询词在库中确实不存在"的形状
      ⇒ **只作信号**，单独出现不置 `ok=False`（否则会在健康系统上制造假红）。
    · `all_results_weak`：非空但全是弱分，与 metacognition 的 gap_hint 同口径，仅标注。

    本函数**不改检索行为**、不新增探针，只读已有字段；`stats` 传则顺带累加计数器。
    """
    br_raw = data.get("breakdown")
    br: Dict[str, Any] = br_raw if isinstance(br_raw, dict) else {}
    vc = str(br.get("vector_channel") or "")
    chans_raw = br.get("channels")
    chans: List[Any] = list(chans_raw) if isinstance(chans_raw, list) else []
    res: List[Any] = list(results) if isinstance(results, (list, tuple)) else []
    n = len(res)

    reasons: List[str] = []
    if vc.startswith("none:") or vc.startswith("unknown:"):
        reasons.append("no_adapter:" + (vc or "unknown"))

    all_zero = False
    if str(br.get("routing") or "") == "full" and n == 0:
        contrib = {k: br.get(k) for k in
                   ("vector", "bm25", "graph", "aggregator", "procedural", "pagetree")}
        if contrib and all((v or 0) == 0 for v in contrib.values()):
            all_zero = True
            reasons.append("all_channels_zero")

    if n and all(float(r.get("score") or 0) <= 0.15 for r in res if isinstance(r, dict)):
        reasons.append("all_results_weak")

    degraded = [r for r in reasons
                if not r.startswith("all_results_weak") and r != "all_channels_zero"]

    if stats is not None:
        stats["zero_result_with_fault"] = stats.get("zero_result_with_fault", 0) + (
            1 if (n == 0 and degraded) else 0)
        stats["zero_result_healthy"] = stats.get("zero_result_healthy", 0) + (
            1 if (n == 0 and not degraded) else 0)
        stats["zero_result_all_channels_zero"] = stats.get(
            "zero_result_all_channels_zero", 0) + (1 if all_zero else 0)

    return {
        "ok": not degraded,
        "n_results": n,
        "channels": chans,
        "vector_channel": vc,
        "degraded_reason": (",".join(degraded) or None),
        "signals": (["all_channels_zero"] if all_zero else None),
        "note": ("ok=false ⇒『空结果』**不代表**没有相关记忆，而是检索面故障"
                 "（拿不到 adapter）；请勿把空结果读成『无证据』。"
                 "ok=true 且 n_results=0 才是真·无相关记忆。"
                 "signals=all_channels_zero 表示 full 路由下各通道贡献均为 0 —— "
                 "在 ok=true 时这通常意味着查询词在库中确实不存在，"
                 "但在 ok=false 时它是同一形状的佐证。"),
    }


def _inflight_searches() -> int:
    """当前在**有界执行器**里跑的检索数（预热据此让路）。"""
    with _SEARCH_INFLIGHT_LOCK:
        return int(_SEARCH_INFLIGHT["n"])


class SearchDeadlineExceeded(Exception):
    """检索超过硬上限（调用方据此返回 503 + 降级体）。"""


class SearchPoolSaturated(Exception):
    """有界池已满 ⇒ 立刻 503（不排队）。"""


def search_deadline_status() -> dict:
    """自述上限配置与计数（供调用方/诊断核查；不改行为）。"""
    return {
        "deadline_s": _SEARCH_DEADLINE_S,
        "pool_size": _SEARCH_POOL_SIZE,
        "slots_free": getattr(_SEARCH_SLOTS, "_value", None),
        "stats": dict(_SEARCH_STATS),
    }


def _search_pool() -> ThreadPoolExecutor:
    global _SEARCH_POOL
    if _SEARCH_POOL is None:
        _SEARCH_POOL = ThreadPoolExecutor(max_workers=_SEARCH_POOL_SIZE,
                                          thread_name_prefix="search-bounded")
    return _SEARCH_POOL


# ── 2026-09-30（外部审计 · 目标项 15/16 现场抓到的真实故障）：**卡死槽位自愈**
#
# 现场（线上实测）：`search_inflight` **恒为 2**、`pool_saturated: True`、
# `search_saturated_total 79`，`/memory/search/hybrid` 的 breakdown 一直是
# `{'lexical_only': True, 'pool_saturated': True}` —— 即：**多通道全路径已经
# 永久回不来了，只剩词法降级**（降级本身是第 1 轮的可用性修复，工作正常）。
#
# 根因：槽位只在 `_runner` 的 `finally` 里释放，也就是**只有 `call()` 真正返回
# 才归还**。若 `call()` 永久卡住（阻塞在嵌入器锁 / 读锁 / 线程死锁上），
# 槽位**永不归还**；而池容量与信号量容量都是 2 ⇒ **两次卡死就把池永久占死**。
# 这不是"慢查询"，是**容量泄漏且无自愈**。
#
# Python 杀不掉线程，所以正确的修法是**放弃并重建**：
# 当「全部槽位都被占用」且「最老的占用已超过 STUCK 阈值」时，
# 丢弃旧池（`shutdown(wait=False)`，卡死线程被孤立但不再计入容量），
# 换一个新的池与信号量 —— 服务随即恢复多通道能力，无需重启。
_SEARCH_ACQ_TIMES: list = []           # 当前占用中槽位的获取时间（monotonic）
_SEARCH_ACQ_LOCK = threading.Lock()
_SEARCH_RECOVERIES = {"n": 0}

#: 全部槽位被占用多久才判定"卡死"并重建（默认 max(90s, deadline×4)）。
_SEARCH_STUCK_S = float(os.environ.get(
    "TRINITY_SEARCH_STUCK_S", str(max(90.0, _SEARCH_DEADLINE_S * 4))) or 90)


def _stuck_seconds() -> float:
    with _SEARCH_ACQ_LOCK:
        if not _SEARCH_ACQ_TIMES:
            return 0.0
        return time.monotonic() - min(_SEARCH_ACQ_TIMES)


def vector_search_status() -> dict:
    """`/vector/search` 的计数快照（供 /metrics 与判据读取）。

    2026-10-06（复评 G5）：与 `search_pool_status()` 并列但**互不混用** ——
    向量端点不占检索池，故它的数字不得进 `_SEARCH_STATS`，也不得被读成池指标。
    """
    return {
        "stats": dict(_VECTOR_STATS),
        "deadline_s": _SEARCH_DEADLINE_S,
        "uses_search_pool": False,
    }


def search_pool_status() -> dict:
    """池健康快照（供 /metrics 与判据读取）。"""
    with _SEARCH_ACQ_LOCK:
        held = len(_SEARCH_ACQ_TIMES)
    return {
        "pool_size": _SEARCH_POOL_SIZE,
        "slots_held": held,
        "stuck_seconds": round(_stuck_seconds(), 1),
        "stuck_threshold_s": _SEARCH_STUCK_S,
        "recoveries": int(_SEARCH_RECOVERIES["n"]),
    }


def maybe_recover_search_pool() -> bool:
    """池被卡死时重建它，恢复并发与多通道能力。返回是否发生了恢复。

    判据：**全部**槽位都被占用，且最老的占用超过 ``_SEARCH_STUCK_S``。
    （只占满但仍在推进的池**不会**被误重建 —— 阈值远大于 `_SEARCH_DEADLINE_S`。）
    """
    global _SEARCH_POOL, _SEARCH_SLOTS
    with _SEARCH_ACQ_LOCK:
        held = len(_SEARCH_ACQ_TIMES)
        oldest = min(_SEARCH_ACQ_TIMES) if _SEARCH_ACQ_TIMES else None
    if held < _SEARCH_POOL_SIZE or oldest is None:
        return False
    stuck = time.monotonic() - oldest
    if stuck < _SEARCH_STUCK_S:
        return False

    try:
        if _SEARCH_POOL is not None:
            _SEARCH_POOL.shutdown(wait=False)
    except Exception:  # noqa: BLE001 — 重建不能因为收尾失败而放弃
        swallow(__name__, None)
    _SEARCH_POOL = None
    _SEARCH_SLOTS = threading.BoundedSemaphore(_SEARCH_POOL_SIZE)
    with _SEARCH_ACQ_LOCK:
        _SEARCH_ACQ_TIMES.clear()
    _SEARCH_RECOVERIES["n"] += 1
    _SEARCH_STATS["pool_recovered"] = _SEARCH_STATS.get("pool_recovered", 0) + 1
    _log.warning(
        "search pool RECOVERED: %d/%d slot(s) stuck for %.0fs (>= %.0fs) — "
        "旧池已丢弃并重建，多通道检索恢复；卡死的线程无法被 Python 杀死，"
        "它们被孤立且不再计入容量",
        held, _SEARCH_POOL_SIZE, stuck, _SEARCH_STUCK_S)
    return True


async def run_search_bounded(call):
    """在有界池里跑 `call()`；超硬上限抛 `SearchDeadlineExceeded`，池满抛 `SearchPoolSaturated`。

    这是"慢查询 → 503"取代"慢查询 → 被健康守卫 kill"的那一步。

    2026-09-30：进入前先跑一次**卡死自愈**（见 `maybe_recover_search_pool`），
    否则两次永久卡死就会让多通道路径万劫不复、只能靠重启恢复。
    """
    maybe_recover_search_pool()
    if not _SEARCH_SLOTS.acquire(blocking=False):
        _SEARCH_STATS["saturated"] += 1
        raise SearchPoolSaturated()
    _SEARCH_STATS["submitted"] += 1
    loop = asyncio.get_running_loop()
    _slot_mark = time.monotonic()
    with _SEARCH_ACQ_LOCK:
        _SEARCH_ACQ_TIMES.append(_slot_mark)
    with _SEARCH_INFLIGHT_LOCK:
        _SEARCH_INFLIGHT["n"] += 1

    # 2026-09-30（外部审计 · 目标项 16）：**给"引擎调用到底跑了多久"补留痕**。
    #
    # 现场：服务侧首个 full 查询恒定 ~20,142ms 并降级（`lexical_only: True` 来自
    # `_degraded_response`），而**同一个查询在进程内只要 0.20s**；
    # 日志里 grep `lexical|degrad|timeout|deadline|embed` **命中 0 行** ⇒ 只能靠猜。
    #
    # 下面两行让"猜"变成"读"：提交时记参数与起点，**调用真正结束时**记真实耗时。
    # 关键区分：若真实耗时 ≈ 20–25s ⇒ 是"略超上限"；若几百秒 ⇒ 是"卡住不放"。
    # 二者要修的地方完全不同。
    _t_submit = time.monotonic()

    def _runner():
        try:
            return call()
        finally:
            _elapsed = time.monotonic() - _t_submit
            if _elapsed >= _SEARCH_DEADLINE_S:
                _log.warning(
                    "bounded search FINISHED after %.1fs (deadline %.1fs, over by %.1fs) "
                    "— 该调用已超限，调用方早已拿到降级体；此处记录真实耗时以便定位",
                    _elapsed, _SEARCH_DEADLINE_S, _elapsed - _SEARCH_DEADLINE_S)
            with _SEARCH_INFLIGHT_LOCK:
                _SEARCH_INFLIGHT["n"] -= 1
            # 槽位在**任务真正结束**时释放，且放在**工作线程**里而不是事件循环回调里。
            # 2026-09-29 自查（判据抓到我自己的缺陷）：原先用 `fut.add_done_callback(...)`
            # 释放 —— 那要等事件循环再转一圈；若循环在此期间关闭（进程收尾、`asyncio.run`
            # 逐次调用、测试收尾），回调永不执行 ⇒ **每次超时永久漏掉一个槽位**，
            # 最终把池占死（判据 `test_超时后槽位最终会释放_不无界堆积` 实测 value=0）。
            #
            # 2026-09-30：这里**不能用 `return` 提前退出 finally**（Python 会发
            # `SyntaxWarning: 'return' in a 'finally' block`，且会吞掉飞行中的异常）。
            # 改为布尔标记 + 条件释放。
            _still_ours = True
            with _SEARCH_ACQ_LOCK:
                try:
                    _SEARCH_ACQ_TIMES.remove(_slot_mark)
                except ValueError:
                    # 已被看门狗重建（旧池的占用记录已清空）⇒ 不能再归还新信号量，
                    # 否则会把新池的容量放大（BoundedSemaphore 会抛 ValueError）。
                    _still_ours = False
            if _still_ours:
                try:
                    _SEARCH_SLOTS.release()
                except ValueError as _e:  # noqa: BLE001 — 过期 runner 对已被替换的信号量放行失败
                    # 2026-09-30：原为静默 `pass`，被 `structure_gate` 的
                    # `silent_failure:no_growth` 棘轮计入 ⇒ 改为可听见（DEBUG 级，不刷屏）。
                    _log.debug("stale search runner could not release slot: %s", str(_e)[:80])

    try:
        fut = loop.run_in_executor(_search_pool(), _runner)
    except Exception:
        with _SEARCH_INFLIGHT_LOCK:
            _SEARCH_INFLIGHT["n"] -= 1
        _SEARCH_SLOTS.release()          # 连提交都失败 ⇒ 立即归还（runner 不会被执行）
        raise
    try:
        out = await asyncio.wait_for(asyncio.shield(fut), timeout=_SEARCH_DEADLINE_S)
    except asyncio.TimeoutError:
        _SEARCH_STATS["timeout"] += 1
        raise SearchDeadlineExceeded()
    except Exception:
        _SEARCH_STATS["error"] += 1
        raise
    _SEARCH_STATS["ok"] += 1
    return out


def _deadline_response(request, reason: str) -> JSONResponse:
    """503 降级体：**可读、可归因**，绝不给一个"看起来像正常空结果"的东西。"""
    return JSONResponse(status_code=503, content={
        "status": "timeout" if reason == "search_deadline_exceeded" else "saturated",
        "degraded_reason": reason,
        "deadline_s": _SEARCH_DEADLINE_S,
        "pool_size": _SEARCH_POOL_SIZE,
        "query": getattr(request, "query", None),
        "results": [],
        "breakdown": {"deadline_exceeded": reason == "search_deadline_exceeded",
                      "pool_saturated": reason == "search_pool_saturated"},
        "note": ("检索超过硬上限（TRINITY_SEARCH_DEADLINE_S）⇒ 返回 503 而不是挂死；"
                 "该次引擎调用仍在后台跑完（无法安全杀线程），但**不阻塞事件循环**。"
                 "调大上限或池大小请改这两个环境变量。"),
    })


# ═══════════════════════════════════════════════════════════════════════════
# 2026-09-30 可用性修复：池满/超时 ⇒ **降级为纯词法仍可用**，而不是整站不可用
# ═══════════════════════════════════════════════════════════════════════════
#
# 现场（本机实测，外部审计期间复现）：
#   一次 full 查询超 20 s 硬上限 ⇒ 孤儿线程占住 1 个槽位（Python 无法安全杀线程）。
#   池只有 _SEARCH_POOL_SIZE=2 个槽 ⇒ **两个孤儿就把池占满**，
#   此后所有查询在 16–82 ms 内快速 503。
#   而按本仓自己的实测成本（语料嵌入 0.225–0.30 s/行 × ~2 万行 = 75–100 分钟），
#   孤儿要跑**一小时以上**才归还槽位 —— 即「一次慢查询 = 搜索停摆一小时」。
#   实测指标：trinity_search_inflight 2 / trinity_search_timeouts_total 2 /
#            trinity_search_saturated_total 8。
#
# 处置：**保留**底层契约（run_search_bounded 仍按原样抛异常、_deadline_response
# 仍是 503 可读体、指标名不变），只在**路由层**加一条回退：池满/超时时改用
# 纯词法检索（FTS/LIKE，不经过有界池）继续服务，并**明确标注**降级。
#
# 为什么不违反本仓"绝不返回看起来像正常空结果"的原则：
#   降级体带 `degraded: true` + `degraded_reason` + `lexical_only: true`，
#   调用方可决定性判定；它给出的是**真结果**，比"诚实的空 503"更有用且同样诚实。
#   只有当回退本身也失败/超时，才回落到原 503 体。
_LEXICAL_FALLBACK_TIMEOUT_S = float(
    os.environ.get("TRINITY_LEXICAL_FALLBACK_TIMEOUT_S", "8") or 8)


def _lexical_fallback(mem, query: str, top_k: int, request) -> tuple:
    """在**一次性守护线程**里跑纯词法检索；返回 ``(rows, err)``。

    为什么是一次性线程：有界池此刻已被孤儿占满（这正是本函数被调用的原因），
    再走池必然又被拒；而直接在事件循环里同步跑会阻塞整个循环（那正是要避免的病）。
    每次调用一个新线程 ⇒ 单个卡住的回退不会累积成新的池级孤儿。
    """
    box: dict = {}

    def _run() -> None:
        try:
            adapter = getattr(mem, "_adapter", None)
            if adapter is None:
                box["rows"] = []
                return
            # 2026-09-30（外部审计 · 目标项 15 现场抓到）：**必须把请求的完整作用域带下来**。
            #
            # 此前只传了 agent/persona/tenant 三个，**漏了 `include_docs`**（以及
            # app/session/category/visibility）。后果不是"通道变少"，而是
            # **静默改变结果的作用域**：调用方要文档语料，兜底却把 `doc:*` 整类排除 ——
            # 实测复现：`/memory/search/hybrid` 带 `include_docs=true` 时
            # `degraded=true`，返回的 10 条**一条 doc 都没有**（全是 episodic/reflection/…，
            # 且 `source_file` 缺失），而库里 doc 行有 2,939 条带 `source_file`。
            # 这正是 `docs_corpus_hybrid_20260916`（R@10 0.35）在当前代码上
            # 测成 0.0、20/20 题"未解析"的直接原因。
            box["rows"] = adapter.search_memories(
                query=query,
                top_k=max(1, int(top_k or 10)),
                agent_id=getattr(request, "agent_id", None),
                persona_id=getattr(request, "persona_id", None),
                tenant_id=getattr(request, "tenant_id", None),
                app_id=getattr(request, "app_id", None),
                session_id=getattr(request, "session_id", None),
                category=getattr(request, "category", None),
                include_docs=bool(getattr(request, "include_docs", False)),
                visibility_rule=getattr(request, "visibility_rule", None),
            )
            # 2026-09-30（外部审计 · 补读记账）：**降级路径也必须记「读了哪些」**。
            #
            # 现场：读计数（`memory_utilization_audit.py` 的 U1a）是从
            #   `audit_log where action in ('search','search_hybrid') and details ? 'memory_ids'`
            # 数的；而那条记录由 `search_hybrid` 自己写。**降级路径从不调用
            # `search_hybrid`**（它直接走 `adapter.search_memories`）⇒ 走降级的检索
            # **一条都不计数**。指标自己的文档就写着这一点：
            # 「读数 0 表示「没记录」而**不是**「没人读」」。
            # 后果（实测）：服务曾长期被卡死槽位降级（第 14 轮才修好），
            # 那段时间的读取全部未被记账 ⇒ U1a 读数塌陷，看起来像"没人用"。
            #
            # 记账口径与主路径**逐字一致**（同一个 `action='search_hybrid'`、
            # 同样取前 10 个 memory_id ⇒ 同为**下界**），这样指标口径不因路径而分叉。
            # 位置：本函数已在**一次性守护线程**里跑，同步 SQLite 写不会阻塞事件循环。
            try:
                _w = getattr(adapter, "write_audit_log", None)
                if callable(_w) and box["rows"]:
                    _w(memory_id=None, action="search_hybrid",
                       agent_id=getattr(request, "agent_id", None),
                       persona_id=getattr(request, "persona_id", None),
                       details={
                           "query": query,
                           "top_k": max(1, int(top_k or 10)),
                           "strategy": "lexical_fallback",   # 明确标注这是降级路径写的
                           "degraded": True,
                           "hits": len(box["rows"]),
                           "memory_ids": [r.get("memory_id") for r in box["rows"]
                                          if isinstance(r, dict) and r.get("memory_id")][:10],
                       })
            except Exception as _ae:  # noqa: BLE001 — 记账失败绝不影响返回结果
                _log.debug("degraded-path audit write skipped: %s", str(_ae)[:80])
        except Exception as _e:  # noqa: BLE001 — 由调用侧转成降级原因
            box["err"] = _e

    _t = threading.Thread(target=_run, daemon=True, name="search-lexical-fallback")
    _t.start()
    _t.join(_LEXICAL_FALLBACK_TIMEOUT_S)
    if _t.is_alive():
        return None, "lexical_fallback_timeout"
    if "err" in box:
        return None, f"lexical_fallback_error:{type(box['err']).__name__}"
    return (box.get("rows") or []), None


def _degraded_response(request, reason: str, rows, note_extra: str = "") -> JSONResponse:
    """200 降级体：结果是真的，但**明确标注**它来自词法回退而非完整引擎。

    2026-09-30（字段一致性）：回退行来自 `adapter.search_memories`，带的是
    `score` 字段，而完整引擎路径的消费者读的是 **`hybrid_score`**（见
    `_routers_explain.py:126`、`_search.py:277` 的 `r.setdefault("score",
    r.get("hybrid_score", 0.0))`）。若不映射，降级响应里 `hybrid_score` 恒为 0
    ⇒ 消费者会把"有结果"读成"零相关"。此处把 `score` 镜像到 `hybrid_score`
    （不覆盖已有的）。
    """
    try:
        for _r in (rows or []):
            if isinstance(_r, dict) and "hybrid_score" not in _r:
                _r["hybrid_score"] = _r.get("score", 0)
    except Exception as _e:  # noqa: BLE001 — 字段映射失败不得让降级响应 500
        _log.debug("degraded-response score mirroring skipped: %s", str(_e)[:80])
    return JSONResponse(status_code=200, content={
        "status": "degraded",
        "degraded": True,
        "degraded_reason": reason,
        "lexical_only": True,
        "deadline_s": _SEARCH_DEADLINE_S,
        "pool_size": _SEARCH_POOL_SIZE,
        "query": getattr(request, "query", None),
        "results": rows,
        "count": len(rows),
        "breakdown": {
            "lexical_only": True,
            "deadline_exceeded": reason == "search_deadline_exceeded",
            "pool_saturated": reason == "search_pool_saturated",
        },
        "note": ("有界检索池已满或上次检索超硬上限 ⇒ 本次**降级为纯词法检索**"
                 "（不经引擎，结果可能少于完整混合检索）。池槽位由孤儿任务占用，"
                 "它跑完后自动恢复；期间搜索仍然可用。"
                 + (" " + note_extra if note_extra else "")),
    })


@router.post("/memory/search/lexical-rerank")
async def lexical_rerank_search(request: HybridSearchRequest):
    """doc 域「引擎召回 + 确定性词法重排」——**新增的可选路由，既有路径一行未改**。

    动机（2026-09-21 实测，同一 20 题 doc golden set、3 轮中位；产物 output/_route_composition_20260921.json，
    由 temp/_route_composition_20260921.py 在「活引擎 + 本模块 + 生产 PG」上复现）：
      引擎自身（cascade，50 深）      R@1 0.550 / R@3 0.750 / R@5 0.800 / R@10 0.900
      本组合（引擎候选 + 本重排）      R@1 **0.850** / R@3 0.900 / R@5 0.900 / R@10 0.900
      离线整语料文档级 BM25（参照臂）  R@1 0.900 / R@10 1.000
    ⇒ 引擎索引信息是全的，丢信息的是融合排序；本路由把排序交回确定性方法。

    实现：内部按 top_k×5（≥50，封顶 200）**过取**候选 → 用 `doc_lexical_rerank`
    （按 source 聚合的文档级 BM25 + 章节标题 3x + 中文 bigram，无 LLM）重排 → 取前 top_k。
    任何异常 **fail-open** 返回未重排结果，并在响应里带 `lexical_rerank` 元数据供调用方核查：
    其中 `enabled/reason` 把三种「没重排」（empty_index / no_query_token_match /
    no_candidate_evidence）分开标注 —— 否则「enabled=True 但顺序没动」无法归因。
    """
    mem = get_memory()
    try:
        from trinity.retrieval.doc_lexical_rerank import search_with_rerank as _swr

        # 过取深度（top_k×5，≥50，封顶 200）与 fail-open 都在共用实现里 ——
        # REST / CLI / MCP 三个面共用同一份，避免"同一逻辑 N 份、口径漂移"。
        return _swr(mem, request.query, top_k=int(request.top_k),
                    persona=getattr(request, "persona_id", "") or "",
                    strategy=(request.strategy or "cascade"),
                    agent_id=request.agent_id, persona_id=request.persona_id,
                    tenant_id=request.tenant_id)
    except Exception:  # noqa: BLE001 — 连 import 都失败时退回既有路径的等价调用（绝不让新路由更脆）
        swallow(__name__, None)
        return mem.search_hybrid(
            query=request.query, top_k=min(max(int(request.top_k) * 5, 50), 200),
            strategy=(request.strategy or "cascade"), agent_id=request.agent_id,
            persona_id=request.persona_id, tenant_id=request.tenant_id)


# ── 2026-10-06（t18）**记账不在本层** ────────────────────────────────────────
# 记账的唯一实现在**检索出口**：`core/client/_hybrid_stages._account_returned_hits`，
# 由 `search_hybrid` 的两条出口（`_hybrid_search.py` L1207 / L1399）各调一次。
# 为什么不放在本路由（先按任务书试过一版，被"非 API 调用方"实测推翻）：
#   本路由只是 `Trinity.search_hybrid` 的调用方**之一**。实测还有 `/memory/search`
#   （经 `core/client/_search.py:241,329`）、`/memory/recall`、graphql `search_memories`、
#   `_routers_brain`(3 处)、`_routers_explain`、`engine_worker`，以及 `trinity/brain/**`
#   十余个认知模块都在调它 ⇒ **只在本路由记账会让那些入口永久不再记账**
#   （比 t17 的 R-1 回退更大）。出口是**所有调用方都必经**的那一层。
# t17 曾在此处删过一次调用（修法 (ii)：通道层独占）；t18 把"谁独占"从通道层换成**出口层**，
# 通道层（`core/client/_hybrid_search.py` 4 处 `search_memories`）改为 `touch=False`。
@router.post("/memory/search/hybrid")
async def hybrid_search(request: HybridSearchRequest):
    """混合检索—向量 + BM25 关键词+ 图谱融合。
    三种策略:
      - fusion:  加权求和 (vector=0.5, bm25=0.3, graph=0.2)
      - rrf:     Reciprocal Rank Fusion (rank-based, robust)
      - cascade: 向量粗排 →BM25 精排 →图谱扩充

    返回:
      results 中每条含 hybrid_score / vector_score / bm25_score / graph_score 明细，
      引擎库可查到的记忆附 content_preview（聚合池专属 id 保持仅分数，见 A1 评测修复）。    """
    mem = get_memory()
    _t0 = time.perf_counter()  # P0-3：轨迹记录真实检索延迟
    # 2026-09-10（体检 659 P1-4）：真实检索流量埋点——/metrics 此前只反映
    # 聚合池的进程内计数（重启归零），与 audit_log 里的真实调用量脱节。
    try:
        record_retrieval(
            "api:/memory/search/hybrid",
            query=request.query,
            latency_ms=(time.perf_counter() - _t0) * 1000.0,
            extra={"strategy": getattr(request, "strategy", "")},
        )
    except Exception:  # noqa: BLE001 — 观测路径绝不影响检索
        swallow(__name__, None)
    # 2026-09-14（704）**查询侧扩展**（HyDE 式；EXECUTION 700：SS-P R@1 0.300→0.500，claim_gate PASS；
    # 守门臂 temporal-reasoning R@1 无退化 0.900→0.900）。默认 off；auto=仅请教式/偏好式查询；
    # 任何异常 **fail-open** 用原查询（检索可用性优先），并把 meta 挂到响应里供调用方核查。
    _qexp_meta = None
    _q = request.query
    try:
        from trinity.retrieval.query_expansion import expand as _qexp
        _q, _qexp_meta = _qexp(request.query)
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # 2026-09-14（714）**检索规划接线**（H1-1 第 6 处）：trinity/brain/retrieval_planning.plan_retrieval
    # 此前无任何调用者（unbound）。它给出 查询类型→通道/深度 的**决策**（factual→单通道精确；
    # explanatory→deep）。此处只落地其中**可安全执行**的一维：deep ⇒ 检索深度加倍（top_k×2，封顶 50）。
    # 默认 off（TRINITY_RETRIEVAL_PLANNING=deep|off），任何异常 fail-open 用原参数；
    # 计划原文挂到响应 retrieval_plan 供调用方核查（不改排序、不删行）。
    _plan = None
    _top_k = request.top_k
    try:
        from trinity.brain.retrieval_planning import plan_retrieval as _pr
        _plan = _pr(request.query)
        if (str(os.environ.get("TRINITY_RETRIEVAL_PLANNING", "off")).lower() == "deep"
                and str((_plan.get("plan") or {}).get("depth")) == "deep"):
            _top_k = min(50, int(request.top_k or 10) * 2)
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # 2026-09-29（用户授权 ③）：**引擎调用不再直接跑在事件循环里**。
    # 原实现是同步调用 ⇒ 一次慢检索堵死整个事件循环（/health 无响应 ⇒ 被健康守卫 kill）。
    # 现走有界池 + 硬上限：超时/池满一律 503 + 可读降级体。
    try:
        data = await run_search_bounded(lambda: mem.search_hybrid(
            query=_q,
            top_k=_top_k,
            strategy=request.strategy,
            agent_id=request.agent_id,
            persona_id=request.persona_id,
            tenant_id=request.tenant_id,
            # 2026-09-30（外部审计 · 缺口修复）：把 include_docs 贯穿到客户端层，
            # 使 /memory/search/hybrid 能检索 doc:* 知识语料（此前只有 GET /memories
            # 暴露该开关，而那条路由自身有一个 500）。默认 False ⇒ 行为不变。
            include_docs=bool(getattr(request, "include_docs", False)),
        ))
    except SearchDeadlineExceeded:
        # 2026-09-30 可用性修复：先试纯词法回退，失败才回落到 503。
        # 2026-10-06（D3 同根因）：`_lexical_fallback` 内部的
        # `_t.join(_LEXICAL_FALLBACK_TIMEOUT_S)`（默认 8s）是**同步 join** ——
        # 直接在事件循环里调用，等于每次降级都把整个循环堵住 8 秒
        # （`/health` 一起排队）。放到独立线程里执行 ⇒ 循环不再被占住。
        # 为保持既有契约（`tests/unit/test_degraded_read_accounting_20260930.py`
        # 仍以同步调用它），**不改函数签名**，只在调用点外包 `asyncio.to_thread`。
        rows, err = await asyncio.to_thread(
            _lexical_fallback, mem, _q, _top_k, request)
        if rows is not None:
            _SEARCH_STATS["degraded_lexical"] = _SEARCH_STATS.get("degraded_lexical", 0) + 1
            return _degraded_response(request, "search_deadline_exceeded", rows,
                                      note_extra=f"回退命中 {len(rows)} 条。")
        _SEARCH_STATS["fallback_failed"] = _SEARCH_STATS.get("fallback_failed", 0) + 1
        return _deadline_response(request, "search_deadline_exceeded")
    except SearchPoolSaturated:
        # 同上（2026-10-06 D3 同根因）：同步 join 不得跑在事件循环线程里。
        rows, err = await asyncio.to_thread(
            _lexical_fallback, mem, _q, _top_k, request)
        if rows is not None:
            _SEARCH_STATS["degraded_lexical"] = _SEARCH_STATS.get("degraded_lexical", 0) + 1
            return _degraded_response(request, "search_pool_saturated", rows,
                                      note_extra=f"回退命中 {len(rows)} 条。")
        _SEARCH_STATS["fallback_failed"] = _SEARCH_STATS.get("fallback_failed", 0) + 1
        return _deadline_response(request, "search_pool_saturated")
    try:
        if isinstance(data, dict) and _qexp_meta and _qexp_meta.get("expanded"):
            data["query_expansion"] = _qexp_meta
        if isinstance(data, dict) and _plan:
            data["retrieval_plan"] = _plan          # 714：计划原文回挂（可核查是否真的加深）
            data["retrieval_depth_applied"] = _top_k  # 实际使用的 top_k
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # A1 修复：为引擎库记忆回填 content_preview，避免调用方二次请求
    results = data.get("results", data if isinstance(data, list) else [])
    adapter = getattr(mem, "_adapter", None)
    for r in results:
        mid = r.get("memory_id")
        if mid and not r.get("content_preview") and adapter is not None:
            try:
                detail = adapter.get_memory(mid)
                if detail and detail.get("content"):
                    _c = detail["content"]
                    # 2026-09（EXECUTION 105.18）：回填路径解密缺失——get_memory
                    # 返回 enc:v1: 密文（SQLite 侧存储加密默认开；PG 侧无实装，
                    # 见 docs/SECURITY_BOUNDARIES.md），需显式解密
                    if isinstance(_c, str) and _c.startswith("enc:v1:")                             and hasattr(adapter, "_decrypt_content"):
                        try:
                            _c = adapter._decrypt_content(_c)
                        except Exception as _e:
                            swallow(__name__, _e)
                    r["content_preview"] = _c[:200]
            except Exception as _e:
                swallow(__name__, _e)
    # 2026-09-13（P0 门控修正）：full 融合路径的行在**引擎出口时还没有文本**
    # （content 为空，content_preview 正是本函数刚回填的）→ 引擎侧门控的词面重叠
    # 判据在那里看不到正文，会**系统性误判 no_term_overlap**（实测：长查询
    # "…主存储切换与迁移完成的结论是什么" overlap=0 → 假弃答）。
    # 故在预览回填**之后**用同一判据重算并覆盖引擎侧结论。
    try:
        from trinity.retrieval.evidence_gate import apply_evidence_gate
        _g = apply_evidence_gate(request.query, {"results": results}, source="api_after_preview")
        results = _g.get("results", results)
        if isinstance(data, dict):
            data["evidence_gate"] = _g.get("evidence_gate")
            data["abstain"] = _g.get("abstain")
            # 注意：**此处不是**仲裁的正确落点。证据门控在下面还会被
            # `attach_confidence()`（第 ~269 行）用引擎自己的置信**下调**一次
            # （取 min），所以 prob_relevant 在这里还不是终值。
            # 仲裁放在那之后（见该处注释）。
    except Exception as _e:
        swallow(__name__, _e)
    # ── Mano-P 借鉴（2026-09-09）：检索轨迹落盘（TRINITY_RETRIEVAL_TRACE=on）──
    # P0-3（2026-09-10 OpenViking 借鉴）：补齐 per-channel 归因——此前
    # channels={"primary": results} 把融合结果当唯一通道，无法回答"这条记忆
    # 被哪个通道捞出、排第几"。现按结果行上的逐通道分数键还原归因，
    # 并把 breakdown 与命中通道一并记入 meta。
    # 默认 off，不改变既有行为；失败静默（观测路径不得影响检索）。
    try:
        from trinity.retrieval.trace import channels_from_results, trace_if_enabled

        _chosen = [r.get("memory_id") for r in results
                   if isinstance(r, dict) and r.get("memory_id")]
        _channels = channels_from_results(results)
        if not _channels:
            # 轻通道（FTS）等无逐通道分数时保持旧口径，但显式命名以免误读为归因
            _channels = {"primary": results}
        trace_if_enabled(
            query=request.query,
            channels=_channels,
            chosen=_chosen[:5],
            fused=results,
            latency_ms=(time.perf_counter() - _t0) * 1000.0,
            agent_id=getattr(request, "agent_id", "") or "",
            top_k=int(getattr(request, "top_k", 0) or 0),
            meta={
                "strategy": getattr(request, "strategy", ""),
                "breakdown": (data.get("breakdown") if isinstance(data, dict) else None),
                "channels_hit": sorted(_channels.keys()),
            },
        )
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # ── 2026-09-09 大脑化：图级在线 Hebbian（HeLa-Mem 借鉴）──
    # 一次检索里共同命中的记忆两两强化 memory_links（实时共激活，不等日周期）。
    try:
        from trinity.brain.hebbian_links import record_coactivation

        _ids = [r.get("memory_id") for r in results
                if isinstance(r, dict) and r.get("memory_id")]
        record_coactivation(_ids, adapter=getattr(mem, "_adapter", None))
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # ── 记账（2026-10-06 t18）：**不在本层** —— 唯一实现在检索出口
    # `core/client/_hybrid_stages._account_returned_hits`（`search_hybrid` 两条出口各调一次），
    # 那里才是所有调用方（本路由 / `/memory/search` / recall / graphql / brain / worker）
    # 都必经、且知道**最终返回集**的那一层。理由与本层曾试过又被推翻的一版见上方注释。
    # ── 2026-09-09 大脑化：GWT 注意力广播（Theater of Mind 借鉴）──
    # 候选竞争 → 胜者写入 global_workspace（焦点可被后续检索/上下文读取）。
    try:
        from trinity.brain.workspace_broadcast import broadcast_from_results

        broadcast_from_results(results)
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # 2026-09（EXECUTION 105.8）认知循环集成：检索响应默认附加轻量元认知
    # （confidence + gap 提示）——零额外延迟（基于已有结果计算，不调 LLM/嵌入）。
    try:
        from trinity.brain.metacognition import assess_confidence
        # 2026-09-29（③ 的连带修正）：**把类型说真话**。`assess_confidence` 期望
        # `list[str]`（通道名列表），而这里原先直接赋 `breakdown["channels"]`（无注解 ⇒
        # 被推成 `dict[str, list[list[Any]]]`）。本轮把引擎调用移进有界执行器后，
        # mypy 的推断随之变化，**当场把这条既有类型谎言顶成一处新的 arg-type**
        # （ratchet 768→769）。修法是按实现语义收窄，而不是抬基线或加 ignore。
        _chan_names: List[str] = []
        if isinstance(data, dict):
            _bc = (data.get("breakdown") or {}).get("channels", [])
            if isinstance(_bc, list):
                _chan_names = [c for c in _bc if isinstance(c, str)]
        _meta = assess_confidence(results, _chan_names)
        _scores = [r.get("score") for r in results
                   if isinstance(r.get("score"), (int, float))]
        _all_fallback = len(results) > 0 and all(
            (s or 0) <= 0.15 for s in _scores)
        _gap_hint = (len(results) == 0
                     or (_all_fallback and _meta["confidence"] < 0.4))
        _meta["gap_hint"] = _gap_hint
        if isinstance(data, dict):
            data["metacognition"] = _meta
        # 2026-09-19（Jev 借鉴·概率源接线）：元认知置信回灌证据门控。
        # 门控在此前已跑过（那时只有词面重叠），此处用引擎自己的置信把概率**下调**，
        # 只改 policy/abstain 标注（passed 不变）⇒ 默认行为与改动前一致。
        try:
            from trinity.retrieval.evidence_gate import attach_confidence
            if isinstance(data, dict):
                attach_confidence(data, _meta.get("confidence"), query=request.query)
        except Exception as _e2:
            swallow(__name__, _e2)
        # 2026-09-29（用户授权 ②）：**把"质量数"统一到终值**。
        #
        # 上面 ③ 已经把 `prob_relevant` 下调成终值，而 `memory_policy.monitoring.recall_quality`
        # 是引擎出口（①，`memory_policy_hook.attach`）用**改写前**的值写的 ⇒ 实测同一响应里
        # `recall_quality=0.96` 与 `prob_relevant=0.4` 并存（仲裁者只能把它标成冲突，
        # 数字本身仍是两个）。这里先把它重算成终值，**再**让仲裁者基于统一后的值裁决 ——
        # 顺序不能反。纯旁路字段、幂等、失败静默。
        try:
            from trinity.retrieval.memory_policy_hook import reattach_policy_after_gate
            reattach_policy_after_gate(data, request.query)
        except Exception as _e2b:
            swallow(__name__, _e2b)
        # 2026-09-29（外部审计修复，根因 C 的**自洽性**）：**仲裁必须在这里重算**。
        #
        # 实测到的顺序（本文件内三处依次改写证据门控）：
        #   ① 引擎出口 core/client/_hybrid_search.py:1378 → memory_policy_hook.attach
        #      在此处第一次跑仲裁（读到的是**引擎侧**门控值，0.96）；
        #   ② 本文件 ~165 `apply_evidence_gate(..., source="api_after_preview")` 重跑门控；
        #   ③ 本处 ~269 `attach_confidence()` 用引擎元认知置信把概率**下调**（取 min）
        #      ⇒ 终值 0.96 → 0.4。
        # 而 ① 的仲裁结果会原样留在响应里 ⇒ **最终响应自相矛盾**：
        #   metacognition_verdict.confidence      = 0.96
        #   metacognition_verdict.confidence_source = "evidence_gate.prob_relevant"
        #   evidence_gate.prob_relevant           = 0.4   ← 与上面不符
        # 即"三套自评互相矛盾"被换成了"仲裁与门控互相矛盾"。修法：在**终值落定之后**
        # 用最终门控值把仲裁重算一次。仲裁是纯函数 + 旁路字段（不插行/不改排序/不删行），
        # 重算幂等；失败静默（观测/标注路径不得影响检索）。
        try:
            if isinstance(data, dict) and os.environ.get(
                    "TRINITY_METACOG_ARBITER", "on").lower() not in ("off", "0", "false"):
                from trinity.brain.metacognition_arbiter import arbitrate
                data["metacognition_verdict"] = arbitrate(data, request.query)
        except Exception as _e3:
            swallow(__name__, _e3)
    except Exception as _e:
        swallow(__name__, _e)
    # ── 2026-09-11（V2 评估修复）：信号发射收敛为**共用实现** ──
    # 发现：信号源全仓只有本处，MCP 主路径不发信号 → "脑干接错血管"。
    # 现由 trinity.brain.retrieval_signals 统一发射，MCP 侧同一份实现（避免口径分叉）。
    try:
        from trinity.brain.retrieval_signals import emit_retrieval_signals

        _conf = 0.0
        if isinstance(data, dict):
            _conf = float((data.get("metacognition") or {}).get("confidence") or 0.0)
        emit_retrieval_signals(getattr(request, "query", ""), results, _conf,
                               source="api_search")
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    # 2026-09（EXECUTION 105.11）：按需重建式回忆（recall=True 时附加；
    # 默认 False 保持取档式性能——深度加工按需）
    if getattr(request, "recall", False) and isinstance(data, dict) and results:
        try:
            from trinity.brain.value_encoder import recall_reconstruct
            _sources = [{"memory_id": r.get("memory_id"),
                         "content": str(r.get("content_preview") or r.get("content") or "")[:300],
                         "created_at": str(r.get("created_at"))[:10] if r.get("created_at") else ""}
                        for r in results[:8]]
            _raw = recall_reconstruct(request.query, _sources, top_k=8)
            if _raw:
                data["recall"] = {"text": _raw.strip(), "confidence": 0.7}
        except Exception as _e:
            swallow(__name__, _e)
    # ── 2026-10-02（事故后加固）：**给"零结果"一个可分辨的来源** ────────────────
    # 事故实测：权威库损坏时本端点返回 **HTTP 200 + `results: []`**，而调用方
    # 无法区分两种情况：
    #   (a) 检索面健康，确实没有相关记忆（**正确答案**，不应报错）
    #   (b) 检索面坏了（无 adapter / 库打不开），却装作"没有"（**fail-open**）
    # 这是最危险的失败形态：上游会把故障读成"无证据"，进而继续编答案。
    # 修法：**不改状态码**（200 仍表示"请求被处理了"），但把可分辨的
    # `retrieval_health` 挂进响应体，并**显式**给出 degraded_reason。
    # 判定抽成纯函数 `retrieval_health()`（本模块顶部）以便带反事实地测。
    try:
        if isinstance(data, dict):
            data["retrieval_health"] = retrieval_health(data, results, _SEARCH_STATS)
    except Exception as _e:
        swallow(__name__, _e)
    return data


@router.post("/memory/search/cross-modal", tags=["Cross-Modal"])
async def cross_modal_search(request: CrossModalSearchRequest):
    """跨模态检索—自动检测输入类型并路由。
    支持:
      - auto:   自动检测query 是text / image / combined
      - text:   文字搜图片记忆(image_description)
      - image:  图片搜文字记忆(text)
      - combined: 联合检索（需 [text, image_path] 格式）    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    # A4 修复：无可用编码器（离线/模型缺失）时返回明确的降级响应，而非 500/挂起
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "query_type": request.query_type, "degraded": True,
                "detail": "CLIP/文本编码器不可用（离线或模型未缓存）；配置本地模型后可启用"}
    return cm.search_cross_modal(
        query=request.query,
        query_type=request.query_type,
        top_k=request.top_k,
    )


@router.post("/memory/search/image-by-text", tags=["Cross-Modal"])
async def image_by_text(request: ImageByTextRequest):
    """文搜图—用自然语言描述检索相关图片记忆。
    在image_description 模态记忆中做语义检索，返回最相关的图片描述    及其关联的图片文件路径。    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "degraded": True, "detail": "文本编码器不可用（离线/模型未缓存）"}
    return mem.search_image_by_text(text=request.text, top_k=request.top_k)


@router.post("/memory/search/text-by-image", tags=["Cross-Modal"])
async def text_by_image(request: TextByImageRequest):
    """图搜文—用图片检索相关文字记忆。
    对传入的图片进行编码后，在text 模态记忆中做语义检索，
    返回与图片语义最相近的文字记忆。    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "degraded": True, "detail": "CLIP/文本编码器不可用（离线/模型未缓存）"}
    return mem.search_text_by_image(image_path=request.image_path, top_k=request.top_k)


@router.post("/reason")
async def reason(
    query: str = Body(...),
    multi_hop: bool = Body(False),
    top_k: int = Body(5),
    qtype: Optional[str] = Body(None, description="题型提示（multi-session/temporal-reasoning/single-session-preference…），用于策略路由"),
    question_date: Optional[str] = Body(None, description="问题日期 YYYY/MM/DD（temporal REL 计算用）"),
    route: bool = Body(False, description="走 RouteReasoner（已验证生成策略；需 DEEPSEEK_API_KEY）"),
):
    """Open-domain reasoning.

    2026-08-17 产品化: route=True（或环境 TRINITY_ROUTE_REASONER=on）时走
    RouteReasoner——multi→turn 粒度 / temporal→REL+inner2 / pref→两段式 /
    其他→dated plain；否则回退 OpenDomainReasoner。
    """
    mem = get_memory()
    if not hasattr(mem, 'reason'):
        raise HTTPException(status_code=501, detail="reason() not available")
    prev_route = os.environ.get("TRINITY_ROUTE_REASONER", "off")
    if route:
        os.environ["TRINITY_ROUTE_REASONER"] = "on"
    try:
        return mem.reason(
            query=query, multi_hop=multi_hop, top_k=top_k,
            qtype=qtype, question_date=question_date,
        )
    finally:
        if route:
            os.environ["TRINITY_ROUTE_REASONER"] = prev_route


@router.post("/embeddings")
async def embed_text(text: str = Body(...), backend: str = Body("auto")):
    """Generate semantic embedding."""
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        engine = create_engine(backend=backend)
        vec = engine.embed(text)
        return {
            "text": text[:100], "dim": engine.embedding_dim(),
            "model": engine.model_name(), "embedding": vec.tolist(),
            "norm": float(np.linalg.norm(vec)),
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Embedding module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")


@router.post("/embeddings/batch")
async def embed_texts(texts: List[str] = Body(...), backend: str = Body("auto")):
    """Batch embed texts."""
    try:
        from trinity.embeddings import create_engine
        if not texts:
            return {"count": 0, "dim": 0, "model": "none", "embeddings": []}
        engine = create_engine(backend=backend)
        vecs = engine.embed_batch(texts)
        return {
            "count": len(vecs), "dim": engine.embedding_dim(),
            "model": engine.model_name(), "embeddings": [v.tolist() for v in vecs],
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Embedding module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch embedding failed: {e}")


def _vector_search_sync(
    query: str, top_k: int, index_backend: str, embed_backend: str,
) -> Dict[str, Any]:
    """`/vector/search` 的**同步**函数体（原 handler 内联代码，行为逐字保留）。

    2026-10-06（D3）：本段**只允许在独立线程里执行**（见 `vector_search`）。
    `create_engine` 冷启动（加载 ~1.9GB session）、`eng.embed`，以及回退路径
    `get_all_memories(limit=200)` + `embed_batch(200 段)` 都是同步长阻塞调用；
    原先它们在事件循环线程里跑 ⇒ 整个循环被占死（`/health` 一起失去响应 ⇒
    supervisor 判 UNHEALTHY 后 kill）。
    """
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        eng = create_engine(backend=embed_backend)
        qv = np.asarray(eng.embed(query), dtype=np.float32)
        mem = get_memory()
        # 2026-09（EXECUTION 104.9）：PG 主存储直接 pgvector HNSW 直查——
        # 原实现每次全量拉 200 条 + 内存重建索引 + 逐条嵌入（实测 >90s）；
        # 直查 ~15ms。失败自动回退下方内存路径。
        adapter = getattr(mem, "_adapter", None)
        if adapter is not None and hasattr(adapter, "vector_search"):
            try:
                res = adapter.vector_search(
                    qv, top_k=top_k,
                    agent_id=getattr(mem, "_search_agent_id", None),
                    persona_id=getattr(mem, "_search_persona_id", None),
                    tenant_id=getattr(mem, "_search_tenant_id", None),
                )
                if res:
                    return {
                        "query": query, "total": len(res),
                        "model": eng.model_name(), "dim": eng.embedding_dim(),
                        "index_backend": "pgvector-hnsw",
                        "results": [{"id": r.get("memory_id"),
                                     "score": round(float(r.get("score", 0.0)), 4),
                                     "metadata": r} for r in res],
                    }
            except Exception as _e:
                swallow(__name__, _e)  # fall through to in-memory path
        # fallback: in-memory index path (non-PG adapters or direct-query failure)
        from trinity.vector_index import create_index
        idx = create_index(backend=index_backend, dim=eng.embedding_dim())
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=200)
            except Exception as _e:
                swallow(__name__, _e)
        if not memories:
            return {"query": query, "total": 0, "results": [], "note": "No memories in pool"}
        texts = [m.get("content", "") for m in memories if m.get("content")]
        if not texts:
            return {"query": query, "total": 0, "results": [], "note": "No content"}
        vecs = eng.embed_batch(texts)
        for m, v in zip(memories, vecs):
            mid = m.get("memory_id", m.get("id", f"mem_{hash(str(m))}"))
            idx.add(mid, v, m)
        results = idx.search(eng.embed(query), top_k=top_k)
        return {
            "query": query, "total": len(results),
            "model": eng.model_name(), "dim": eng.embedding_dim(),
            "index_backend": type(idx).__name__,
            "results": [{"id": r.id, "score": round(float(r.score), 4), "metadata": r.metadata} for r in results],
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Required module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vector search failed: {e}")


def _vector_deadline_response(query: str, top_k: int,
                              index_backend: str) -> JSONResponse:
    """`/vector/search` 的 503 降级体：**可读、可归因**（与 `_deadline_response` 同风格）。

    这里**没有**"纯词法回退"可退（这是向量专用端点）⇒ 诚实返回 503，
    而不是伪装成"看起来正常的空结果 200"。
    """
    return JSONResponse(status_code=503, content={
        "status": "timeout",
        "degraded": True,
        "degraded_reason": "vector_search_deadline_exceeded",
        "lexical_only": False,
        "deadline_s": _SEARCH_DEADLINE_S,
        "query": query,
        "top_k": top_k,
        "index_backend": index_backend,
        "results": [],
        "count": 0,
        "breakdown": {"deadline_exceeded": True, "pool_saturated": False,
                      "vector_search": True},
        "note": ("向量检索超过硬上限（TRINITY_SEARCH_DEADLINE_S）⇒ 返回 503 而不是挂死；"
                 "该次引擎调用仍在**独立线程**里跑完（无法安全杀线程），"
                 "但**不阻塞事件循环**（/health 与其它检索不受影响）。"
                 "调大上限请改该环境变量。"),
    })


@router.post("/vector/search")
async def vector_search(
    query: str = Body(...), top_k: int = Body(10),
    index_backend: str = Body("numpy"), embed_backend: str = Body("auto"),
):
    """Semantic vector search (PG pgvector HNSW direct when available).

    2026-10-06（D3，实测根因）：本 handler 此前**整个函数体在事件循环里同步执行**
    —— 它是本模块唯一没走有界池的检索入口（`run_search_bounded` 的唯一调用点是
    hybrid 路由）。现场证据：`api.out.log` 里该路由**从未有一条完成记录**，且重启
    前的窗口出现 `GET /v1/diagnostics →404 (20932.5ms)`（不干活的 404 也排队 20.9s）。
    处置：同步体搬到 `_vector_search_sync`，在 `asyncio.to_thread` 的**独立线程** +
    `_SEARCH_DEADLINE_S` 硬上限里执行；超时返回可读降级体（503）。

    **刻意不接 `run_search_bounded`/`_SEARCH_SLOTS`**：共享池默认只有
    `_SEARCH_POOL_SIZE=2` 个槽，一次慢向量检索会挤掉并发的 hybrid 检索
    （属未声明的行为变化）。独立线程即可解除事件循环阻塞
    （同目录 `_pool_refresh.py:244` 已有 `asyncio.to_thread` 先例）。
    """
    try:
        _VECTOR_STATS["requests"] += 1
        _out = await asyncio.wait_for(
            asyncio.to_thread(
                _vector_search_sync, query, top_k, index_backend, embed_backend),
            timeout=_SEARCH_DEADLINE_S,
        )
        _VECTOR_STATS["ok"] += 1
        return _out
    except asyncio.TimeoutError:
        # 超时 ⇒ 诚实降级（孤儿线程跑完自行退出；无法安全杀线程，但不占循环）。
        # 刻意**不动** `_SEARCH_STATS`：那是"有界检索池"的指标，向量端点不占池，
        # 混入会污染 timeout 计数口径。
        # 2026-10-06（复评 G5）：但"不进池"不等于"不用计量" —— 此前本端点的超时
        # **在任何 metric 里都看不见**（只能在响应体 `degraded_reason` 里读到，而
        # 调用方往往是脚本、不解析 body）⇒ 运维无从知道它在频繁超时。
        # 现用**独立的** `_VECTOR_STATS` 计数，既不污染池口径，又可被 /metrics 读取。
        _VECTOR_STATS["timeout"] += 1
        return _vector_deadline_response(query, top_k, index_backend)
    except Exception:
        _VECTOR_STATS["error"] += 1
        raise


@router.post("/vector/index")
async def index_memories(backend: str = Body("auto"), force_reindex: bool = Body(False)):
    """Index all memories to vector store."""
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        eng = create_engine(backend=backend)
        mem = get_memory()
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=1000)
            except Exception as _e:
                swallow(__name__, _e)
        try:
            from trinity.vector_index import ChromaDBIndex
            idx = ChromaDBIndex(dim=eng.embedding_dim(), collection_name="trinity_api_search")
        except ImportError:
            from trinity.vector_index import create_index
            idx = create_index(backend="numpy", dim=eng.embedding_dim())
        indexed, errors = 0, 0
        for m in memories:
            try:
                text = m.get("content", "")
                if not text:
                    continue
                idx.add(m.get("memory_id", m.get("id", f"mem_{indexed}")), eng.embed(text), m)
                indexed += 1
            except Exception:
                errors += 1
        return {"total_memories": len(memories), "indexed": indexed, "errors": errors,
                "model": eng.model_name(), "dim": eng.embedding_dim(), "index_backend": type(idx).__name__}
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Required module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory indexing failed: {e}")


@router.post("/memory/skills/search", tags=["Retrieval"], summary="技能/经验定向检索")
async def skills_search(query: str = Body(...), top_k: int = Body(5, ge=1, le=10)):
    """技能库定向检索（2026-09-09 闭环 658.26 A1b）。

    通用 hybrid 对技能内容召回差（内容词与任务词错配，实测样例 0 命中且 16s）；
    技能/程序记忆量小（~220 条），这里全量取回后按 jieba 词重叠打分——
    实测 1ms、样例命中 59 条。调用方：retrieval_bridge.engine_skill_search
    （技能注入通道）。仅类别内检索，不污染通用检索面。
    """
    mem = get_memory()
    adapter = getattr(mem, "_adapter", None)
    if adapter is None:
        return {"results": [], "note": "no adapter"}
    import jieba as _jb
    _jb.setLogLevel(60)
    words = [w.strip() for w in _jb.cut((query or "")) if len(w.strip()) >= 2][:12]
    if not words:
        return {"results": []}
    try:
        with adapter._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT memory_id, category, content, tags FROM memories "
                "WHERE status='active' AND category IN ('procedural','skill') "
                "ORDER BY created_at DESC LIMIT 400")
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"skills search failed: {exc}")
    results = []
    for mid, cat, content, tags in rows:
        txt = content or ""
        if txt.startswith("enc:v1:"):
            try:
                txt = adapter._decrypt_content(txt)
            except Exception:  # noqa: BLE001
                continue
        hits = sum(1 for w in words if w in txt)
        if not hits:
            continue
        results.append({
            "memory_id": mid, "category": cat,
            "content": txt[:500],
            "tags": tags if isinstance(tags, list) else [],
            "score": round(hits / max(len(words), 1), 4),
        })
    results.sort(key=lambda x: -x["score"])
    return {"query": query, "total": len(results), "results": results[:top_k]}


