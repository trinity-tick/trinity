#!/usr/bin/env python3
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13, 顶部插入）
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
"""
Trinity REST API Server — shared runtime state, helpers and HTTP middleware.

Extracted from the former trinity/api/server.py monolith (v8.0.0+).

This module owns ALL module-level mutable state and the functions that use
it (so globals resolve in one place), plus the four @app.middleware("http")
handlers (defined here WITHOUT decorators; server/__init__.py registers them
on the app in the original order so the middleware stack is identical).

It must NOT import from trinity.api.server (no circular imports); the
_live_memory / _live_aggregator helpers resolve get_memory/get_aggregator
through the server package at CALL time so test monkeypatching of
trinity.api.server.get_memory / get_aggregator keeps working exactly
as it did on the monolith module globals.
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from trinity import Trinity
from trinity.agents import MemoryAggregator, create_aggregator
from trinity.api.middleware import (
    get_metrics,
    is_rate_limited_request,
    metrics_dispatch,
    rate_limit_burst,
    rate_limit_enabled,
    rate_limit_rate,
)

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = object


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiter (simple token bucket)
# ═══════════════════════════════════════════════════════════════════════════
class TokenBucket:
    def __init__(self, rate: int = 60, burst: int = 120):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

def _build_rate_limiter() -> TokenBucket:
    """Build a bucket from TRINITY_RATE_LIMIT_RATE / TRINITY_RATE_LIMIT_BURST."""
    return TokenBucket(rate=rate_limit_rate(), burst=rate_limit_burst())


_rate_limiter = _build_rate_limiter()


def reconfigure_rate_limiter() -> TokenBucket:
    """Re-read TRINITY_RATE_LIMIT_* env vars and rebuild the shared bucket.

    Resets the token count to a full burst. Used by tests for isolation and
    available for live reconfiguration without a restart.
    """
    global _rate_limiter
    _rate_limiter = _build_rate_limiter()
    return _rate_limiter


# ═══════════════════════════════════════════════════════════════════════════
# App Lifecycle
# ═══════════════════════════════════════════════════════════════════════════
_aggregator: Optional[MemoryAggregator] = None
_memory: Optional[Trinity] = None
_app_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _app_start_time
    _app_start_time = time.time()
    # ── 2026-10-02（内存归因取证）：启动期内存时序落盘 ─────────────────────────────
    # 为什么需要：常驻 API 的 PrivateMemorySize64（守卫判据）实测在 **8.44 ↔ 24.45GB** 跳，
    #   而离线把整条启动路径逐段跑完只有 1.4–3.2GB（scripts/profile_api_startup_memory.py、
    #   scripts/profile_lifespan_commit.py）。差异无法归因，因为本环境**没有权限 attach 活体**
    #   （py-spy: `Failed to open process ... os error 5`；OpenProcess/taskkill 同样 Access denied）。
    #   ⇒ 反过来：让进程**自己**把时序写到磁盘，事后读 JSONL 定位"台阶"出现的秒数。
    # 开销：每秒一次 GetProcessMemoryInfo（微秒级）+ 线程 CPU 快照（仅台阶时）。
    # 只在启动后前 150s 采样（daemon 线程，绝不阻塞启动；异常全吞）。
    # 关闭：TRINITY_MEM_TRACE=0。产物：~/.trinity/state/startup_mem_trace.jsonl（仓库外）。
    # 回滚：删掉下面这个 try 块。
    try:
        from scripts.startup_mem_trace import start as _mem_trace_start
        _mem_trace_start(seconds=150.0, tag="api-lifespan")
    except Exception:  # noqa: BLE001 — 取证路径绝不影响启动
        try:
            import os as _os_tr, sys as _sys_tr
            _root_tr = _os_tr.path.dirname(_os_tr.path.dirname(_os_tr.path.dirname(
                _os_tr.path.dirname(_os_tr.path.abspath(__file__)))))
            if _root_tr not in _sys_tr.path:
                _sys_tr.path.insert(0, _root_tr)
            from scripts.startup_mem_trace import start as _mem_trace_start2
            _mem_trace_start2(seconds=150.0, tag="api-lifespan")
        except Exception as _e_trace:  # noqa: BLE001 — 取证失败不得影响启动
            # 2026-10-02：原写法是裸 `pass`，被 structure_gate 的静默失败棘轮计为一条
            # （AST 判据：except 体里只有一条 pass）⇒ 改为"显式空操作 + 留痕"，语义不变。
            logging.getLogger(__name__).debug("startup_mem_trace 未接线：%s", type(_e_trace).__name__)
    # ── 2026-10-03（**补接线**）：内存跳变时抓**全线程 Python 栈** ────────────────────
    # 为什么必须补：实测 `startup_mem_trace` 已抓到一次真实台阶
    #   `pid=23904  +16399.6 MB → private=24741.8 MB`（t=41.9s，ws 仅 6940MB），
    #   但当时 **jump_stack_trace 只挂在我手写的启动脚本里**（09:01 就停了）
    #   ⇒ 生产进程的内存曲线**在记**，而**跳变当刻的栈没记**，所以那次 16GB 的归属至今未知。
    # 本块把它挂到**生产 lifespan**，窗口放宽到 3600s（原先只 150s ⇒ 覆盖不到中后期事件）。
    # 开销：每秒一次 GetProcessMemoryInfo（微秒级）；**只在跳变（默认 ≥1GB）时**抓栈。
    # 关闭：TRINITY_JUMP_TRACE=0；阈值：TRINITY_JUMP_TRACE_MB（默认 1024）。
    # 产物：~/.trinity/state/jump_stack_trace.jsonl（仓库外；含文件名:行号(函数)，无业务数据）。
    # 回滚：删掉下面这个 try 块。
    try:
        import os as _os_jt, sys as _sys_jt
        _root_jt = _os_jt.path.dirname(_os_jt.path.dirname(_os_jt.path.dirname(
            _os_jt.path.dirname(_os_jt.path.abspath(__file__)))))
        if _root_jt not in _sys_jt.path:
            _sys_jt.path.insert(0, _root_jt)
        from scripts.jump_stack_trace import start as _jump_trace_start
        _jump_trace_start(
            seconds=float(_os_jt.environ.get("TRINITY_JUMP_TRACE_SECONDS", "3600") or 3600),
            jump_mb=float(_os_jt.environ.get("TRINITY_JUMP_TRACE_MB", "1024") or 1024),
            tag="api-lifespan")
    except Exception as _e_jt:  # noqa: BLE001 — 取证失败绝不影响启动
        logging.getLogger(__name__).debug("jump_stack_trace 未接线：%s", type(_e_jt).__name__)
    _live_aggregator()  # pre-warm
    # 2026-09-27（本轮 ①）：readyz 子探针预热——把懒导入/懒构造的冷代价从
    # 「首次 /readyz」移到启动期（实测重启后首探 engine_cache 撞 0.5s 预算超时，
    # 其后同为 2–8ms）。失败留痕不抛，行为与预热前一致；回滚=删本块。
    try:
        from . import _observability as _obs
        _obs.warm_readyz_probes()
    except Exception as _exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("readyz warmup skipped: %s", _exc)
    _startup_prewarm()  # 2026-08-15：启动期后台预热（BM25 构建 + 首次检索）
    # 2026-09-10（658.38）：池增量刷新常驻任务——聚合池由本进程持有，故也应由
    # 本进程维护（离线 sync 会被内存池覆盖，曾造成 6,682 vs 36,523 的永久缺口）。
    _pool_task = None
    try:
        import asyncio as _aio
        from . import _pool_refresh as _pr
        _pool_task = _aio.create_task(_pr.run_periodic())
    except Exception:  # noqa: BLE001
        _pool_task = None
    yield
    if _pool_task is not None:
        try:
            _pool_task.cancel()
        except Exception:  # noqa: BLE001
            swallow(__name__, None)
    # Shutdown: flush persistence
    global _aggregator
    if _aggregator is not None:
        _aggregator._save()


def _startup_prewarm() -> None:
    """启动期后台预热（2026-08-15 二轮压测修复）：把 ~2.7s 冷启动
    （BM25 后台构建 + embedding fit + jieba）从"首个请求"移到启动期。

    - get_memory() 触发 adapter 连接（jieba 后台预热）
    - 只触发 BM25 后台构建（_ensure_bm25_index），**不跑完整 search_hybrid**
      ——预热线程跑全链路检索会与首请求竞争写锁/GIL（实测首请求 16s）
    - 轮询等 _bm25_ready（上限 30s），不阻塞启动
    失败静默：即使预热失败，首个请求仍走惰性路径（行为与预热前一致）。
    """
    import threading as _th

    def _warm() -> None:
        try:
            mem = _live_memory()
            mem._ensure_bm25_index()  # 仅触发后台构建，返回即释放
            deadline = time.time() + 30
            while time.time() < deadline and not getattr(
                    mem, "_bm25_ready", False):
                time.sleep(0.2)
        except Exception:
            swallow(__name__, None)
        # 2026-09（EXECUTION 104.9）：嵌入引擎预热——向量通道冷启动 ~24s
        # （transformers import + tokenizer + ONNX session）移到启动期后台，
        # 首个向量查询不再卡 24s。TRINITY_PREWARM_EMBED=0 可关闭；失败静默
        # （惰性路径兜底，行为与预热前一致）。
        if os.environ.get("TRINITY_PREWARM_EMBED", "1") == "1":
            try:
                from trinity.core.client._helpers import _get_embedding_engine
                eng = _get_embedding_engine()
                if eng is not None:
                    eng.embed("warmup")
            except Exception:  # 629: 预热尽力而为,失败静默降级(有意)
                swallow(__name__, None)
        # 2026-09 (EXECUTION 123): jieba 词典预热——首个中文检索不再卡
        # 1.8s（词典构建是进程级一次性，从首请求移到启动期）。
        try:
            import jieba as _jb
            _jb.setLogLevel(60)
            _jb.cut("预热中文分词词典")
        except Exception:
            swallow(__name__, None)
        # 2026-09 (EXECUTION 124): reranker 预热——首查不再卡 2-10s
        # 2026-09-02（CE 修复后恢复默认开）：main() 已顺序 preload，此处后台加载 CE
        # 模型（缓存完整，~0.3s）；TRINITY_PREWARM_RERANK=0 可关。
        if os.environ.get("TRINITY_PREWARM_RERANK", "1") == "1":
            try:
                from trinity.vector_index.reranker import CrossEncoderReranker
                _rk = CrossEncoderReranker(model_name="chinese")
                _rk._load_model()  # 失败静默（降级链兜底）
            except Exception:
                swallow(__name__, None)
        # 2026-09-29（用户授权 ①，py-spy 现场定位）：**全语料向量索引预热**。
        #
        # 现场栈（受控重启后首个 full 查询 **20,223 ms**，被请求级硬上限 503）：
        #     onnxruntime … InferenceSession.run
        #     _embed_batch_raw (trinity/embeddings/engine.py:389)
        #     _vector_search (trinity/core/client/_search.py:1169)
        #     _ppr_fn (trinity/core/client/_hybrid_index.py:207)   ← PPR 语义种子（**默认路径**）
        # 即：首个 full 查询要在**请求路径里**把 ~2 万条正文嵌一遍（争用时实测 163,182 ms），
        # 且那个孤儿任务会占住检索执行器槽位数分钟（之后所有查询快速 503）。
        # 处置：把这一次全量嵌到启动期后台 —— 首个用户查询不再付这笔钱。
        # 关闭：TRINITY_PREWARM_VEC_CORPUS=0；失败静默（惰性路径兜底，行为与预热前一致）。
        if os.environ.get("TRINITY_PREWARM_VEC_CORPUS", "1") == "1":
            try:
                # 2026-09-29（①‑A 第三次修正）：预热必须**给请求让路**。
                #
                # 实测教训：预热用 2000 行/轮（≈450 s/轮）霸占**串行 ONNX** ⇒ 请求路径
                # 的查询嵌入排队 ⇒ 超 20 s 硬上限 ⇒ 503 + 孤儿占槽 ⇒ 池满 ⇒
                # **之后所有查询 17–23 ms 快速 503**（日志实证）＝"预热期间搜索整体不可用"。
                #
                # 故：① 每轮预算默认降到 **200 行**（≈45 s 上限，配合下面的让路）；
                #     ② 每轮开始前**等请求清空**（有 in-flight 检索就先睡，最多等 60 s），
                #        让批量活永远不挡在请求前面。
                # 代价（如实）：全量 19,355 行会跨很多轮、耗时被拉长（分钟级→小时级）；
                # **真正的正解是把语料向量持久化**（像聚合器索引那样落盘、启动直接加载），
                # 已登记为下一步。
                from trinity.api.server._routers_search import _inflight_searches
                # 2026-09-30（外部审计 · 一次**被证伪**的假设，留痕）：
                # 我曾把这里的每轮预算 200 → 32，假设"一轮 200 行的嵌入独占会挡住请求"。
                # **实测否证**（probe_prewarm_contention.py，安静 vs 并发对照）：
                #     安静基线（完全不跑预热）冷 full 查询 = **14.62 s**
                #     开预热后同一查询                     = **0.59 s**（因为已被预热弄热）
                # ⇒ 预热**不是**残余延迟的原因；把预算调小只会让语料索引建成更慢
                #   （19,355 行：97 轮 → 605 轮）而收益为零。故**已还原为 200**。
                # 真正的残余已定位到别处：`write_audit_log` 的链尾查询做全表扫描
                # （`SELECT checksum ... ORDER BY timestamp DESC, id DESC LIMIT 1`，
                #  305,855 行、单次 362 ms / 服务内 profile 3.117 s），
                # 已在 `adapters/sqlite/_schema.py` 加 `idx_audit_chain_tail` 修复（→ 0 ms）。
                try:
                    # 2026-09-30（外部审计修复 · **本行是那次池卡死的主因**）：
                    # 原实现用 `setattr(mem, "_vec_corpus_budget_override", 200)` ——
                    # 那是**共享实例属性**，而请求路径读的是**同一个**属性
                    # ⇒ 整个预热窗口（≈97 轮 × 45 s ≈ 73 分钟）内**请求也在请求内嵌语料**
                    # ⇒ 与预热抢 `_SESSION_RUN_LOCK` ⇒ 有界请求 > 90 s ⇒ 撞 20 s 上限降级；
                    # 池大小只有 2，两个一卡就占满 ⇒ 后续 503（线程栈取证：两个
                    # `search-*` 线程都停在 `engine.py:70 with self._lock`）。
                    # 改为**线程局部**作用域（本预热跑在专用线程 `api-startup-prewarm`），
                    # 请求线程看不到这个放大 ⇒ 回到政策规定的默认 `0`（不嵌）。
                    from trinity.core.client._vec_budget import corpus_budget_scope
                    _wb = int(os.environ.get("TRINITY_VEC_CORPUS_WARM_BUDGET", "200") or 200)
                    # 2026-10-04（**让预热可断点续跑 + 避开启动争锁**）：实测两件事 ——
                    #  ① 嵌入器在**空闲态满速**（0.151 s/行），而启动重活期争锁约 6.6× 慢；
                    #  ② API 在启动重活期会被 supervisor 重启若干次，而本预热**从零重来**
                    #     ⇒ **永远跑不完、落盘永不发生**（三次受控启动都观察到）。
                    # 对策 a：**等"进程真正空闲"再开始**（避开与聚合器索引/BM25 的启动期嵌入争抢
                    # 串行 ONNX 会话锁）。
                    # ⚠️ 2026-10-05 修正：上一版的判据 `_bm25_ready` **在预热启动时已经是 True**
                    #（实测 `PREWARM-START settle_wait=0s bm25=True`）⇒ 等待立即通过、
                    # **完全没有起到让路作用**。这正是"启动期内首轮 >20 分钟未完成、
                    # 而隔离态同一批 200 行只要 30.1s"的直接原因。
                    # 现判据改为**看本进程自己的 CPU 占用**（与谁在持锁无关，故不会被
                    # "某个标志一开始就是真"绕过）：
                    #   连续 IDLE_WINDOW 秒内本进程 CPU 增量 < IDLE_MAX_CPU_S ⇒ 判定空闲 ⇒ 开始预热。
                    # 这同时天然覆盖"聚合器索引/BM25 还在跑"的窗口（那时 CPU 必然高）。
                    try:
                        _busy_wait_max = float(os.environ.get(
                            "TRINITY_PREWARM_BUSY_WAIT_MAX_S", "1800") or 1800)
                        _idle_window = float(os.environ.get(
                            "TRINITY_PREWARM_IDLE_WINDOW_S", "20") or 20)
                        _idle_max_cpu = float(os.environ.get(
                            "TRINITY_PREWARM_IDLE_MAX_CPU_S", "1.0") or 1.0)
                    except Exception:  # noqa: BLE001
                        _busy_wait_max, _idle_window, _idle_max_cpu = 1800.0, 20.0, 1.0
                    _t0 = time.time()

                    def _proc_cpu_s() -> float:
                        """本进程累计 CPU 秒（Windows：GetProcessTimes）。取不到返回 -1.0。

                        ⚠️ 必须显式设 `restype`/`argtypes`：64 位下 `GetCurrentProcess()` 返回
                        `HANDLE`，若不设则 ctypes 默认按 **c_int** 接收 ⇒ 句柄被截断 ⇒
                        `GetProcessTimes` 静默失败（实测返回 0，会把"忙"误判成"空闲"）。
                        """
                        try:
                            import ctypes
                            from ctypes import wintypes
                            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                            k32.GetCurrentProcess.restype = wintypes.HANDLE
                            k32.GetCurrentProcess.argtypes = []
                            k32.GetProcessTimes.restype = wintypes.BOOL
                            k32.GetProcessTimes.argtypes = [
                                wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                ctypes.POINTER(wintypes.FILETIME),
                                ctypes.POINTER(wintypes.FILETIME),
                                ctypes.POINTER(wintypes.FILETIME),
                            ]
                            h = k32.GetCurrentProcess()
                            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                            if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                                   ctypes.byref(k), ctypes.byref(u)):
                                def _f(t):  # FILETIME → 秒
                                    return (t.dwHighDateTime << 32 | t.dwLowDateTime) / 1e7
                                return _f(k) + _f(u)
                        except Exception:  # noqa: BLE001 — 取不到就退化为"永不判定空闲"
                            return -1.0
                        return -1.0

                    _idle_since = None
                    _wait_used = 0.0
                    while _wait_used < _busy_wait_max:
                        _c0 = _proc_cpu_s()
                        time.sleep(_idle_window)
                        _wait_used += _idle_window
                        _c1 = _proc_cpu_s()
                        if _c0 < 0 or _c1 < 0:
                            break  # 取不到 CPU ⇒ 不再等，直接开始（保持旧行为）
                        if (_c1 - _c0) < _idle_max_cpu:
                            _idle_since = _wait_used
                            break
                    # 对策 b：打出"是否命中加载 / 索引已有多少行 / 等了多久才空闲" ——
                    # 这是**断点续跑是否生效**与**让路是否真的发生**的唯一可观测证据。
                    try:
                        from trinity.core.client import _corpus_persist as _cpd2
                        _vi0 = getattr(mem, "_vector_index", None)
                        _s0 = getattr(_vi0, "size", None)
                        logger.warning(
                            "PREWARM-START loaded_hit=%s idx_rows=%s waited_for_idle=%.0fs "
                            "idle_at=%s bm25=%s",
                            _cpd2.loaded_from_disk(), (_s0() if callable(_s0) else "n/a"),
                            _wait_used, _idle_since, getattr(mem, "_bm25_ready", None))
                    except Exception as _de:  # noqa: BLE001 — 诊断绝不阻塞预热
                        logger.warning("PREWARM-START 诊断失败: %r", _de)
                    with corpus_budget_scope(_wb):
                        for _ri in range(300):
                            _waited = 0.0
                            while _inflight_searches() > 0 and _waited < 60.0:
                                time.sleep(1.0)
                                _waited += 1.0
                            # t30（R-9，第 5 个变体）：**预热不是读取需求** —— 传 `account=False`。
                            # 本行在一个 `for _ri in range(300)` 里，修前每轮都会给命中的 8 行各 +1
                            # （实测 5 轮 ⇒ 每行 Δ=5，外推 300 轮 ⇒ 每行 +300）⇒ 凭空制造读事件。
                            # 判据：tests/unit/test_access_count_single_count_20261006.py::test_预热路径不得改变access_count_t30
                            mem._vector_search("预热", 8, account=False)
                            # 每 5 轮报进度 ⇒ 可判断"是否在被重启前真的在推进"
                            if _ri < 2 or _ri % 5 == 0:
                                _vi = getattr(mem, "_vector_index", None)
                                _sz = getattr(_vi, "size", None)
                                logger.warning(
                                    "PREWARM-PROGRESS round=%d idx_rows=%s n_seen=%s complete=%s",
                                    _ri, (_sz() if callable(_sz) else "n/a"),
                                    len(getattr(mem, "_vec_index_seen", {}) or {}),
                                    getattr(mem, "_vec_index_complete", None))
                            if getattr(mem, "_vec_index_complete", False):
                                break
                except Exception as _we:  # noqa: BLE001 — 预算作用域建立失败不得影响预热
                    swallow(__name__, _we)
            except Exception:  # noqa: BLE001 — 预热尽力而为，失败不影响任何行为
                swallow(__name__, None)

    _th.Thread(target=_warm, daemon=True, name="api-startup-prewarm").start()


def get_aggregator() -> MemoryAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = create_aggregator(persist=True)
    return _aggregator


def get_memory() -> Trinity:
    global _memory
    if _memory is None:
        _memory = Trinity()
    return _memory


def _live_memory() -> Trinity:
    """Resolve get_memory() through the server package at call time.

    The monolith resolved get_memory as a module global, so
    monkeypatch.setattr(server, "get_memory", stub) was honored by every
    endpoint and by the lifespan. After the package split the endpoints and
    lifespan live in other modules; routing the lookup through the package
    attribute reproduces the same semantics (patched when patched, real
    otherwise).
    """
    from trinity.api.server import get_memory as _gm
    return _gm()


def _live_aggregator() -> MemoryAggregator:
    """Resolve get_aggregator() through the server package at call time
    (same rationale as _live_memory)."""
    from trinity.api.server import get_aggregator as _ga
    return _ga()


# TTL-cached aggregator statistics for /metrics (avoids rebuilding the
# pool distribution on every scrape). Refreshed at most once per TTL.
_mem_stats_cache: Dict[str, Any] = {"ts": 0.0, "stats": None}
_MEM_STATS_TTL = 5.0


# Static files directory (package moved server.py -> server/: one level up)
_static_dir = Path(__file__).parent.parent / "static"


# GraphQL schema (imported defensively; server/__init__.py mounts the router)
try:
    from trinity.api.graphql_schema import schema as _trinity_graphql_schema
except Exception:
    _trinity_graphql_schema = None


# ═══════════════════════════════════════════════════════════════════════════
# HTTP middleware handlers (bodies identical to the monolith; the
# @app.middleware("http") decorators were moved to server/__init__.py which
# registers them in the original order: global_error, rate_limit,
# request_logging, metrics — metrics last = outermost).
# ═══════════════════════════════════════════════════════════════════════════
async def global_error_handler(request: Request, call_next):
    """Catch-all error middleware —returns structured error JSON."""
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as exc:
        # 2026-09-30（外部审计 · 后补）：**这个 catch-all 此前不记录任何东西** ——
        # 只把 `str(exc)` 塞进响应体，栈就永久丢了。
        # 现场：`GET /memories?query=…` 稳定返回 500
        # `'utf-8' codec can't decode byte 0xbd in position 3: invalid start byte`，
        # 而 `api.out.log` / `api.err.log` 里 grep `0xbd`、`codec can't decode`
        # **命中 0 行** ⇒ 无从定位（同一类问题在项 16 也出现过：引擎降级无留痕）。
        # 现在把**完整栈**记进日志（WARNING 级，避免只依赖 ERROR 过滤），
        # 响应体保持原样（不把内部栈暴露给调用方）。
        logger.warning(
            "unhandled exception on %s %s: %s: %s",
            request.method, request.url.path, type(exc).__name__, exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "detail": str(exc),
                "path": request.url.path,
            },
        )


async def rate_limit_middleware(request: Request, call_next):
    """Token-bucket rate limiting on /memories, /memory/* and /agents/* write endpoints.

    - Only POST/PUT/DELETE are limited; read endpoints (GET/HEAD) pass through.
    - /metrics is exempt (no rate limiting, no counting loop).
    - Config via env: TRINITY_RATE_LIMIT_ENABLED (default on),
      TRINITY_RATE_LIMIT_RATE (default 60/s), TRINITY_RATE_LIMIT_BURST (default 120).
    - Denials return 429 {"error": "rate_limit_exceeded", "detail": ...} and are
      counted in trinity_rate_limit_denied_total{path}.
    """
    path = request.url.path
    if (
        rate_limit_enabled()
        and is_rate_limited_request(path, request.method)
        and not _rate_limiter.consume()
    ):
        get_metrics().inc(
            "trinity_rate_limit_denied_total",
            {"path": path},
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "detail": (
                    f"Too many write requests (limit {_rate_limiter.rate}/s, "
                    f"burst {_rate_limiter.burst}); retry later"
                ),
            },
        )
    return await call_next(request)


async def request_logging_middleware(request: Request, call_next):
    """Structured request logging + OpenTelemetry-compatible trace span."""
    from trinity.telemetry import get_tracer

    tracer = get_tracer()
    span = tracer.start_span("api.request", attributes={"method": request.method, "path": request.url.path})
    start = time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as exc:
        span.error(exc)
        raise
    finally:
        elapsed = (time.time() - start) * 1000
        print(f'[api] {request.method} {request.url.path} →{status} ({elapsed:.1f}ms)')
        span.set_attribute("status", status)
        span.set_attribute("elapsed_ms", round(elapsed, 1))
        span.ok()
        span.finish()
        tracer.end_span(span)


# Metrics middleware is registered last so it is the OUTERMOST layer:
# it wraps the whole chain and therefore also records rate-limit 429
# responses and the full end-to-end request duration.

async def metrics_middleware(request: Request, call_next):
    """Prometheus request metrics — /metrics itself is skipped (no scrape loop)."""
    return await metrics_dispatch(request, call_next)
