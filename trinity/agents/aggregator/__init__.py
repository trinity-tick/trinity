"""Memory Aggregator - shared memory pool with dimension indexing (package decomposition, 2026-08-17).
The former monolith aggregator.py was split into domain mixins (_init/_persist/_ingest/_search/_vector/_rl/_graph/_stats/_maintenance/_similarity/_diagnostics). Public API unchanged: MemoryAggregator, create_aggregator, self_test and _AggregatorKGraphAdapter are re-exported here; module constants are re-exported from ._constants.
"""

from __future__ import annotations

import json
import logging
import math
import numpy as np  # 2026-09-30：向量索引持久化改用 NPZ（见下），替代 pickle
import os
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

# EXECUTION 519 (C2p-II): numpy 延迟到使用点导入（冷启动剪 numpy 链）
from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

from ._constants import (SIMILARITY_MERGE_THRESHOLD, MAX_POOL_SIZE, PERSIST_FILENAME, PERSIST_DEBOUNCE_SECONDS, PERSIST_MAX_DIRTY, VECTOR_PERSIST_FILENAME, CLEANUP_INTERVAL_SECONDS, _HAS_FAISS, _SENTINEL, logger,
                        MIN_ROWS_FOR_INDEX_RATIO, MIN_INDEX_LANDING_RATIO)
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


# ── 2026-09-20 §1002：替换重试（治「Aggregator persist failed: [WinError 5]」）─────────
# 实测（2026-09-20 的 api.err 日志，按签名归并）：同类失败 **54 次**（52 次「拒绝访问」+
# 2 次「另一个程序正在使用此文件」），全部死在 os.replace(tmp -> aggregator_pool.json)；
# 而且**每失败一次就留下一个 74MB 的 .tmp**（data/ 下累计 6 个 / 124.8MB）。
# 根因是 Windows 语义，不是并发 bug：只要目标文件被任何**未共享删除**的读者或写者打开，
# rename 立刻 WinError 5；而这类占用**天然瞬时**（读者读完即关、重复实例随即被杀）。
# 所以正确处置不是换写法，而是**有界重试**——绝大多数冲突在几百毫秒内自解。
# 回滚：TRINITY_POOL_REPLACE_ATTEMPTS=1 ⇒ 退回单次尝试（改动前行为）。
PERSIST_RETRY_STATS: Dict[str, Any] = {"attempts": 0, "retries": 0, "failures": 0, "last_error": None}


# ── 2026-09-22 §1270：**索引持久化的写侧哑线**（「池写了、索引静默没写」）────────────
# 实测（本轮）：`data/aggregator_vectors.pkl` **不存在**，只剩 09-20 22:13 被改名的
# `.stale-20260920` / `.bak-20260920`（各 74.94MB），而那次改名**在任何登记里都查不到**。
# 而本文件 `_persist()` 写索引的条件是 `self._faiss_index is not None and self._index_id_map`
# ⇒ **写池的进程若没有热索引，索引文件被静默跳过**（无日志、无计数）。后果：
# 池文件照写（今天 11:50 还写过），索引文件一旦丢失就**再也回不来** ⇒ 每次进程启动都付一次
# **全量重建**（本轮实测 0.15–0.30 s/行，池 19,313 条 ⇒ 约 50 分钟–1.6 小时；§1019/§1024/
# §1184/§1246 那串「API 停摆 / 健康探测判死」的上游就是这个）。
# 修法（只加读数、不改行为）：跳过时**分原因计数 + 节流告警**，落了盘也计数；
# 读数经 `statistics()["vector_persist"]` 出到 `/metrics`（消费者看得见才算数，§13.5）。
#
# ── 2026-09-22 §1271：**把「刚建好索引」这个时刻用起来**（自愈，不再排重活）──────────
# 上面那条「只加读数」还留着一个缺口：**没人**会在「我持有完整索引」时把文件补回去
# ——`_save()` 只在池有脏写时跑，而写池的进程常常没有热索引。于是 D10 原本的选项 B
# （挂维护链跑一次 50–90 分钟的重活）在尾段 **1200s 预算**下**根本跑不完**（§812-§820
# 就是「38 任务挤 1800s 被系统性饿死」的前科）。改法换成：**索引刚建完就顺手落盘**
# （成本 ≈ 一次 74MB 写，秒级；见 `_vector.py::_persist_index_if_missing`）。
# 回滚：`TRINITY_VEC_SELFHEAL=off`。
VECTOR_PERSIST_STATS: Dict[str, Any] = {
    "written": 0,
    "skipped_no_index": 0,
    "skipped_empty_id_map": 0,
    "failed_no_file": 0,        # §1270：faiss 写不出文件（非 ASCII 路径下**两种形态都见过**）
    "persisted_after_rebuild": 0,   # §1271：建好索引后**顺手落盘**（文件缺失/比池旧时）
    "skipped_fresh": 0,             # §1271：文件已存在且不比池旧 ⇒ **故意不写**（别每次写 ~150MB）
    "selfheal_error": 0,            # §1271：自愈本身失败（必须留痕，不许静默）
    "last_skip_ts": 0.0,
}

