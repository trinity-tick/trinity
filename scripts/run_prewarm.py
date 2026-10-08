#!/usr/bin/env python3
"""WS-B B3: 冷启动预热脚本（只读零 DB 写入，幂等，TTL 门控）。

预热动作（尽力而为，失败不致命，逐项报告）：
  1) jieba 分词器字典加载（cut 一次最小语料触发词典/缓存初始化）
  2) 当前默认存储后端一次"代表性首查"（预热连接 + 解析/统计/JIT）：
       - postgresql（默认主存储）：只读事务 SELECT count(*)/SELECT 1（含向量通道连通）
       - sqlite：TRINITY_STORE 库已存在则只读查一次；不存在→跳过（不建库，守零写入）
  3) 向量通道探测：PG 由 psycopg2 SELECT 1（pgvector profile 随连接验证）；
     sqlite 分支在引擎内具象，本脚本不冷初始化 faiss/bge（零写入守则）
  4) ANN 索引存在性 + mtime 报告（~/.trinity/data/ann_index.bin），并对比上次状态

幂等：state 文件 ~/.trinity/state/prewarm_last.json 记录 ts；距上次 <
TRINITY_PREWARM_TTL_S（默认 21600=6h）直接 exit 0 输出 skipped；--force 忽略 TTL。

只读/隔离承诺：本脚本绝不 CREATE/INSERT/UPDATE/commit 到库；PG 连接以
read-only 事务防御；冒烟/评测以 TRINITY_STORE 指向临时文件触发 sqlite 分支自隔离。

用法： python scripts/run_prewarm.py [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

REPO = Path(__file__).resolve().parent  # scripts/
ROOT = REPO.parent
sys.path.insert(0, str(ROOT))

STATE_DIR = Path.home() / ".trinity" / "state"
STATE_FILE = STATE_DIR / "prewarm_last.json"
ANN_FILE = Path.home() / ".trinity" / "data" / "ann_index.bin"
DEFAULT_TTL_S = 21600  # 6h


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as _e:
        swallow(__name__, _e)
    return {}


def _should_skip(args, state) -> bool:
    if args.force:
        return False
    last = state.get("ts")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except Exception:
        return False
    ttl = int(os.environ.get("TRINITY_PREWARM_TTL_S", str(DEFAULT_TTL_S)))
    if age < ttl:
        print(f"[prewarm] skipped: last {age:.0f}s ago < TTL {ttl}s "
              f"(remaining {int(ttl - age)}s). State={STATE_FILE}")
        return True
    return False


def _warm_jieba() -> bool:
    """初始化 jieba（词典加载为首次 cut 热点）。"""
    try:
        import jieba
        jieba.setLogLevel(60)
        list(jieba.cut("Trinity 记忆系统预热 001"))
        return True
    except Exception as e:  # pragma: no cover
        print(f"[prewarm] jieba init FAILED: {e}")
        return False


def _resolve_backend() -> str:
    """后端判定：显式 env TRINITY_STORAGE_BACKEND → TRINITY_STORE 显式→sqlite
    → creds yaml → 默认 postgresql。"""
    v = os.environ.get("TRINITY_STORAGE_BACKEND", "").strip().lower()
    if v in ("postgresql", "pg"):
        return "postgresql"
    if v == "sqlite":
        return "sqlite"
    if os.environ.get("TRINITY_STORE") or os.environ.get("TRINITY_DB_PATH"):
        return "sqlite"
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
            # t31：凭证文件是**版本化结构**（真键缩进在 refs 下）⇒ 顶层 .get 恒空。
            # 逐字兜底：refs 打底、顶层覆盖（与 _pg_std/security.credentials 的读法一致）。
            cfg = {**(cfg.get("refs") or {}), **cfg}
            k = str(cfg.get("TRINITY_STORAGE_BACKEND", "")).strip().lower()
            if k in ("postgresql", "pg"):
                return "postgresql"
    except Exception as _e:
        swallow(__name__, _e)
    return "postgresql"


def _prewarm_pg_first_query() -> dict:
    out = {"ok": False, "count": None, "detail": ""}
    try:
        import psycopg2
        from psycopg2 import pool as pg_pool
        from trinity.security.credentials import resolve_credentials
        creds = resolve_credentials()
        creds.setdefault("host", "127.0.0.1")  # 规避 localhost IPv6 被 pg_hba 拒
        pool = pg_pool.SimpleConnectionPool(1, 2, **creds)
        try:
            conn = pool.getconn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("SET default_transaction_read_only = on")
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM memories")
                        out["count"] = cur.fetchone()[0]
                        cur.execute("SELECT 1")  # 连通/解析预热 + 向量 profile 前提
                    out["ok"] = True
                    out["detail"] = "pg read-only count + SELECT 1 ok"
            finally:
                pool.putconn(conn)
        finally:
            pool.closeall()
    except Exception as e:  # pragma: no cover
        out["detail"] = f"pg prewarm failed: {e}"
    return out


def _prewarm_sqlite_first_query() -> dict:
    out = {"ok": False, "count": None, "detail": ""}
    # 2026-09-27（§1361，与 fuse_docs 同族）：生产里 `TRINITY_STORE` 是**目录** ⇒ 直接当文件会指向目录，
    # 「存在」判断通过但打开失败。改法同既有惯例：是目录就拼 `trinity_store.db`。
    store = os.environ.get("TRINITY_STORE") or "~/.trinity/store/trinity_store.db"
    p = Path(store).expanduser()
    if p.is_dir():
        p = p / "trinity_store.db"
    if not p.exists():
        out["detail"] = f"sqlite store absent {p} — skip (zero-write, no db creation)"
        out["ok"] = True
        return out
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT count(*) FROM memories")
            out["count"] = cur.fetchone()[0]
            out["ok"] = True
            out["detail"] = f"sqlite read-only count @ {p}"
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover
        out["detail"] = f"sqlite prewarm failed: {e}"
    return out


def _warm_corpus_index() -> dict:
    """**语料向量索引预热（2026-10-05：本正解在此落地）**。

    为什么放在**这里**（独立进程）而不是 API 的 lifespan：
      ① API 进程被 supervisor **刻意**限成 `TRINITY_ONNX_THREADS=1`
         （`trinity-supervisor.ps1:84-93` §1020：8 线程曾把 CPU 占满 ⇒ uvicorn 拿不到
         时间片 ⇒ 健康探测 >20s ⇒ 被 supervisor 判「不服务」而杀）。
         实测后果：同一批 200 行，**隔离进程 30.1s（0.150 s/行）**，
         而 **API 进程内 >20 分钟未完成（≈40×）**。
      ② 本仓既有纪律（`trinity-supervisor.ps1:97`）：
         **「批量写入走维护链，不与在线服务抢」** —— 预热正是后台批量活。

    做法：用**线程局部预算**（与 lifespan 里那条预热完全同构）+ 全线程 ONNX，
    跑 `_vector_search("预热", 8)` 若干轮直到覆盖或达上限；
    索引与嵌入缓存会**自动落盘**（`_corpus_persist` 的 save + `CachedEmbeddingEngine.save_cache`），
    此后 API 启动只需 **加载**（已实测：加载命中的进程每轮 3.5s，且嵌入缓存 0 命中即 0 秒）。

    **零 DB 写入**承诺不变：只读库、只写 `~/.trinity/data/` 下的缓存文件。
    """
    import time as _t
    rep: dict = {"ok": False, "rounds": 0, "idx_rows": 0, "seconds": 0.0, "note": ""}
    t0 = _t.time()
    # ── **并发守卫**（2026-10-05）───────────────────────────────────────────────
    # 依据（实测）：诊断期间观察到 **autostart 的维护循环**（`perception-continuous` /
    # `brain-event` / `consolidate-recent`）与我的测试**并发运行** —— 我无法在自己的
    # 测量里排除这个因子，而两份预热同时跑既互相拖慢、又都在写同一组缓存文件。
    # 故用**独占锁文件**兜住：拿不到锁就**跳过**（打印 skipped，rc=0，不报 FAILED）。
    # 锁文件带 pid + 时间戳；**超过 STALE 秒视为陈旧**（上次进程被杀留下的），可夺取。
    _lock = os.path.join(os.path.expanduser("~/.trinity"), "data", "corpus_prewarm.lock")
    _lock_fd = None
    try:
        _stale = float(os.environ.get("TRINITY_PREWARM_LOCK_STALE_S", "7200") or 7200)
    except Exception:  # noqa: BLE001
        _stale = 7200.0
    # ⚠️ 2026-10-05：这里曾加过一个"忙碌守卫"（数竞争 python 进程数 > 阈值就跳过），
    # **已移除** —— 实测它**没有判别力**：本机平时就常驻 ~7 个 python 进程
    # （API/MCP/collector 等），进程数**恒高** ⇒ 守卫会**永远跳过**预热。
    # 教训（与本仓 G10 同型）：判据必须能**区分**两种状态，否则它不是守卫而是开关。
    #
    # ── **空闲判定（错峰让路）**（2026-10-05，取代上面那个坏守卫）────────────────
    # 用**可直接测量的量**：系统级 CPU 空闲率（两次采样之差），与"谁在跑"无关。
    # 依据（本会话最关键的性能事实，逐一判别排除后得到）：
    #   · `TRINITY_ONNX_THREADS` 1 vs 8 → 0.101 vs 0.105 s/行 ⇒ **不影响**吞吐；
    #   · 文本长度（首 200 行平均 370、最大 3000 字符）→ 隔离 0.150 s/行 ⇒ **不影响**；
    #   · **与其它维护任务并发** → 隔离 0.107~0.150 vs 维护链运行中 **~7.9 s/行**（**≈50×**）。
    #   ⇒ 忙时跑预热＝**空烧满预算再被判 FAILED**（实测两次 `exit 124`）。
    # 语义：采样 `CPU_IDLE_WINDOW_S` 秒，空闲率 < `CPU_IDLE_MIN_PCT` ⇒ 判定"忙"⇒
    #       **跳过**（rc=0、留痕、不报 FAILED），把预算留给安静的窗口。
    # 踩过的坑：`GetSystemTimes` **必须设 restype/argtypes** —— 不设时 64 位下
    #       FILETIME 被截断、调用静默失败（实测返回 0 ⇒ 把"忙"误判成"空闲"）。
    try:
        _idle_win = float(os.environ.get("TRINITY_PREWARM_CPU_IDLE_WINDOW_S", "6") or 6)
        _idle_min = float(os.environ.get("TRINITY_PREWARM_CPU_IDLE_MIN_PCT", "80") or 80)
    except Exception:  # noqa: BLE001
        _idle_win, _idle_min = 6.0, 80.0

    def _sys_cpu_idle_s() -> float:
        """系统累计空闲 CPU 秒（Windows）。取不到返回 -1.0。"""
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetSystemTimes.restype = wintypes.BOOL
            k32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME),
                                          ctypes.POINTER(wintypes.FILETIME),
                                          ctypes.POINTER(wintypes.FILETIME)]
            idle, kern, user = (wintypes.FILETIME() for _ in range(3))
            if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return -1.0

            def _f(t):
                return (t.dwHighDateTime << 32 | t.dwLowDateTime) / 1e7
            return _f(idle)
        except Exception:  # noqa: BLE001
            return -1.0

    if _idle_min > 0 and _idle_win > 0:
        try:
            _i0 = _sys_cpu_idle_s()
            _t0i = _t.time()
            _t.sleep(_idle_win)
            _i1 = _sys_cpu_idle_s()
            _wall = max(1e-6, _t.time() - _t0i)
            import os as _os
            _ncpu = _os.cpu_count() or 1
            if _i0 >= 0 and _i1 >= 0:
                _idle_pct = (_i1 - _i0) / (_wall * _ncpu) * 100.0
                rep["note"] += f"cpu_idle={_idle_pct:.0f}%({_ncpu}cpu); "
                if _idle_pct < _idle_min:
                    rep["note"] = (f"skipped: host busy (cpu_idle={_idle_pct:.0f}% < "
                                   f"{_idle_min:.0f}%); 并发时嵌入实测 ~7.9 s/行 vs 空闲 0.15 s/行，"
                                   f"跑也是空烧预算")
                    rep["seconds"] = round(_t.time() - t0, 1)
                    return rep
            else:
                rep["note"] += "cpu_idle=unavailable; "
        except Exception as _ie:  # noqa: BLE001 — 判定本身失败不阻塞预热
            rep["note"] += f"idle-check unavailable ({_ie!r}); "
    try:
        os.makedirs(os.path.dirname(_lock), exist_ok=True)
        if os.path.exists(_lock):
            try:
                _age = _t.time() - os.path.getmtime(_lock)
            except Exception:  # noqa: BLE001
                _age = 0.0
            if _age < _stale:
                rep["note"] = f"skipped: another corpus prewarm holds the lock (age={_age:.0f}s)"
                rep["seconds"] = 0.0
                return rep
            # 陈旧锁 ⇒ 夺取（记录留痕）
            rep["note"] = f"took over stale lock (age={_age:.0f}s); "
        _lock_fd = open(_lock, "w", encoding="utf-8")
        _lock_fd.write(f"{os.getpid()} {_t.time():.0f}\n")
        _lock_fd.flush()
    except Exception as _le:  # noqa: BLE001 — 守卫本身失败不阻塞预热
        rep["note"] += f"lock unavailable ({_le!r}); proceeding; "
        _lock_fd = None
    try:
        from trinity.core.client import Trinity
        from trinity.core.client import _corpus_persist as _cp
        from trinity.core.client._vec_budget import corpus_budget_scope
        mem = Trinity()
        try:
            budget = int(os.environ.get("TRINITY_VEC_CORPUS_WARM_BUDGET", "200") or 200)
        except Exception:  # noqa: BLE001
            budget = 200
        try:
            max_rounds = int(os.environ.get("TRINITY_PREWARM_CORPUS_MAX_ROUNDS", "300") or 300)
        except Exception:  # noqa: BLE001
            max_rounds = 300
        # **墙钟预算**（2026-10-05）：全量 2 万行在独立进程约 50 分钟，而调用方的预算是有限的
        # ⇒ 必须**按时间分块**：到点就保存并退出，**下次接着跑**（靠 `_corpus_persist` 的加载 +
        # `_seen` 播种续跑，已实测：加载命中的进程每轮 3.5s、嵌入缓存命中时 0 秒）。
        #
        # ⚠️ **三层预算必须留出余量**（实测踩过，见下）：
        #     维护 wrapper 1800s > `Invoke-Task` 预算 1700s > 本子进程 timeout 1700s
        #     > **内层墙钟预算（本值，默认 1200s）**
        #   原默认取 1500s ⇒ 内层 1500 + 收尾/保存 与外层 1700 **贴得太近**，
        #   实测被外层 `TIMEOUT (1700s task budget)` 掐死、**`save_now()` 没跑到、什么都没落盘**
        #   （2026-10-05 10:05 那次：`prewarm : FAILED (exit 124)`，`~/.trinity/data/corpus_vec*` 为空）。
        #   取 1200s ⇒ 留 500s 余量给"跑完最后一轮 + 落盘 + 退出"。
        try:
            budget_s = float(os.environ.get("TRINITY_PREWARM_CORPUS_BUDGET_S", "1200") or 1200)
        except Exception:  # noqa: BLE001
            budget_s = 1200.0
        _deadline = t0 + budget_s
        with corpus_budget_scope(budget):
            for _ri in range(max_rounds):
                # ⚠️ **必须在每轮开始前也查一次**（2026-10-05 实测踩到）：
                #   原先只在**一轮跑完之后**查 ⇒ 若单轮本身耗时超过剩余预算，
                #   就会**大幅超支**（实测：预算 300s 的那次跑了 >600s 仍在
                #   `_embed_batch_raw` 里，因为第 1 轮就远超 300s，检查点还没轮到）。
                #   放在轮前 ⇒ 只要已经到点就**立刻**停并落盘，不会被单轮拖穿外层预算。
                if _t.time() >= _deadline:
                    rep["note"] = f"wall-clock budget reached ({budget_s:.0f}s) — 保存进度，下次续跑"
                    break
                mem._vector_search("预热", 8)
                rep["rounds"] = _ri + 1
                if getattr(mem, "_vec_index_complete", False):
                    break
                if _t.time() >= _deadline:
                    rep["note"] = f"wall-clock budget reached ({budget_s:.0f}s) — 保存进度，下次续跑"
                    break
        # ⚠️ 必须在**循环之后**读：索引加载发生在循环内第一次 `_vector_search`
        #（`_search.py` 里 `_get_vector_index()` 才调 `load_corpus_index`），
        # 在循环前读会**永远是 False**（2026-10-05 实测踩到：明明续跑生效却报 False）。
        rep["loaded_from_disk"] = _cp.loaded_from_disk()
        _vi = getattr(mem, "_vector_index", None)
        _sz = getattr(_vi, "size", None)
        rep["idx_rows"] = _sz() if callable(_sz) else 0
        rep["complete"] = bool(getattr(mem, "_vec_index_complete", False))
        rep["saved"] = bool(_cp.save_now())
        try:
            eng = getattr(mem, "_embedding_engine", None)
            if eng is not None and hasattr(eng, "save_cache"):
                rep["embed_cache_saved"] = bool(eng.save_cache())
        except Exception as _ee:  # noqa: BLE001 — 嵌入缓存落盘尽力而为
            rep["embed_cache_saved"] = False
            rep["embed_note"] = repr(_ee)[:80]
        rep["ok"] = True
    except Exception as _e:  # noqa: BLE001 — 预热尽力而为，失败不影响主流程
        rep["note"] = repr(_e)[:160]
    finally:
        # 释放并发守卫锁（任何路径都要释放，否则下次会被自己的锁挡住）
        try:
            if _lock_fd is not None:
                _lock_fd.close()
            if os.path.exists(_lock):
                os.remove(_lock)
        except Exception as _re:  # noqa: BLE001 — 释放失败只会让下次等多一会儿（有 stale 兜底）
            rep["note"] += f"lock release failed ({_re!r}); "
    rep["seconds"] = round(_t.time() - t0, 1)
    return rep


def check_ann(state: dict) -> dict:
    """ANN 索引存在性/mtime 报告，并对比上次状态标记 changed。"""
    report = {"ann_present": ANN_FILE.exists(), "ann_path": str(ANN_FILE)}
    if ANN_FILE.exists():
        st = ANN_FILE.stat()
        report["ann_mtime"] = datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        report["ann_size_mb"] = round(st.st_size / (1024 * 1024), 2)
        last_m = (state.get("ann") or {}).get("mtime")
        report["ann_changed_since_last"] = (last_m is not None and report["ann_mtime"] != last_m)
    else:
        report["ann_mtime"] = None
        report["ann_size_mb"] = 0
        report["ann_changed_since_last"] = None
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trinity cold-start prewarm (read-only)")
    ap.add_argument("--force", action="store_true", help="ignore TTL and re-warm")
    # ⚠️ `default=None`（不是 `str(STATE_FILE)`）：`--state` 是**覆盖**用的，
    #   而下面曾写成 `state = _load_state() if not args.state else {}` ——
    #   由于默认值恒为真，那个三元**永远走 else** ⇒ `state` 恒为空 ⇒
    #   `_should_skip` **永远判定"不 skip"** ⇒ **TTL 6h 幂等形同虚设**、每次触发都全跑
    #   （白烧 CPU）。2026-10-05 实测发现：真实 `~/.trinity/state/prewarm_last.json`
    #   根本不存在，而每次运行都打印 "state updated"。修法：默认 None ⇒
    #   未显式传 `--state` 时才读默认状态文件。判据：连续两次运行，第二次应打印
    #   "skipped (TTL ...)" 且 rc=0。
    ap.add_argument("--state", default=None,
                    help="optional alternate state path (tests)")
    ap.add_argument("--no-corpus", action="store_true",
                    help="skip the corpus vector-index warm (2026-10-05)")
    args = ap.parse_args(argv)

    _state_path = Path(args.state) if args.state else STATE_FILE
    if args.state:
        state = {}
    else:
        # 读**真实**状态文件（`_load_state()` 硬编码 STATE_FILE）以让 TTL 生效。
        try:
            if STATE_FILE.exists():
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            else:
                state = {}
        except Exception:  # noqa: BLE001 — 状态文件损坏不应阻塞预热
            state = {}
    if _should_skip(args, state):
        return 0

    backend = _resolve_backend()
    print(f"[prewarm] backend={backend}")

    j_ok = _warm_jieba()
    print(f"[prewarm] jieba init: {'ok' if j_ok else 'fail'}")

    if backend == "postgresql":
        pg = _prewarm_pg_first_query()
        print(f"[prewarm] pg first-read: count={pg.get('count')} ok={pg['ok']} | {pg.get('detail')}")
        print(f"[prewarm] vector(pg): probed via SELECT 1; ok={pg['ok']}")
    else:
        sq = _prewarm_sqlite_first_query()
        print(f"[prewarm] sqlite first-read: count={sq.get('count')} ok={sq['ok']} | {sq.get('detail')}")
        print("[prewarm] vector(sqlite): lives in engine; not cold-built here (zero-write)")

    ann = check_ann(state)
    print(f"[prewarm] ann: present={ann['ann_present']} mtime={ann.get('ann_mtime')} "
          f"size_mb={ann.get('ann_size_mb')} changed_since_last={ann['ann_changed_since_last']}")

    # 2026-10-05：**语料向量索引预热**（独立进程、可用全线程、不与在线服务抢）。
    # 开关：TRINITY_PREWARM_CORPUS=0 关闭；--no-corpus 亦可。
    _corpus_rep = {}
    if os.environ.get("TRINITY_PREWARM_CORPUS", "1") != "0" and not getattr(args, "no_corpus", False):
        _corpus_rep = _warm_corpus_index()
        print(f"[prewarm] corpus index: ok={_corpus_rep['ok']} rounds={_corpus_rep['rounds']} "
              f"idx_rows={_corpus_rep['idx_rows']} complete={_corpus_rep.get('complete')} "
              f"loaded_from_disk={_corpus_rep.get('loaded_from_disk')} "
              f"saved={_corpus_rep.get('saved')} seconds={_corpus_rep['seconds']}"
              + (f" | note={_corpus_rep['note']}" if _corpus_rep.get("note") else ""))
    else:
        print("[prewarm] corpus index: skipped (disabled)")

    try:
        d = _state_path.parent
        d.mkdir(parents=True, exist_ok=True)
        new_state = {"ts": _now_iso(), "backend": backend,
                     "jieba_ok": j_ok, "ann": {"mtime": ann.get("ann_mtime")}}
        _state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"[prewarm] state updated -> {_state_path}")
    except Exception as e:  # pragma: no cover
        print(f"[prewarm] state write failed (non-fatal): {e}")

    print("[prewarm] done (zero DB writes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