# ── 2026-10-02（外部复评 §3.3）：**池文件**落盘的成色也要有出口 ─────────────────
# 为什么加：`_save()` 的失败分支此前**只有一行 `logger.warning("Aggregator persist failed")`**，
# 没有任何计数器 ⇒ 与索引侧的 `VECTOR_PERSIST_STATS` 不对称。而 `scripts/_pool_write_guard.py`
# 的模块头自己记着同族事故：「API 进程撞车就是 `Aggregator persist failed: [WinError 5] 拒绝访问
# — tmp -> aggregator_pool.json` 的来源，也是 21:50/21:52 那次停摆的一环」。
# 2026-10-02 11:52–12:31 的复评里，`:8001` 在 10 分钟内死了两次（`api-forensics prevAlive=False`），
# 两次都紧邻池写入调用 —— **判死原因恰恰缺的就是"它最后一次落盘成功没有"这个读数**。
# 本改动**只加读数、不改行为**（回滚：删掉本字典与两处 `+= 1` 即可，无行为依赖）。
POOL_PERSIST_STATS: Dict[str, Any] = {
    "ok": 0,            # 池文件（+ 关系图 + stats）成功原子替换
    "failed": 0,        # 失败（权限/磁盘/序列化）——**非致命**，但必须可见
    "disabled": 0,      # persist_path 为空（memory-only 模式，故意不落盘）
    "last_error": "",
    "last_fail_ts": 0.0,
}


def _vec_persist_warn_interval() -> float:
    """跳过落盘时的告警节流间隔（秒，默认 600；`TRINITY_VEC_PERSIST_WARN_S` 可调，0 = 每次喊）。"""
    try:
        return float(os.environ.get("TRINITY_VEC_PERSIST_WARN_S") or 600)
    except Exception:  # noqa: BLE001 —— 参数坏掉时退回默认，不阻断落盘
        return 600.0


def _replace_attempts() -> int:
    """重试次数（默认 6；env 可调，1 = 关闭重试）。"""
    try:
        n = int(os.environ.get("TRINITY_POOL_REPLACE_ATTEMPTS") or 6)
    except Exception:  # noqa: BLE001 —— 参数坏掉时退回默认，不阻断落盘
        n = 6
    return max(1, min(n, 60))


def _pool_file_looks_complete(path: str) -> bool:
    """池文件**结构上是否完整**（O(1) 内存的廉价判据，用于决定要不要隔离）。

    为什么需要（2026-09-21 §1008 实测事故）：01:20 一个**完全有效**的 71.6MB /
    19,274 条池文件被当成"损坏"隔离成 .corrupt_1789925588（事后 json.load 解析通过、
    19,274 条一条不少），服务随后以 **48 条空池**运行了 8 小时——根因是
    _load 的**外层 except 把"装载过程中的任何异常"都当成"文件损坏"**
    （当时是内存压力下的装载失败/单条坏行，与文件内容无关）。
    判据：只看**首字节是不是 { 且末字节是不是 }** ⇒ 真截断会隔离，能解析的绝不隔离。
    代价 O(1)：不为了判完整性再把 71MB 读进内存（那正是当时内存紧张的原因）。
    """
    try:
        size = os.path.getsize(path)
        if size < 32:
            return False
        with open(path, "rb") as f:
            head = f.read(16)
            f.seek(max(0, size - 64))
            tail = f.read()
        return head.lstrip()[:1] == b"{" and tail.rstrip()[-1:] == b"}"
    except Exception:  # noqa: BLE001
        return False


def _sweep_stale_tmps(persist_path: str, max_age_s: float = 3600.0) -> int:
    """清掉**陈旧**的 .tmp 兄弟文件，返回删除数。

    为什么需要（2026-09-20 §1002 实测）：失败一次留一个 74MB 的 .tmp，data/ 下累计
    6 个 / 124.8MB；而进程**写到一半被杀**（如 22:13 的 API 实例 14024）同样会留尸。
    判据保守到不可能误伤：只删「同前缀 .tmp + mtime 早于 max_age_s」——
    活着的写者从 create 到 rename 只有毫秒级窗口，1 小时的旧文件必然是无主尸。
    关闭：TRINITY_POOL_TMP_SWEEP=off。
    """
    if str(os.environ.get("TRINITY_POOL_TMP_SWEEP", "on")).strip().lower() in ("0", "off", "false", "no"):
        return 0
    removed = 0
    try:
        d = os.path.dirname(persist_path) or "."
        base = os.path.basename(persist_path) + "."
        now = time.time()
        for name in os.listdir(d):
            if not (name.startswith(base) and name.endswith(".tmp")):
                continue
            p = os.path.join(d, name)
            try:
                if now - os.path.getmtime(p) <= max_age_s:
                    continue
                os.unlink(p)
                removed += 1
            except OSError:
                continue
        if removed:
            logger.warning("Aggregator: swept %d stale .tmp file(s) under %s", removed, d)
    except OSError as _e:
        # 2026-09-30：原为静默 `pass`（`silent_failure` 棘轮只降不升）⇒ 改可听见。
        logger.debug("stale .tmp sweep skipped: %s", str(_e)[:80])
    return removed


def _quarantine_unreadable(path: str) -> Optional[str]:
    """把**读不出来**的索引改名隔离（返回新路径；失败返回 None，**绝不删除**）。

    为什么不是 `os.remove`（2026-09-23 §1287 事故，实测）：这条分支是**全仓唯一**
    能删掉 `data/aggregator_vectors.pkl` 的落点，而它由「读失败」触发 —— 但读失败的原因
    可以与被读文件的质量**无关**：本进程没有 faiss（`_HAS_FAISS` 是进程级事实）、
    内存紧张时 faiss 自身分配失败、换名的瞬时占用。实测代价：D10 于 11:34 造好 75.4MB 索引、
    11:34:05 独立复核可读，**11:45 已不存在**（且没有任何备份留下）；重建一次 ≈ 50–95 分钟。

    改名而不是删除：自愈照旧（原路径被腾空 ⇒ `_prewarm_ann_index` 会重建并 `_save`），
    证据留下（`.unreadable-<ts>` 可事后判因）。判据：
    `tests/unit/test_vector_index_read_failure_keeps_file.py`。
    """
    last: Optional[BaseException] = None
    for i in range(3):
        dst = "%s.unreadable-%s%s" % (path, time.strftime("%Y%m%d-%H%M%S"),
                                      "" if i == 0 else "-%d" % i)
        try:
            os.replace(path, dst)
            logger.warning("Aggregator: unreadable vector index quarantined（隔离，未删除）: %s", dst)
            return dst
        except OSError as exc:                     # 读者占用等瞬时原因
            last = exc
            time.sleep(0.2)
    logger.warning("Aggregator: quarantine failed (%s) — 文件保持原样，**不删**: %s", last, path)
    return None


def _replace_with_retry(src: str, dst: str, attempts: Optional[int] = None) -> None:
    """os.replace + 有界指数退避重试；最终失败时**先清掉 tmp 再抛**（防垃圾累积）。

    计数落在 PERSIST_RETRY_STATS：tests/unit/test_pool_persist_retry.py 用它做前后对照
    （判据：占用在重试窗口内释放 ⇒ 必须**写成功**，而不是"少失败几次"）。
    """
    n = _replace_attempts() if attempts is None else max(1, int(attempts))
    delay = 0.15
    last: Optional[BaseException] = None
    for i in range(n):
        PERSIST_RETRY_STATS["attempts"] += 1
        try:
            os.replace(src, dst)
            return
        except OSError as exc:  # WinError 5 / 32：瞬时占用，重试即自解
            last = exc
            if i < n - 1:
                PERSIST_RETRY_STATS["retries"] += 1
                time.sleep(delay)
                delay = min(delay * 2.0, 2.0)
    PERSIST_RETRY_STATS["failures"] += 1
    PERSIST_RETRY_STATS["last_error"] = str(last)
    try:
        if os.path.exists(src):
            os.unlink(src)
    except OSError as _e:
        # 2026-09-30：原为静默 `pass`（紧随其后的 raise 才是主路径）⇒ 改可听见。
        logger.debug("pre-replace unlink skipped: %s", str(_e)[:80])
    raise last if last is not None else OSError("replace failed: %s -> %s" % (src, dst))


from ._base import _AggregatorMixinBase
class _PersistMixin(_AggregatorMixinBase):
    """Persistence / lifecycle mixin.

    Defined in the package namespace (__init__.py) on purpose: _save/_load/_mark_dirty
    read PERSIST_* / _HAS_FAISS as module globals at call time, and external
    code monkey-patches them through trinity.agents.aggregator (e.g.
    benchmark/sync_pool_from_db_v2.py sets agg_mod.PERSIST_MAX_DIRTY = 10**9;
    tests/unit/test_aggregator_index_selfheal.py does setattr(agg_mod,
    '_HAS_FAISS', True)).
    """

    def _discover_persist_path(self) -> Optional[str]:
        """Auto-discover the persistence file path via TRINITY_HOME."""
        candidates = [
            os.environ.get("TRINITY_HOME"),
            os.path.join(os.path.expanduser("~"), "trinity"),
            os.path.join(os.path.expanduser("~"), ".trinity"),
        ]
        for base in candidates:
            if base and os.path.isdir(base):
                return os.path.join(base, "data", PERSIST_FILENAME)
        # Fallback: write alongside aggregator.py
        return os.path.join(os.path.dirname(__file__), "..", "..", "data", PERSIST_FILENAME)

    def _save(self) -> None:
        """Persist the current pool and vector index to disk atomically."""
        if not self._persist_path:
            POOL_PERSIST_STATS["disabled"] += 1   # 2026-10-02 复评 §3.3：memory-only 也要可分辨
            return
        try:
            with self._lock:
                data = {
                    "version": "6.99.0",
                    "timestamp": time.time(),
                    "memories": [dv.to_dict(full=True) for dv in self._pool.values()],
                    "relations": {
                        mid: dict(edges) for mid, edges in self._relations_graph.items()
                    },
                    "stats": dict(self._stats),
                }
            # Atomic write: 每进程独立 tmp（pid 后缀，避免多进程共用 .tmp 竞态）→ fsync → rename
            persist_dir = os.path.dirname(self._persist_path)
            os.makedirs(persist_dir, exist_ok=True)
            _sweep_stale_tmps(self._persist_path)   # §1002: 顺手清理无主 .tmp（保守判据，见函数头）
            tmp_path = f"{self._persist_path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp_path, self._persist_path)   # §1002: 有界重试，见模块头
            POOL_PERSIST_STATS["ok"] += 1   # 2026-10-02 复评 §3.3：池文件已原子替换

            # ── P0-1: Persist vector index ──
            # §1270：**跳过也必须留痕** —— 见本模块 VECTOR_PERSIST_STATS 上方的事故说明。
            if self._faiss_index is None or not self._index_id_map:
                _why = "no_index" if self._faiss_index is None else "empty_id_map"
                VECTOR_PERSIST_STATS["skipped_" + _why] += 1
                _iv = _vec_persist_warn_interval()
                _now = time.time()
                if _iv <= 0 or _now - float(VECTOR_PERSIST_STATS.get("last_skip_ts") or 0.0) >= _iv:
                    VECTOR_PERSIST_STATS["last_skip_ts"] = _now
                    logger.warning(
                        "vector index NOT persisted while pool persisted (%s; pool=%d) — "
                        "下次冷启动将付一次全量重建；若 %s 缺失，用 "
                        "python scripts/ann_index_persist_rebuild.py 补建（§1270）",
                        _why, len(self._pool), VECTOR_PERSIST_FILENAME,
                    )
            else:
                vec_path = os.path.join(persist_dir, VECTOR_PERSIST_FILENAME)
                # 2026-09-29（用户授权 ②）：**落盘前再核一次"近乎空"**。
                # 实测事故：磁盘上 79,245,357 字节的好索引在 20:32:16 被一份
                # **2 行 / 8,237 字节**的索引覆盖（另一进程/实例的失败重建
                # 走 `_save()` 落盘）⇒ 之后每次启动 `_load()` 判
                # `idx=2 vs pool=19354` 并丢弃 ⇒ 聚合器通道恒为 0。
                # 处置：池够大而落地率过低 ⇒ **不写这个文件**（保留磁盘上的旧索引）+ 响亮留痕。
                if (len(self._pool) >= MIN_ROWS_FOR_INDEX_RATIO
                        and len(self._index_id_map) < len(self._pool) * MIN_INDEX_LANDING_RATIO):
                    VECTOR_PERSIST_STATS["skipped_near_empty"] = \
                        VECTOR_PERSIST_STATS.get("skipped_near_empty", 0) + 1
                    logger.warning(
                        "**拒绝用近乎空的索引覆盖索引文件**：index=%d / pool=%d（< %.0f%%）"
                        "⇒ 保留磁盘上的旧索引（2026-09-29 ②，防 79MB 好索引被 2 行索引覆盖）",
                        len(self._index_id_map), len(self._pool), MIN_INDEX_LANDING_RATIO * 100)
                    return
                vec_tmp = f"{vec_path}.{os.getpid()}.tmp"
                # 2026-10-01（外部审计 · 修我自己引入的 mypy +4）：原为无注解的
                # `vec_data = {"dim": ..., "id_map": ...}` ⇒ mypy 把它推断成
                # `dict[str, int | list[str]]`，于是下面 `vec_data.get("dim")`/
                # `.get("id_map")` 的返回类型是 `object`，派生 4 个错误
                # （`attr-defined ×2` / `misc ×1` / `call-overload ×1`，
                #  与 `scripts/mypy_ratchet.py` 报的增长类别**逐一对上**）。
                # 显式注解成 `Dict[str, Any]` 即消（**不用 type: ignore 掩盖**）。
                vec_data: Dict[str, Any] = {
                    "dim": self._vector_dim,
                    "id_map": self._index_id_map,
                }
                if _HAS_FAISS:
                    import faiss
                    faiss.write_index(self._faiss_index, vec_tmp)
                    # §1270：faiss 对**非 ASCII 路径**的处理是未定义的 —— 实测两种形态都出现过：
                    #   ①（探针 `temp/_faiss_nonascii_probe_20260922.py`）**不报错、也不写文件**：
                    #     ASCII 目录 173 字节写成功；含中文的目录写完后**目录为空**、无异常
                    #     （同一路径 pickle 正常）；
                    #   ②（`scripts/ann_index_persist_rebuild.py` 内）抛
                    #     `RuntimeError: could not open … for writing`。
                    # ⇒ 防御要**同时**核异常与文件存在性：抛异常那种下面这行不执行（外层 except
                    # 会记一次含糊的 persist failed），**静默那种就靠这一行抓**；不核它，症状会跑到
                    # 下一行 `os.replace` 上（FileNotFoundError），真因（路径编码）就丢了。
                    if not os.path.exists(vec_tmp):
                        VECTOR_PERSIST_STATS["failed_no_file"] += 1
                        logger.warning(
                            "faiss.write_index produced no file (path=%s) — 路径含非 ASCII 字符时"
                            "faiss 会静默不写；向量索引未落盘（§1270）", vec_tmp,
                        )
                        raise RuntimeError("faiss.write_index wrote nothing: %s" % vec_tmp)
                    # 2026-09-30（外部审计 · 目标项 16）：**把 id_map 一起落盘**。
                    #
                    # 现场：faiss 分支此前**只写索引本身**（id_map 仅存在于非 faiss 的 NPZ 分支）
                    # ⇒ 加载时无法核对"索引第 p 行 == 池第 p 条"，只能拿池顺序硬套，
                    # 一旦计数不等就**整个丢弃**。实测每次启动都打印：
                    #     vector index row count mismatch (idx=19355 pool=19356) — discard, will rebuild
                    # **差 1 行**就让 ~84MB 的索引被丢掉、全量重建，而重建正是与查询争用
                    # 嵌入器的来源（项 16 的实质）。有 id_map 才能做**按位置的前缀校验**。
                    # 格式用 JSON（与第 8 轮修反序列化 RCE 的纪律一致：绝不用 pickle）。
                    try:
                        with open(vec_tmp + ".idmap.json", "w", encoding="utf-8") as _f:
                            json.dump(list(self._index_id_map), _f, ensure_ascii=False)
                    except Exception as _e:  # noqa: BLE001 — 旁车失败不影响索引主文件
                        logger.warning("id_map 旁车写入失败（不影响索引本身）：%s", str(_e)[:120])
                else:
                    # 2026-10-01（外部审计）：`_faiss_index` 的**静态类型**是 faiss 的
                    # `Index`（其 stub 无 `tolist`），而运行时这里是 numpy 支撑的索引对象。
                    # 原写法 `self._faiss_index.tolist()` 直接触发 `attr-defined`
                    # ⇒ 改为 `getattr` 守卫（**不用 type: ignore 掩盖**），
                    # 顺带对"索引类型没有 tolist"的情况给出可听见的降级：
                    # **只写 id_map 与空 vectors，而不是写出错误的向量**。
                    _tolist = getattr(self._faiss_index, "tolist", None)
                    if callable(_tolist):
                        vec_data["vectors"] = _tolist()
                    else:
                        vec_data["vectors"] = []
                        logger.warning(
                            "索引类型 %s 无 tolist() ⇒ 本次**不写向量**（只落 id_map）；"
                            "下次冷启动需重建该索引",
                            type(self._faiss_index).__name__)
                    # 2026-09-30（外部审计 · 反序列化 RCE）：原为 `pickle.dump(vec_data, f)`。
                    # pickle 在**读**时执行任意代码 ⇒ 持久化目录里被替换一个恶意
                    # `aggregator_vectors.pkl` 就等于任意代码执行（且该目录与池文件同处）。
                    # 改为 NPZ（numpy 是本包既有依赖），读取侧一律
                    # `np.load(..., allow_pickle=False)` ⇒ **只解析数据、绝不执行代码**。
                    # 传文件对象（而非路径）以免 numpy 自动追加 `.npz` 后缀。
                    with open(vec_tmp, "wb") as f:
                        np.savez_compressed(
                            f,
                            dim=np.asarray([int(vec_data.get("dim", 384) or 384)]),
                            id_map=np.asarray([str(x) for x in (vec_data.get("id_map") or [])]),
                            vectors=np.asarray(vec_data.get("vectors") or [], dtype=np.float32),
                        )
                _replace_with_retry(vec_tmp, vec_path)          # §1002: 同池文件口径
                # 2026-09-30（目标项 16）：id_map 旁车必须与主文件**同批次**替换，
                # 否则会出现"新索引 + 旧 id_map"的错配（比没有旁车更危险）。
                _im_tmp = vec_tmp + ".idmap.json"
                if os.path.exists(_im_tmp):
                    try:
                        _replace_with_retry(_im_tmp, vec_path + ".idmap.json")
                    except Exception as _e:  # noqa: BLE001 — 旁车失败不阻断主流程
                        logger.warning("id_map 旁车替换失败：%s", str(_e)[:120])
                VECTOR_PERSIST_STATS["written"] += 1

            # ── P0-2: RL 记忆决策状态持久化（2026-08-17）──────────
            # EpisodicRLScorer 奖励跨重启累积（此前只存内存，进程重启清零）。
            if self._rl_scorer is not None:
                try:
                    self._rl_scorer.save(os.path.join(persist_dir, "rl_state.json"))
                except Exception as _e:
                    swallow(__name__, _e)

            logger.debug("Aggregator pool persisted (%d memories)", len(self._pool))
        except Exception as exc:
            # 2026-10-02（外部复评 §3.3）：失败**必须可数**，不能只有一行 warning。
            POOL_PERSIST_STATS["failed"] += 1
            POOL_PERSIST_STATS["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            POOL_PERSIST_STATS["last_fail_ts"] = time.time()
            logger.warning("Aggregator persist failed (non-fatal): %s", exc)

    def _mark_dirty(self) -> None:
        """Schedule a debounced save after a write operation.

        Avoids excessive disk I/O by coalescing multiple writes into
        a single persist() call after PERSIST_DEBOUNCE_SECONDS of
        inactivity, or when PERSIST_MAX_DIRTY dirty writes accumulate.
        """
        self._dirty_count += 1

        if self._dirty_count >= PERSIST_MAX_DIRTY:
            # Force immediate save
            if self._persist_timer:
                self._persist_timer.cancel()
                self._persist_timer = None
            self._save()
            self._dirty_count = 0
            return

        # Reset debounce timer
        if self._persist_timer:
            self._persist_timer.cancel()

        self._persist_timer = threading.Timer(
            PERSIST_DEBOUNCE_SECONDS,
            self._flush_dirty,
        )
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _flush_dirty(self) -> None:
        """Timer callback: persist and reset dirty count."""
        with self._lock:
            if self._dirty_count > 0:
                self._save()
                self._dirty_count = 0
            self._persist_timer = None

    def _load(self) -> None:
        """Restore pool and vector index from disk."""
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            memories = data.get("memories", [])
            relations = data.get("relations", {})
            stats = data.get("stats", {})

            loaded = 0
            skipped_bad = 0
            for d in memories:
                # 2026-09-21 §1008：**单条坏行不得毒死整池**。原实现里任何一条
                # DimensionVector.from_dict 抛错都会冒泡到外层 except ⇒ 整个 71MB 池被隔离
                # （§1008 事故的另一条可能路径）。改为跳过并计数，日志留痕。
                try:
                    dv = DimensionVector.from_dict(d)
                except Exception as _e:  # noqa: BLE001
                    skipped_bad += 1
                    if skipped_bad <= 3:
                        logger.warning("pool load: skip malformed entry: %s", str(_e)[:140])
                    continue
                self._pool[dv.memory_id] = dv
                # Re-index into DimensionEngine for query support
                self._engine._vectors[dv.memory_id] = dv
                for agent in dv.source_agents:
                    self._add_to_agent_index(dv.memory_id, agent)
                self._add_to_topic_index(dv.memory_id, dv.topics)
                # Update engine stats
                self._engine._stats["total_indexed"] += 1
                loaded += 1

            self._relations_graph = {
                mid: dict(edges) for mid, edges in relations.items()
            }
            self._stats.update(stats)
            if skipped_bad:
                logger.warning("pool load: skipped %d malformed entries, kept %d",
                               skipped_bad, loaded)

            # ── P0-1: Restore vector index ──
            persist_dir = os.path.dirname(self._persist_path)
            vec_path = os.path.join(persist_dir, VECTOR_PERSIST_FILENAME)
            if os.path.exists(vec_path):
                try:
                    # 2026-08-17（记忆周期优化 P1-3）：VECTOR_PERSIST_FILENAME 曾被
                    # 不同 faiss 可用性的进程写成两种格式——有 faiss 时 faiss.write_index
                    # （原生二进制），无 faiss 时 pickle.dump（magic 0x80 开头）。
                    # 之前按 _HAS_FAISS 固定选一种读法，读到另一种格式即抛
                    # "Index type ... not recognized"→ 删文件 → 每次启动全量重建
                    # （数分钟 GIL 饥饿）。改为读文件头 8 字节探测格式，两种都兼容。
                    with open(vec_path, "rb") as _probe:
                        _magic = _probe.read(8)
                    # 2026-09-30（外部审计 · 反序列化 RCE）：格式探测扩为**三种**。
                    #   b"PK.." -> NPZ（新的安全容器；读取侧 allow_pickle=False）
                    #   0x80    -> **遗留 pickle**（只识别、**绝不反序列化**）
                    #   其它    -> faiss 原生二进制
                    _is_npz = _magic[:2] == b"PK"
                    _is_pickle = (not _is_npz) and len(_magic) > 0 and _magic[0] == 0x80  # 遗留 pickle magic
                    if not _is_npz and not _is_pickle and not _HAS_FAISS:
                        # 2026-09-23 §1287：**本进程没有 faiss，而文件是 faiss 格式**
                        # ⇒「读不了」不等于「文件坏了」。旧行为会掉进下面的 pickle 分支、
                        # 对 faiss 二进制 `pickle.load` 必抛、然后 `os.remove` ⇒ 有 faiss 的
                        # 进程刚写好的索引被无 faiss 的进程销毁（实测：75.4MB / 11 分钟内消失）。
                        # 这个文件是**别的**进程的正常产物 ⇒ 一个字节都不许动。
                        logger.warning(
                            "vector index is faiss-format but this process has no faiss — "
                            "skip; file untouched（不删/不动）: %s", vec_path)
                    elif _HAS_FAISS and not _is_npz and not _is_pickle:
                        import faiss
                        self._faiss_index = faiss.read_index(vec_path)
                        self._vector_dim = self._faiss_index.d
                        _pool_ids = list(self._pool.keys())
                        _ntotal = int(getattr(self._faiss_index, "ntotal", 0) or 0)
                        # 2026-09-30（外部审计 · 目标项 16）：**优先按落盘 id_map 做前缀校验**。
                        #
                        # 现场：每次启动都打印
                        #   vector index row count mismatch (idx=19355 pool=19356) — discard, will rebuild
                        # **差 1 行**就让 ~84MB 的索引被整个丢弃、全量重建 —— 而重建正是
                        # 与查询争用嵌入器的来源。根因是 faiss 分支**没有持久化 id_map**，
                        # 加载时无法核对"索引第 p 行 == 池第 p 条"，只能全有或全无。
                        #
                        # 现在写了 id_map 旁车（见写入侧），于是可以做**按位置的前缀校验**：
                        #   * 计数相等且 id 序列逐位相同        ⇒ 直接采用（旧行为）；
                        #   * 索引是池的**严格前缀**（idx <= pool 且逐位相同）⇒ **采用前缀**，
                        #     尾部那些池条目交给增量补齐路径（位置不变式对前 ntotal 行成立）；
                        #   * 其余（重排/删条/对不上）          ⇒ 仍然丢弃重建（保守，绝不猜）。
                        # ⚠️ 2026-09-30（外部审计 · **撤回我自己的放宽**）：
                        #
                        # 我此前把这里放宽为「索引是池的**严格前缀**就采用前缀」，动机是
                        # 每次启动都打印 `vector index row count mismatch (idx=N pool=N+1)`
                        # ⇒ 差 1 行就丢弃 ~84MB 索引并全量重建，而重建会与查询争用嵌入器。
                        #
                        # **但那一放宽削掉了一道安全校验**，被既有判据当场抓住：
                        #   `tests/unit/test_vector_index_load_alignment.py::
                        #    test_池多一条时不许信任旧索引`
                        #   「索引滞后（池 3 条 / 索引 2 行）：**旧行为会把池第 2 条当成
                        #    索引第 2 行的名字**」⇒ 要求 `_faiss_index is None`。
                        # 该判据描述的是 **id_map 旁车存在之前**的契约，且它要守的不变式是
                        # 「**索引第 p 行 == 池第 p 条**，且**计数相等**」。**安全优先**：
                        # 这里恢复"计数必须相等"的严格校验。
                        #
                        # id_map 旁车**保留**（它是真改进：让"逐位核对"成为可能、
                        # 也让不匹配时能给出可诊断的原因），但**只用于核对，不用于放宽**。
                        # 若要恢复"采用前缀"的性能优化，须先走**契约变更**（改这条判据）
                        # 并取得所有者点头 —— 与 §34.2 同一条纪律。
                        _im_path = vec_path + ".idmap.json"
                        _saved_ids = None
                        if os.path.exists(_im_path):
                            try:
                                with open(_im_path, encoding="utf-8") as _f:
                                    _saved_ids = [str(x) for x in json.load(_f)]
                            except Exception as _e:  # noqa: BLE001 — 旁车坏了就退回旧路径
                                logger.warning("id_map 旁车不可用（%s）— 退回按池顺序判定",
                                               str(_e)[:100])
                                _saved_ids = None
                        _accepted_prefix = False
                        if (_saved_ids is not None
                                and _ntotal == len(_pool_ids)      # **计数必须相等**
                                and _saved_ids == _pool_ids):      # 且逐位相同
                            self._index_id_map = list(_pool_ids)
                            _accepted_prefix = True
                        elif _saved_ids is not None and _saved_ids != _pool_ids[:_ntotal]:
                            logger.warning(
                                "id_map 旁车与池顺序不一致 ⇒ 丢弃索引重建"
                                "（saved=%d pool=%d）", len(_saved_ids), len(_pool_ids))
                        if _accepted_prefix:
                            pass                      # 前缀校验通过：_index_id_map 已在上面设好
                        elif _ntotal != len(_pool_ids):
                            # 2026-09-23 §1276：**行数不等且无法逐位核对 ⇒ 不许把池顺序当 id_map**。
                            # 不变式（_maintenance.py §1268 自述）：索引第 p 行 == 池第 p 条。
                            # 池删过条（merge/dedup）或重排过而索引没跟上时，旧实现会把第 p 行
                            # 映射到**另一条记忆** ⇒ 检索静默返回错误记忆（比“空结果”坏得多）。
                            # 丢弃 ⇒ 交给重建路径（§1275 之后重建不再堵读者）。
                            logger.warning(
                                "vector index row count mismatch (idx=%d pool=%d%s) — discard, will rebuild",
                                _ntotal, len(_pool_ids),
                                "" if _saved_ids is None else " and id_map does not match prefix")
                            self._faiss_index = None
                            self._index_id_map = []
                        else:
                            # 计数相等：无旁车时沿用旧行为（按池顺序当 id_map）；
                            # 有旁车且已逐位校验通过时也落在这里（_accepted_prefix 已置 True）。
                            self._index_id_map = _pool_ids
                    elif _is_pickle:
                        # 2026-09-30（外部审计 · 反序列化 RCE）：**拒绝反序列化**。
                        # pickle 在加载时执行任意代码 —— 只要有人把恶意 .pkl 放进持久化
                        # 目录，进程启动即被拿下。这里只识别格式、不加载内容，
                        # 直接交给重建路径（本机实测该文件**根本不存在**，代价为零）。
                        # 文件**一个字节都不动**（保持与 §1287「读不了 ≠ 文件坏了」同款纪律）。
                        logger.warning(
                            "legacy pickle vector index REFUSED (unsafe deserialization) — "
                            "rebuild instead; file left untouched: %s", vec_path)
                    else:
                        # NPZ 安全容器：allow_pickle=False ⇒ 只解析数据，不执行代码
                        with open(vec_path, "rb") as f:
                            _npz = np.load(f, allow_pickle=False)
                            vec_data = {
                                "dim": int(_npz["dim"][0]) if "dim" in _npz else 384,
                                "id_map": ([str(x) for x in _npz["id_map"]]
                                           if "id_map" in _npz else []),
                                "vectors": (_npz["vectors"].tolist()
                                            if "vectors" in _npz else []),
                            }
                        self._vector_dim = vec_data.get("dim", 384)
                        self._index_id_map = vec_data.get("id_map", [])
                        vectors = vec_data.get("vectors", [])
                        # 2026-09-23 §1276：pickle 格式**自带 id_map** ⇒ 直接校验对齐
                        # （比 faiss 分支多一层：这里能验顺序，不只是行数）。
                        if list(self._index_id_map) != list(self._pool.keys()) or \
                                (vectors and len(vectors) != len(self._index_id_map)):
                            logger.warning(
                                "pickle vector index misaligned with pool (idx=%d pool=%d) — discard, will rebuild",
                                len(self._index_id_map), len(self._pool))
                            self._faiss_index = None
                            self._index_id_map = []
                            vectors = []
                        if vectors:
                            if _HAS_FAISS:
                                import faiss
                                _faiss_idx = faiss.IndexFlatIP(self._vector_dim)
                                import numpy as np  # noqa: E402  EXECUTION 519 lazy
                                _faiss_idx.add(np.ascontiguousarray(np.array(vectors, dtype=np.float32)))
                                self._faiss_index = _faiss_idx
                            else:
                                import numpy as np  # noqa: E402  EXECUTION 519 lazy
                                self._faiss_index = np.array(vectors, dtype=np.float32)
                except Exception as exc:
                    # 2026-09-23 §1287：双格式探测后仍失败（**真的**损坏/截断）⇒ **隔离改名**，
                    # 不删除。理由：这条路径曾被「本进程没有 faiss / 内存分配失败 / 瞬时占用」
                    # 这类**与被读文件质量无关**的原因触发，一次就销毁 50–95 分钟的重建产物
                    # （实测 75.4MB 索引 11 分钟内消失、无备份）。原路径仍被腾空 ⇒ 自愈不受影响。
                    logger.warning(
                        "Vector index load failed (%s): %s — quarantine to rebuild（隔离，不删除）",
                        "faiss" if _HAS_FAISS else "pickle", exc,
                    )
                    _quarantine_unreadable(vec_path)

            # ── P0-2: RL 记忆决策状态恢复（2026-08-17）────────────
            # 与 _save 对称：进程重启后恢复 Q 值/命中统计，避免学完即忘。
            try:
                rl_path = os.path.join(persist_dir, "rl_state.json")
                if os.path.exists(rl_path):
                    from trinity.modules.second_brain.episodic_rl import EpisodicRLScorer
                    self._rl_scorer = EpisodicRLScorer.load(rl_path)
                # 2026-08-17（P2）：无论是否从文件恢复，启动即落盘一次，
                # 确保 rl_state.json 存在（空状态也可追溯），
                # 避免"无 RL 反馈就一直不落盘"。
                if self._rl_scorer is not None:
                    self._rl_scorer.save(rl_path)
            except Exception as _e:
                swallow(__name__, _e)

            logger.info(
                "Aggregator pool restored from disk: %d memories, %d relations",
                loaded, len(self._relations_graph),
            )
        except Exception as exc:
            # 2026-09-21 §1008：**只在文件真的不完整时才隔离**。
            # 实测事故：一个完全有效的 71.6MB / 19,274 条池文件被隔离，服务以 48 条空池
            # 跑了 8 小时；失败其实发生在**装载过程内部**（内存压力/单条坏行），与文件无关。
            # 判据：_pool_file_looks_complete()（首 { 末 }，O(1) 内存）。
            if self._persist_path and os.path.exists(self._persist_path) \
                    and _pool_file_looks_complete(self._persist_path):
                logger.error(
                    "Aggregator load failed but the pool FILE IS INTACT (%s) — NOT quarantining "
                    "(failure is inside the loader, not the file). Retry after restart: %s",
                    self._persist_path, exc)
            else:
                # 自愈：真损坏/截断的池文件备份后以空池启动，避免覆盖现场证据
                try:
                    if self._persist_path and os.path.exists(self._persist_path):
                        backup = f"{self._persist_path}.corrupt_{int(time.time())}"
                        os.replace(self._persist_path, backup)
                        logger.warning("Aggregator pool corrupted; backed up to %s", backup)
                except Exception as _e:
                    swallow(__name__, _e)
                logger.warning("Aggregator load failed (starting fresh): %s", exc)


from ._init import _InitMixin
from ._ingest import _IngestMixin
from ._search import _SearchMixin
from ._vector import _VectorMixin
from ._rl import _RLMixin
from ._graph import _GraphMixin
from ._stats import _StatsMixin
from ._maintenance import _MaintenanceMixin
from ._similarity import _SimilarityMixin
from ._diagnostics import _DiagnosticsMixin
from ._kgraph_adapter import _AggregatorKGraphAdapter


class MemoryAggregator(_InitMixin, _PersistMixin, _IngestMixin, _SearchMixin, _VectorMixin, _RLMixin, _GraphMixin, _StatsMixin, _MaintenanceMixin, _SimilarityMixin, _DiagnosticsMixin):
    """Shared cross-agent memory pool with dimension-aware indexing.

    Replaces per-agent isolated storage with a single shared pool.
    Uses DimensionEngine internally for topic/scope/category indexing.
    Supports similarity-based dedup merging, relationship graph
    traversal, and automatic expiration.

    Usage:
        # EXECUTION 524 (C2p-III): 模块级 agg 改为懒实例化（PEP 562；import trinity 不再构造 engine bridge）
_agg = None


def _get_agg():
    global _agg
    if _agg is None:
        _agg = MemoryAggregator()
    return _agg


def __getattr__(name):
    if name == "agg":
        return _get_agg()
    raise AttributeError(name)
        dv = agg.ingest("user prefers dark mode", "main",
                        {"category": "preference", "scope": "global"})
        results = agg.query({"category": "preference"})
        related = agg.get_related(dv.memory_id, depth=2)
    """
    pass


from ._factory import create_aggregator, self_test

__all__ = ["MemoryAggregator", "create_aggregator", "self_test", "_AggregatorKGraphAdapter"]
