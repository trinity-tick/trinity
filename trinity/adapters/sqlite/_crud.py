"""SQLite adapter - memory CRUD & lifecycle mixin (split from sqlite.py, 2026-08-17).

Part of the SQLiteAdapter package decomposition. Behavior identical to the
pre-split single-file implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict  # t61/I1：token 集缓存的 LRU（默认 off）
import re
import sqlite3
import functools
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from trinity.layer_inference import resolve_memory_layer  # 2026-09-30 目标项 13：memory_layer 兜底

from ...security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]
from .._util import _safe_write
from ..._tags import normalize_tags  # EXECUTION 771：tags 归一（防同源双重编码）

from ._base import _SQLiteMixinBase
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

logger = logging.getLogger("trinity.adapters.sqlite")

# ── 2026-10-06（复评 G4）：写入侧 `agent_id` 空值归一化 ────────────────────────
# 实测：生产库存在 `agent_id = ''` 的行（读侧统计曾因此产出**不可消费的空键**，
# 见 F8/G3 的 `agent_distribution` / `modalities` —— PowerShell 5.1 的
# ConvertFrom-Json 会为"空名属性"报「参数 name 值无效」）。
# `agent_id: str = "default"` 这个默认值只在**省略实参**时生效；调用方显式传
# `''` 或纯空白时**不被拦截** ⇒ 脏值直接落库，且写入侧此前**完全无留痕**。
# 这里在**写入汇聚点**补归一化（与 `resolve_memory_layer` 同一位置、同一思路）。
_AGENT_ID_WARN_LOCK = threading.Lock()
_agent_id_dirty_warned = False
agent_id_normalised_total = 0


def _normalise_agent_id(agent_id: Optional[str]) -> Optional[str]:
    """把空串/纯空白的 ``agent_id`` 归一化为 ``"default"``。

    口径：``''`` / 纯空白 **≡ 未提供**（空串不携带归属信息，不代表某个 agent）
    ⇒ 归一化为本适配器的默认 namespace，与**省略该实参**的行为一致。
    ``None`` **不在这里处理**：显式 NULL 由读侧归一化，且调用方可能是刻意为之。

    留痕：首次触发发一次 WARNING（避免刷屏），并累计
    ``agent_id_normalised_total``，供运维/门禁读到"写入侧仍在产生脏值"。
    """
    global _agent_id_dirty_warned, agent_id_normalised_total
    if not isinstance(agent_id, str) or agent_id.strip():
        return agent_id
    agent_id_normalised_total += 1
    if not _agent_id_dirty_warned:
        with _AGENT_ID_WARN_LOCK:
            if not _agent_id_dirty_warned:
                _agent_id_dirty_warned = True
                logger.warning(
                    "AGENT-ID-EMPTY-NORMALISED: 收到空/纯空白的 agent_id，"
                    "已按「未提供」归一化为 'default'"
                    "（写入侧此前不拦脏值，读侧统计会因此产出不可消费的空键）")
    return "default"


# 2026-08-18（conflict 检测改进，agent-memory-bench 对齐）：
# 写入时相似性冲突检测的相似度阈值（search_memories 的 FTS5 归一化 score）。
# 可经 TRINITY_CONFLICT_SIM_THRESHOLD 覆盖；TRINITY_CONFLICT_DETECT=off 可关闭。
CONFLICT_SIM_THRESHOLD = 0.5  # 保留（兼容）；实际用 token 重叠
CONFLICT_TOKEN_OVERLAP = 0.6  # jieba token 集合重叠率阈值（conflict 检测）

#: ── t61/I1：冲突检测的 **token 集缓存**（**默认 off**，可用环境变量回滚）──────────────
#: 依据（t61 实测）：冲突检测成本里有一块**与表规模无关的固定成本**（0 行时 on 9.02ms vs off
#: 1.30ms ⇒ 差 7.72ms，jieba 分词主导），另一块才随表规模**线性**增长（召回里的按词 LIKE 全表扫描）。
#: 本缓存**只动第一块**：`_token_set` 是**纯函数（text → frozenset）**，同一文本二次分词必然得到
#: 同一集合 ⇒ 缓存**不可能改变**任何重叠判定（判据①以"同输入 ⇒ 同冲突组/同合并判定"为证）。
#: ⚠️ **默认 off**：`TRINITY_CONFLICT_TOKEN_CACHE=on` 才启用；回滚 = 不设/置 off（**不需改代码**）。
#: ⚠️ **不动第二块**：把 O(n) 的 LIKE 召回改成"只走 FTS / 给补齐加 LIMIT / 挪到后台"都会**改变
#: 候选集**（= 改语义）⇒ 按任务书硬约束**停下来上报**，见 `WRITE-PATH-CONFLICT-COST.md` §5。
_TOKEN_CACHE: "OrderedDict[str, frozenset]" = OrderedDict()
_TOKEN_CACHE_STATS = {"hits": 0, "misses": 0, "evictions": 0}


def _token_cache_enabled() -> bool:
    return str(os.environ.get("TRINITY_CONFLICT_TOKEN_CACHE", "off")).strip().lower() in (
        "on", "1", "true", "yes")


def _token_cache_max() -> int:
    try:
        return max(0, int(os.environ.get("TRINITY_CONFLICT_TOKEN_CACHE_MAX", "1024")))
    except Exception:  # noqa: BLE001 — 配置写坏 ⇒ 退回不缓存（行为不变）
        return 0


#: ── t115/G7C（D-12 **C 案**）：把冲突判定**挪出写路径**（⭐ **默认 off**，`on` 为目标行为）──
#: 依据（t110 实测）：写路径 p50 **62.3ms**，其中 **≈62.0ms** 是 `_assign_conflicts` 的
#: 巨型 OR FTS 召回（`_search_fts`，@N=12000 保真语料）。
#: ⚠️ **A 案已证伪**（把 OR 词条按 df 收窄 ⇒ 成本 0.285ms，召回 **仅多找到 2 条**
#: —— 真零召回基线是 **0/12**，见 G7C §3.3 的口径更正）⇒ 相对 12/12 仍是**不可接受的召回损失**
#: ⇒ **只剩"把判定挪出写路径"这一条，既不砍候选源、也不改判定逻辑。**
#:
#: **语义变更（必须显式登记，本文件即登记处）**：
#:   · 写入返回时机 **不变**（行照样 INSERT + commit，照样立刻可检索）；
#:   · ⭐ **冲突标记 `conflict_group_id` 的可见时点延后** —— 写入返回时尚未写入，
#:     由后台线程在**可见性窗口内**补上（申报窗口见 G7C-D12-CONFLICT-ASYNC.md §1.3）。
#: ⚠️ **只改"何时算"，不改"怎么算"**：`_assign_conflicts` 本体**一字未动**。
#:
#: ⭐⭐ **为什么默认是 `off`（队长 2026-10-08 裁定）**：
#:   本仓 D-13 的规矩是「**默认变更必须有实测理由**」。本处实测证明的是
#:   **"该能力有效"**（写路径 −96.5%、召回 12/12 不退），**不是"默认值应当为 on"** ——
#:   ⚠️ **未测⑤（下游是否有消费者要求"写入返回即可见"）未核**，
#:   而**默认开的代价是未知的下游时序依赖**，**保守上线（off）的代价接近零**（回滚是逐字的）
#:   ⇒ **两件事不能混**：**能力已实现并实测有效；默认值 = `off`；待未测⑤核实后再切 `on`。**
#: ⚠️ 另一条已登记缺陷：**崩溃窗口内静默丢判定**（内存队列，≈119ms 窗口；可补算但**无自动补偿**）
#:   —— 见 G7C §4-B2/B3。**这也是默认设 `off` 的佐证之一**（默认开 ⇒ 把这个窗口铺到全量写入）。
#: 开启：`TRINITY_CONFLICT_ASYNC=on`；回滚：置 `off`（或删掉该变量）⇒ **逐字回到同步行为**。
_CONFLICT_ASYNC_DEFAULT = "off"
_CONFLICT_ASYNC_QUEUE_MAX = 4096
_CONFLICT_ASYNC: Dict[str, Any] = {
    "queue": [],          # list[tuple[adapter, memory_id, content]]
    "lock": threading.Lock(),
    "wake": threading.Event(),
    "thread": None,
    "started": False,
    # 计数器：**让"丢"变成可读的数字，而不是静默**（G7C §5 边界登记）
    "queued": 0,
    "processed": 0,
    "failed": 0,
    "dropped": 0,
}


def _conflict_async_enabled() -> bool:
    """`TRINITY_CONFLICT_ASYNC`（⭐ **默认 off**）。置 `on` ⇒ 启用（目标行为）；`off` ⇒ 逐字同步。"""
    return str(os.environ.get("TRINITY_CONFLICT_ASYNC", _CONFLICT_ASYNC_DEFAULT)
               ).strip().lower() in ("on", "1", "true", "yes")


def _conflict_async_delay_s() -> float:
    try:
        return max(0.0, int(os.environ.get("TRINITY_CONFLICT_ASYNC_DELAY_MS", "50"))) / 1000.0
    except Exception:  # noqa: BLE001 — 配置写坏 ⇒ 不延迟（仍异步，行为不变）
        return 0.0


#: ── t136/G10R4：**把 G7C 的崩溃窗口接线**（补偿 = 把「静默丢失」变成「重启后可发现」）──────
#: 来源（G7C §4-B2/B3 实测）：`TRINITY_CONFLICT_ASYNC=on` 时队列**只在内存** ⇒ 进程在
#: 「写入已返回、判定尚未执行」的 **≈119ms 窗口**内结束 ⇒ 判定**无声消失**，
#: **仓里没有自动补偿任务** ⇒ 当时如实记为「**能力存在 ≠ 已接线**」。
#:
#: ⭐⭐ **本补偿只做一件事**：在 `connect()` 后扫一遍
#:    `status='active' AND conflict_group_id IS NULL` 的行，**重跑 `_assign_conflicts`**
#:    ⇒ **把「静默丢失」变成「重启后 3 步可查/可修」**：
#:       ① 一个 SQL 就能查出「有多少行没组」；
#:       ② 补偿跑一次就把它们补上（判据①）；
#:       ③ 不再需要人手动重算（G7C-B5 当时只能手工证明"能重算"）。
#:
#: ⚠️⚠️ **它【不消除窗口】**：补偿发生在**下一个进程启动之后**，
#:    「窗口内这一段判定丢失」这件事**依然发生**；本补偿只是让损失**可被度量、可被修复**。
#:    ⇒ 准确表述：**「把静默变成可发现（可修）」，不是「消除窗口」。**（不许含糊）
#:
#: ⚠️ **与正常写入竞争**：补偿与写路径共用 `_assign_conflicts`（同一把 `_write_lock` + 同一 DB）
#:    ⇒ **会竞争**。取舍见报告 §3：① 默认 **off**；② `BATCH_N` 限流（**永不一次全表扫**）；
#:    ③ 「建库后迄今为 NULL 的行」**只需一次收敛**（NULL 行不会因新写入而变多）；
#:    ④ 若要零竞争 ⇒ 由运维在低峰跑 `scripts/g10r4_conflict_compensate.py`（本任务提供）。
#:
#: ⛔ **默认 `off`**（沿用 G7C 裁定二的理由）：**"新默认值必须有实测理由"** ——
#:   本处实测证明的是「补偿有效」，**不是「启动时该自动跑」**；且生产**当前默认走同步路径（窗口不存在）**。
#: 开启：`TRINITY_CONFLICT_COMPENSATE=on`（同族开关，与 `TRINITY_CONFLICT_ASYNC` 并列）。
_CONFLICT_COMPENSATE_DEFAULT = "off"
_CONFLICT_COMPENSATE_STATS: Dict[str, Any] = {
    "runs": 0, "scanned": 0, "changed": 0, "no_change": 0, "errors": 0,
    "last_run_ts": None, "last_run_ms": None, "budget_exhausted": 0,
}


def _conflict_compensate_enabled() -> bool:
    """`TRINITY_CONFLICT_COMPENSATE`（⭐ **默认 off**）。`on` ⇒ 启动后按需扫一遍补偿。"""
    return str(os.environ.get("TRINITY_CONFLICT_COMPENSATE", _CONFLICT_COMPENSATE_DEFAULT)
               ).strip().lower() in ("on", "1", "true", "yes")


def _conflict_compensate_batch() -> int:
    """单次补偿**最多扫多少行**（`TRINITY_CONFLICT_COMPENSATE_BATCH`，默认 **50**）。

    ⚠️ **限流的意义**：**别让"启动"变成一次全表扫描**（生产 active 29,087 行、
    其中 16,167 行 NULL；全扫 16,167 × **≈37–43 ms/行** ≈ **10 分钟**，绝不可在启动做）。
    ⭐ **默认 50 是实测选的**（t136 实测，临时库 N=12000、保真语料）：

    | batch | 实测 p50 | ms/行 |
    |---|---|---|
    | **50** | **1,849.5 ms** | 36.99 |
    | 200 | 7,497.3 ms | 37.49 |
    | 1000 | 42,573.8 ms | 42.57 |

    ⇒ 成本**近似线性**（≈37–43 ms/行）；**50 行 ≈ 1.85 s**（可接受的启动代价），
    **200 行 ≈ 7.5 s（太慢）**。
    ⚠️⚠️ **诚实说明：本入口是【有界阻塞】，不是"零成本后台"** —— 它在调用它的线程上跑完这 50 行。
    若要真正不阻塞 ⇒ 由调用方放到后台线程，或用
    `scripts/g10r4_conflict_compensate.py`（运维在低峰跑）。
    `0` = 不限流（⚠️ 危险：等同于全表补偿，仅供一次性脚本显式指定）。
    """
    try:
        return max(0, int(os.environ.get("TRINITY_CONFLICT_COMPENSATE_BATCH", "50")))
    except Exception:  # noqa: BLE001 — 配置写坏 ⇒ 退回默认限流（更安全）
        return 50


def conflict_compensate_stats() -> Dict[str, Any]:
    """补偿的读数（**让"扫了多少/改了多少"可见**，不再是黑盒）。

    ⭐ 本函数同时是**补偿的惰性触发点**：当 `TRINITY_CONFLICT_COMPENSATE=on` 时，
    **第一次**被调用（由运维/健康检查/启动自检调用即可）就会触发**一批**补偿
    （`TRINITY_CONFLICT_COMPENSATE_BATCH`，默认 200）⇒ **不阻塞启动、不阻塞写路径**。

    ⚠️ **为什么不用 `connect()` 挂钩**：`connect()` 在 `_connection.py`，**不在本任务写域内**
    （任务书写域 = `_crud.py` + `scripts/**`）⇒ 把触发点放在**本写域内**的一个公开只读函数上，
    并由 `scripts/g10r4_conflict_compensate.py` 与判据显式调用 ⇒ **不越域、也不搞隐形副作用**。
    """
    lazy = _maybe_lazy_compensate()
    out = {"enabled": _conflict_compensate_enabled(),
           "batch": _conflict_compensate_batch(), **dict(_CONFLICT_COMPENSATE_STATS)}
    if lazy is not None:
        out["lazy_run"] = lazy
    return out


def _maybe_lazy_compensate() -> Optional[Dict[str, Any]]:
    """惰性补偿：**每进程只试一次**，且**只在开关 on 时**；失败静默（不影响读诊断）。

    ⚠️ 用**独立的一次性锁**保护，避免并发诊断调用打出多批补偿。
    """
    global _COMPENSATE_LAZY_DONE
    if not _conflict_compensate_enabled() or _COMPENSATE_LAZY_DONE:
        return None
    with _COMPENSATE_LAZY_LOCK:
        if _COMPENSATE_LAZY_DONE:
            return None
        _COMPENSATE_LAZY_DONE = True
    try:
        ad = getattr(_COMPENSATE_LAZY_OWNER[0], "_compensate_owner", None)
        if ad is None:
            return None
        return ad.compensate_missing_conflicts()
    except Exception as exc:  # noqa: BLE001 — 惰性补偿失败**不得**影响诊断读取
        swallow(__name__, exc)
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


#: 惰性补偿的**进程级一次性**状态（与上面 `conflict_compensate_stats()` 配套）
_COMPENSATE_LAZY_DONE = False
_COMPENSATE_LAZY_LOCK = threading.Lock()
#: 惰性补偿要操作哪个适配器：由**任一**适配器首次调用 `register_compensate_owner()` 时登记
_COMPENSATE_LAZY_OWNER: List[Any] = [None]


def register_compensate_owner(adapter: Any) -> None:
    """登记「惰性补偿用哪个适配器实例」（进程内第一个登记的生效）。"""
    if _COMPENSATE_LAZY_OWNER[0] is None:
        _COMPENSATE_LAZY_OWNER[0] = adapter


def conflict_async_stats() -> Dict[str, Any]:
    """⭐ 冲突判定的异步队列读数（运维/判据用）：**不丢是可见的，丢也是可见的**。"""
    st = _CONFLICT_ASYNC
    with st["lock"]:
        pend = len(st["queue"])
    return {"enabled": _conflict_async_enabled(), "pending": pend,
            "queued": st["queued"], "processed": st["processed"],
            "failed": st["failed"], "dropped": st["dropped"],
            "thread_alive": bool(st["thread"] is not None and st["thread"].is_alive()),
            "queue_max": _CONFLICT_ASYNC_QUEUE_MAX,
            "delay_ms": int(_conflict_async_delay_s() * 1000)}


def _conflict_async_drain(limit: int = 64) -> List[Tuple[Any, str, str]]:
    with _CONFLICT_ASYNC["lock"]:
        q = _CONFLICT_ASYNC["queue"]
        out = q[:limit]
        del q[:len(out)]
    return out


def _conflict_async_loop() -> None:
    """后台 daemon 线程：**循环把队列里的判定补齐**（可见性窗口内）。"""
    st = _CONFLICT_ASYNC
    while True:
        try:
            item = None
            with st["lock"]:
                if st["queue"]:
                    item = st["queue"].pop(0)
            if item is None:
                st["wake"].wait(0.25)
                st["wake"].clear()
                continue
            delay = _conflict_async_delay_s()
            if delay:
                # 给"写入事务提交"留出窗口，避免后台读到未提交的行
                st["wake"].wait(delay)
                st["wake"].clear()
            adapter, mid, content = item
            try:
                adapter._assign_conflicts(mid, content)
                with st["lock"]:
                    st["processed"] += 1
            except Exception as exc:  # noqa: BLE001 — 单条失败不得终止工作线程
                swallow(__name__, exc)
                with st["lock"]:
                    st["failed"] += 1
        except Exception as exc:  # noqa: BLE001 — 线程自身绝不能死
            swallow(__name__, exc)
            time.sleep(0.05)


def _conflict_async_start() -> None:
    global _CONFLICT_ASYNC_THREAD
    st = _CONFLICT_ASYNC
    if st["started"]:
        return
    with st["lock"]:
        if st["started"]:
            return
        t = threading.Thread(target=_conflict_async_loop, name="trinity-conflict-async",
                             daemon=True)
        t.start()
        st["thread"] = t
        st["started"] = True
    _CONFLICT_ASYNC_THREAD = t


_CONFLICT_ASYNC_THREAD: Optional[threading.Thread] = None


def _conflict_async_submit(adapter: Any, memory_id: str, content: str) -> bool:
    """把一次冲突判定**入队**（**不阻塞写路径**）。返回 True=已入队（异步），False=被丢弃。

    ⚠️ 队列上界 `_CONFLICT_ASYNC_QUEUE_MAX` 满了 ⇒ **丢弃并计数**（`dropped`）：
    **不静默**（`conflict_async_stats()` 读得到），且该判定**可从库里重算**（见 §5 边界）。
    """
    _conflict_async_start()
    st = _CONFLICT_ASYNC
    with st["lock"]:
        if len(st["queue"]) >= _CONFLICT_ASYNC_QUEUE_MAX:
            st["dropped"] += 1
            return False
        st["queue"].append((adapter, memory_id, content))
        st["queued"] += 1
    st["wake"].set()
    return True


class _CrudMixin(_SQLiteMixinBase):
    @_safe_write
    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        agent_id: str = "default",
        app_id: Optional[str] = None,
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        memory_layer: Optional[str] = None,
        auto_redact_pii: bool = False,
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """写入一条记忆。

        Args:
            status: 2026 优化轮 B6 —— **调用方请求的落库状态**。传 `"archived"`
                时该行以 `status='archived'` 与记忆行**同一条 INSERT** 落库
                （= 同一次 commit）。语义与 PG 批量通道 `ingest_batch` 的
                `rec["status"]` 一致（该机制此前只在 PG 批量路径存在）。
                只允许**下调**（active → archived）：绝不会把行改回 active。
                动机：client 层"先写 active、再 `archive_memories`"是两次独立
                commit，实测存在「已提交但未隔离」窗口（归档失败时压测/评测/
                隔离内容静默留在 active 检索面，且审计一并跳过）。
        """
        # 2026-08-24（R9 P0-1）：只读模式（建表/迁移写锁失败降级）——
        # 写操作明确报错，而不是静默失败或抛 database is locked 裸异常。
        if getattr(self, "_readonly_mode", False):
            return {"memory_id": "", "error": "readonly mode (schema init failed due to write lock)"}

        # 2026-09-30（外部审计 · 目标项 13）：**`memory_layer` 兜底**。
        #
        # 实测：active 行 24,863 条中 20,324 条（81.7%）`memory_layer IS NULL`，
        # 且 `trinity/` 内**没有任何生产路径**会填这一列（唯一写者是一次性维护脚本）。
        # 于是层过滤、层分布统计、按层治理长期建立在 81.7% 为空的列上。
        #
        # 这里在**写入汇聚点**补默认值：调用方未显式给就用确定性推断。
        # 显式传入的值**永不被覆盖**（`resolve_memory_layer` 的第一条分支）。
        memory_layer = resolve_memory_layer(
            memory_layer, category=category, content=content, metadata=metadata)

        # 2026-10-06（复评 G4）：**agent_id 空值归一化**（同属写入汇聚点）。
        agent_id = _normalise_agent_id(agent_id)

        with self._write_lock:

            conn = self._conn
            if not conn:
                raise RuntimeError("Not connected. Call connect() first.")

            memory_id = f"mem_{uuid.uuid4().hex[:16]}"
            version_id = f"ver_{uuid.uuid4().hex[:12]}"
            if not session_id:
                session_id = f"sess_{uuid.uuid4().hex[:12]}"
            tags_json = json.dumps(normalize_tags(tags))  # EXECUTION 771
            now = datetime.now(timezone.utc).isoformat()

            # ── t48/G7：适配器写入边界的 **PII 守卫**（下沉到本层）────────────────
            # 与下面的注入守卫（`trinity/security/injection.py::adapter_write_guard`）**同构**，
            # 理由是同一个：daemon / memory 抽取与巩固 / brain / evolution / vms / pipeline
            # 等路径**直写本方法**、不经过 client 层 ⇒ 只挂 client.ingest 时侧门敞开。
            # 策略与开关**全部复用 G2**（`trinity.security.sensitive`），本处只做转接。
            # 回滚：`TRINITY_SENSITIVE_REDACT=0`（G2 同一开关）；本边界另有 `TRINITY_ADAPTER_GUARD=0`。
            try:
                from .._pii_guard import adapter_pii_guard

                content, metadata, _pii_g = adapter_pii_guard(content, metadata)
                if _pii_g.get("refuse"):
                    # high 档：**不掩码、不落库**（与客户端层同语义）
                    return {"memory_id": "",
                            "error": "sensitive-high refused (adapter PII guard)",
                            "severity": _pii_g.get("severity"), "policy": _pii_g.get("policy")}
                if _pii_g.get("isolate"):
                    # quarantine ⇒ 与注入守卫同款：同一条 INSERT 落 status='archived'
                    status = "archived"
            except Exception as _e:  # noqa: BLE001 — 守卫尽力而为，绝不阻断写入
                swallow(__name__, _e)

            # ── PII 检测与脱敏 ──────────────────────────────────────────
            pii_info = None
            stored_content = content
            if auto_redact_pii:
                result = self._detect_pii(content)
                stored_content = result["redacted"]
                pii_found = {k: v for k, v in result["found"].items() if v}
                if pii_found:
                    pii_info = pii_found

            sha256_hash = self._compute_sha256(stored_content)
            # ── t44/G3：把**别的层真的做过的掩码**也纳入同一个账本 ────────────────
            # 写入路径上真正掩码的是 **client 层**（`_ingestion.py`，G2/t43）——它把审计记录
            # 放进 `metadata["pii_redaction"]` 一起传进来。适配器此前**只认自己的**
            # `auto_redact_pii`（默认 False）⇒ 响应 `auto_redacted=False` / `pii_redacted_types=[]`
            # 与"入库内容确实已掩码"**反向** ⇒ 按响应字段审计会得出"没脱敏"的错误结论。
            # 处置：**只如实转述两层账本**（谁掩的、掩了哪些类别），**不重新检测**、**不恒真**。
            _ing = (metadata or {}).get("pii_redaction") if isinstance(metadata, dict) else None
            _ing_kinds = ([str(k) for k in (_ing.get("kinds") or [])]
                          if isinstance(_ing, dict) else [])
            _ada_kinds = list(pii_info.keys()) if pii_info else []
            _ing_layer = (str((_ing or {}).get("layer") or "ingestion")
                          if isinstance(_ing, dict) else "ingestion")
            redaction_kinds = _ada_kinds + [k for k in _ing_kinds if k not in _ada_kinds]
            redaction_source = ("both" if _ada_kinds and _ing_kinds else
                                "adapter" if _ada_kinds else
                                _ing_layer if _ing_kinds else None)
            redacted_any = bool(redaction_kinds)
            # 2026-08-25（核心测试发现修复）：content_hash+persona+agent 幂等去重——
            # 同内容重复 ingest 返回现有 memory_id（CRDT 幂等语义），
            # 此前 UNIQUE 约束直接抛 IntegrityError。
            try:
                _dup = conn.execute(
                    "SELECT memory_id FROM memories WHERE content_hash=? "
                    "AND persona_id=? AND agent_id=? AND status='active' LIMIT 1",
                    (sha256_hash, persona_id, agent_id),
                ).fetchone()
                if _dup:
                    # t44/G3：去重早退路径也必须**如实报账**（此前整个字典没有这两个字段
                    # ⇒ 重复写入的响应里根本读不出"到底有没有脱敏"，同一类"字段说假话"）。
                    # t59/H3：再加 `inserted`/`deduped` —— 让**每一条**结果都能**机器判定**
                    # 它到底是"新落行"还是"命中已有行"。此前只有 `dedup=True`（且**新写入的那条
                    # 什么标记都没有**）⇒ 调用方只能靠"字段缺失"反推，批量通道更看不出真实新增数。
                    return {"memory_id": _dup["memory_id"], "version_id": None,
                            "sha256_hash": sha256_hash, "dedup": True,
                            "inserted": False, "deduped": True,
                            "timestamp": now, "pushed_memories": [],
                            "auto_redacted": redacted_any,
                            "pii_redacted_types": redaction_kinds,
                            "redaction_source": redaction_source,
                            "pii_redaction": _ing if isinstance(_ing, dict) else None}
            except Exception as _e:
                swallow(__name__, _e)
            # 2026-09-13（H1-8 defense-in-depth）：适配器层注入守卫。
            # 注入扫描此前只挂 client.ingest，而 consolidator/compressor/extractor/
            # evolution/self_model 等生产路径直写本方法 ⇒ 侧门敞开
            # （实测 adapter 路径 ASR@1 0.500 / ASR@5 1.000）。high → archived。
            _inj_status = "active"
            _guard_isolated = False   # 注入守卫**自身**判定隔离（审计动作名据此区分）
            # 2026 优化轮 B6：调用方请求的隔离——在 INSERT 前即定型，与记忆行
            # 同一条 INSERT 落库。此前 client 层的隔离是"写 active → 事后
            # archive_memories"，两次独立 commit，实测存在窗口（归档失败 ⇒
            # 内容静默留在 active 检索面）。只允许下调，不提供恢复 active 的通道。
            if str(status or "").strip().lower() == "archived":
                _inj_status = "archived"
            try:
                from trinity.security.injection import adapter_write_guard
                _g = adapter_write_guard(content, agent_id=agent_id, category=category,
                                         tags=tags, metadata=metadata)
                if _g.get("flagged"):
                    metadata = dict(metadata or {})
                    metadata["injection_scan"] = {"severity": _g.get("severity"),
                                                  "patterns": _g.get("patterns", []),
                                                  "layer": "adapter"}
                if _g.get("isolate"):
                    _inj_status = "archived"
                    _guard_isolated = True
            except Exception as _e:
                swallow(__name__, _e)

            tokenized = self._tokenize_content_for_fts(stored_content)
            plain_content = stored_content  # 明文副本（加密前的原始文本）
            # B5 存储加密：content 列写密文；tokenized 明文；hash 基于明文
            stored_content = self._encrypt_content(stored_content)
            tokenized = self._tokenized_for_storage(plain_content, tokenized)

            self._batch_buffer.append({
                "memory_id": memory_id,
                "session_id": session_id,
                "persona_id": persona_id,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "app_id": app_id,
                "content": stored_content,
                "role": role,
                "importance": importance,
                "tags_json": tags_json,
                "category": category,
                "memory_layer": memory_layer,
                "sha256_hash": sha256_hash,
                "now": now,
                "ttl_seconds": ttl_seconds,
                "modality": modality,
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
                "source_uri": source_uri,
            })

            conn.execute("""
                INSERT INTO memories
                (memory_id, session_id, persona_id, tenant_id, agent_id, app_id, content,
                 tokenized_content, role,
                 importance, tags, category, memory_layer, sha256_hash, status, version,
                 ttl_seconds, last_accessed_at, access_count, importance_score,
                 content_hash, conflict_group_id, is_resolved,
                 modality, metadata, source_uri,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 0.0,
                        ?, NULL, 0, ?, ?, ?, ?, ?)
            """, (memory_id, session_id, persona_id, tenant_id, agent_id, app_id, stored_content,
                  tokenized, role,
                  importance, tags_json, category, memory_layer, sha256_hash, _inj_status, ttl_seconds, now,
                  sha256_hash, modality, json.dumps(metadata or {}, ensure_ascii=False),
                  source_uri, now, now))

            conn.execute("""
                INSERT INTO memory_versions
                (version_id, memory_id, content, sha256_hash, operation, created_at)
                VALUES (?, ?, ?, ?, 'CREATE', ?)
            """, (version_id, memory_id, stored_content, sha256_hash, now))

            # 2026-09-13（H1-8）：被隔离的投毒写入单独记审计（静默归档不可回溯）
            # 2026 优化轮 B6：判据用 `_guard_isolated` 而非 `_inj_status` ——
            # 后者现在也包含"调用方请求的隔离"（压测/评测/敏感），那些不是注入，
            # 记成 INJECTION_ISOLATED 会污染审计语义（其动作名由 client 层按
            # ISOLATED_TEST_WRITE / POLICY_QUARANTINE / INJECTION_ISOLATED 区分）。
            if _guard_isolated:
                try:
                    self.write_audit_log(
                        memory_id=memory_id, action="INJECTION_ISOLATED",
                        agent_id=agent_id, persona_id=persona_id,
                        details={"layer": "adapter",
                                 "patterns": (metadata or {}).get(
                                     "injection_scan", {}).get("patterns", [])})
                except Exception as _e:
                    swallow(__name__, _e)

            # ── 审计日志（只写一次） ────────────────────────────────────
            self._write_audit_log(
                action="STORE_MEMORY",
                memory_id=memory_id,
                persona_id=persona_id,
                content_hash=sha256_hash,
                metadata={
                    # t44/G3：审计日志此前写的是**参数** `auto_redact_pii`（默认 False）
                    # ⇒ 审计面与响应面**同一处假话**。现改为如实报两层账本。
                    "has_pii": bool(redaction_kinds),
                    "pii_types": redaction_kinds,
                    "auto_redacted": redacted_any,
                    "redaction_source": redaction_source,
                    "session_id": session_id,
                },
            )

            # 批量提交管理：加入缓冲区，达到条件再 commit
            self._maybe_flush()

            # 2026-08-18（conflict 检测改进）：写入后相似性冲突检测——
            # 高相似但内容不同的旧记忆自动分配 conflict_group_id（候选冲突组）。
            # 2026-08-24（R8 P1-5 修复）：传 plain_content（明文）而非
            # 加密后的 stored_content——密文 base64 分词与明文候选零重叠，
            # 本适配器加密默认开启（2026-09-29 口径更正：指 **SQLite 侧**；
            # PG 侧无加密实装，见 docs/SECURITY_BOUNDARIES.md）后冲突检测曾整体失效。
            # 2026-10-08（t115/G7C · **C 案**）：这一步是写路径的**主要成本**
            # （t110 实测 ≈62.0/62.3ms @N=12000）⇒ **挪出写路径**（后台线程补算）。
            # ⚠️ **语义变更**：`conflict_group_id` 的**可见时点延后**（写入返回时尚未写入），
            #    写入返回时机**不变**；**判定逻辑本体一字未动**（仍是 `_assign_conflicts`）。
            # ⚠️ **A 案（收窄 OR 召回词条）已证伪**：它只多找到 2 条（真零召回基线 **0/12**），
            #    相对 12/12 是**不可接受的召回损失** ⇒ 本处**不碰候选源**（见 G7C §3.3）。
            # ⭐ **开关默认 `off`（队长 2026-10-08 裁定）**：实测证明的是**能力有效**
            #    （−96.5% + 召回不退），**不是"默认值应当为 on"**；⚠️ 未测⑤（下游是否要求
            #    "写入返回即可见"）未核 ⇒ **默认开的代价是未知的下游时序依赖**，保守开 `off` 代价≈0。
            #    另：崩溃窗口内会静默丢判定（§4-B2/B3）⇒ 默认开会把该窗口铺到全量写入。
            # 开启：`TRINITY_CONFLICT_ASYNC=on`；回滚：置 off/删除该变量 ⇒ 逐字回到同步行为。
            if os.environ.get("TRINITY_CONFLICT_DETECT", "on") != "off":
                # t136/G10R4：登记"惰性补偿用哪个适配器"（进程内第一个写入者生效）
                # ⇒ 让 `conflict_compensate_stats()` 这个**公开诊断入口**能触发一批补偿。
                try:
                    register_compensate_owner(self)
                except Exception as _e:  # noqa: BLE001 — 登记失败不影响写入
                    swallow(__name__, _e)
                if _conflict_async_enabled():
                    try:
                        _conflict_async_submit(self, memory_id, plain_content)
                    except Exception as _e:  # noqa: BLE001 — 入队失败 ⇒ 退回同步，判定不丢
                        swallow(__name__, _e)
                        try:
                            self._assign_conflicts(memory_id, plain_content)
                        except Exception as _e2:  # noqa: BLE001
                            swallow(__name__, _e2)
                else:
                    try:
                        self._assign_conflicts(memory_id, plain_content)
                    except Exception as _e:
                        swallow(__name__, _e)

            return {
                "memory_id": memory_id,
                "version_id": version_id,
                "sha256_hash": sha256_hash,
                "timestamp": now,
                "persona_id": persona_id,
                "session_id": session_id,
                "app_id": app_id,
                # t59/H3：**这一条到底落没落行** —— 机器可判定，与去重早退那支**同一套字段**
                # （`inserted=True` ⇔ 本次真的 INSERT 了新行；`deduped=True` ⇔ 命中已有行、**未**新增）。
                # ⚠️ 不得恒真：去重早退支返回的是 `inserted=False, deduped=True`（见上）。
                "inserted": True,
                "deduped": False,
                # t44/G3：**如实反映写入路径上实际发生的脱敏**（两层账本的并集）。
                # 反事实：两套机制都没掩 ⇒ False / []（**不恒真**）。
                "auto_redacted": redacted_any,
                "pii_redacted_types": redaction_kinds,
                # 新增：**哪套机制掩的**（`ingestion` / `adapter` / `both` / `None`）——
                # 纯加法字段，既有消费者不受影响（全仓无人按这两个旧字段分支）。
                "redaction_source": redaction_source,
                "pii_redaction": _ing if isinstance(_ing, dict) else None,
            }
    def _assign_conflicts(self, new_memory_id: str, content: str) -> int:
        """2026-08-18（agent-memory-bench conflict 模式对齐）：写入后检测
        高相似但内容不同的旧记忆，分配相同 conflict_group_id（is_resolved=0）。

        相似度用 jieba 分词 token 集合重叠率（FTS5 BM25 对"语义相近但关键
        信息不同"的矛盾记忆给分过低，不适合做矛盾检测——如"端口是 5432"
        vs "端口是 5430" 只共享前缀词，BM25 分数 ~0）。

        Returns:
            分配的冲突组数量。
        """
        try:
            # 2026-08-21（性能修复）：召回查询截断——_search_fts 会把 query 逐词
            # 拼成 "词"* OR MATCH，超长中文 content 会切出数千词条导致单次 ingest
            # 冲突检测达分钟级（benchmark ingest 卡死根因）。冲突检测只需召回
            # 候选（token 重叠判断在下方用完整 content 计算），前缀截断语义不变；
            # TRINITY_CONFLICT_QUERY_MAX=0 可关闭召回（跳过冲突检测查询）。
            qmax = int(os.environ.get("TRINITY_CONFLICT_QUERY_MAX", "300"))
            recall_query = content if qmax <= 0 else content[:qmax]
            hits = self.search_memories(query=recall_query, top_k=10, touch=False)  # 候选召回（放宽）
        except Exception:
            return 0
        new_tokens = self._token_set(content)
        overlap_threshold = float(
            os.environ.get("TRINITY_CONFLICT_OVERLAP", CONFLICT_TOKEN_OVERLAP))
        assigned = 0
        for r in hits:
            mid = r.get("memory_id")
            if not mid or mid == new_memory_id:
                continue
            old_content = str(r.get("content", ""))
            if old_content == content:
                continue  # 完全相同内容（唯一约束已挡，防御）
            old_tokens = self._token_set(old_content)
            if not new_tokens or not old_tokens:
                continue
            inter = len(new_tokens & old_tokens)
            overlap = inter / max(len(new_tokens), len(old_tokens))
            if overlap >= overlap_threshold:
                group = "conf_" + hashlib.md5(
                    "|".join(sorted([new_memory_id, mid])).encode()
                ).hexdigest()[:12]
                with self._write_lock:
                    self._conn.execute(
                        "UPDATE memories SET conflict_group_id=?, is_resolved=0 "
                        "WHERE memory_id IN (?, ?)",
                        (group, new_memory_id, mid),
                    )
                    self._conn.commit()
                assigned += 1
        return assigned

    # ── t136/G10R4：冲突判定的**补偿**（把 G7C 的崩溃窗口"接线"）───────────────────
    def _conflict_compensate_targets(self, limit: int, after_rowid: int = 0) -> List[Dict[str, Any]]:
        """选出**需要补偿**的行：`status='active'` 且 `conflict_group_id IS NULL`。

        ⭐ **为什么这就是"需要补偿"的判据**：`_assign_conflicts` 是**唯一**写
        `conflict_group_id` 的地方（全仓已核）⇒ **NULL 就意味着"这条的判定没有产出结果"**
        —— 合法情形（本来就没冲突）与丢失情形**在这个字段上不可区分**，
        所以本函数**不试图区分**（区分不了），而是**把两类都重算一遍**：
        重算对"本来就没冲突"的行**不产生任何写入**（判据②），对真正丢失的行**补上组**（判据①）。

        ⚠️ **`limit`（限流）**：`0` = 不限流（⚠️ 仅供一次性脚本显式指定）。
        ⚠️ **`after_rowid`（游标）**：避免每次重头扫（便于分批/续跑）。
        """
        sql = ("SELECT memory_id, content, rowid FROM memories "
               "WHERE status='active' AND conflict_group_id IS NULL AND rowid > ? "
               "ORDER BY rowid")
        args: List[Any] = [after_rowid]
        if limit and limit > 0:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            content = r["content"]
            # 该行的内容可能是密文（本适配器加密默认开）⇒ 用产品自己的解密路径
            try:
                plain = self._decrypt_text_resilient(
                    content, memory_id=r["memory_id"],
                    tokenized=(r["tokenized_content"] if "tokenized_content" in r.keys() else None),
                    where="conflict_compensate")
            except Exception:  # noqa: BLE001 — 解密失败的行跳过（**不猜内容**）
                continue
            out.append({"memory_id": r["memory_id"], "content": plain, "rowid": r["rowid"]})
        return out

    def compensate_missing_conflicts(self, limit: Optional[int] = None,
                                     after_rowid: int = 0) -> Dict[str, Any]:
        """⭐ **一次性补偿**：对"没有冲突组"的 active 行重跑 `_assign_conflicts`。

        ⚠️⚠️ **本函数【不消除】G7C 的崩溃窗口** —— 它只在**下一次启动/调用之后**把
        「窗口内丢失的判定」补回来。**准确表述：把静默变成可发现（可修），不是消除窗口。**

        ⛔ **不改判定逻辑**：内核仍是 `_assign_conflicts`（同一阈值、同一召回、同一组名规则）
        ⇒ **对"本来就没冲突"的行不产生写入**（判据②的"0 变更"就是钉这一条）。

        🔁 **幂等**：跑两次，第二次 `changed=0`（补上组的行已不再是 NULL）。
        🚦 **限流**：`limit` 默认取 `TRINITY_CONFLICT_COMPENSATE_BATCH`（200）；
           `0` = 不限流（⚠️ 危险）。
        """
        import time as _t
        t0 = _t.perf_counter()
        if limit is None:
            limit = _conflict_compensate_batch()
        targets = self._conflict_compensate_targets(limit=limit, after_rowid=after_rowid)
        scanned = changed = no_change = errors = 0
        last_rowid = after_rowid
        for t in targets:
            scanned += 1
            last_rowid = max(last_rowid, int(t["rowid"]))
            before = self._conn.execute(
                "SELECT conflict_group_id FROM memories WHERE memory_id=?",
                (t["memory_id"],)).fetchone()
            before_g = before[0] if before else None
            try:
                self._assign_conflicts(t["memory_id"], t["content"])
            except Exception as exc:  # noqa: BLE001 — 单行失败不终止整批
                swallow(__name__, exc)
                errors += 1
                continue
            after = self._conn.execute(
                "SELECT conflict_group_id FROM memories WHERE memory_id=?",
                (t["memory_id"],)).fetchone()
            if after and after[0] and after[0] != before_g:
                changed += 1
            else:
                no_change += 1
        # ⭐ 剩余量用**全局计数**，不用"rowid > cursor" —— 理由（我在 t136 自查时发现的真缺陷）：
        #   `_assign_conflicts` 会把**命中的邻居行**也一起打上组（同组的两行都被 UPDATE）
        #   ⇒ 扫完 rowid ≤ k 的目标后，**rowid > k 的行也可能已经被顺带补上**
        #   ⇒ 用 `rowid > last_rowid` 统计剩余会**报成 0（假"已完成"）**，而库里可能还有 NULL 行。
        #   ⇒ 改为**全局** `count(*) ... IS NULL`：无论谁被顺带补上，读数都真实。
        more = self._conn.execute(
            "SELECT count(*) FROM memories WHERE status='active' "
            "AND conflict_group_id IS NULL").fetchone()[0]
        res = {"scanned": scanned, "changed": changed, "no_change": no_change,
               "errors": errors,
               "remaining_total": int(more),          # ⭐ 全局剩余（不用游标）
               "remaining_after_cursor": int(more),   # 保留旧字段名，语义 = 全局剩余（见上注释）
               "budget_exhausted": int(more > 0),
               "last_rowid": last_rowid,
               "ms": round((_t.perf_counter() - t0) * 1000.0, 3),
               "batch_limit": limit}
        _CONFLICT_COMPENSATE_STATS["runs"] += 1
        _CONFLICT_COMPENSATE_STATS["scanned"] += scanned
        _CONFLICT_COMPENSATE_STATS["changed"] += changed
        _CONFLICT_COMPENSATE_STATS["no_change"] += no_change
        _CONFLICT_COMPENSATE_STATS["errors"] += errors
        _CONFLICT_COMPENSATE_STATS["budget_exhausted"] += int(more > 0)
        _CONFLICT_COMPENSATE_STATS["last_run_ts"] = _t.strftime("%Y-%m-%d %H:%M:%S")
        _CONFLICT_COMPENSATE_STATS["last_run_ms"] = res["ms"]
        return res

    def compensate_missing_conflicts_at_startup(self) -> Optional[Dict[str, Any]]:
        """启动时补偿（**默认 off**）：`TRINITY_CONFLICT_COMPENSATE=on` 才跑。

        ⚠️ **只扫一批**（`TRINITY_CONFLICT_COMPENSATE_BATCH`，默认 200）⇒ **不阻塞启动**；
        剩余的行留给下一次启动 / 运维脚本 / 后续写入补齐（`remaining_after_cursor` 可读）。
        """
        if not _conflict_compensate_enabled():
            return None
        try:
            return self.compensate_missing_conflicts()
        except Exception as exc:  # noqa: BLE001 — 补偿失败**不得**影响启动/正常写入
            swallow(__name__, exc)
            return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}

    @staticmethod
    def _token_set(text: str):
        """jieba 分词 + 去空白，返回 token 集合（用于冲突相似度）。

        2026-08-21（性能修复）：只对前 TRINITY_CONFLICT_TOKEN_MAX（默认 2000）
        字符分词——超长文本（数万字会话）全量 jieba.cut 单次可达数秒，而
        冲突检测每次 ingest 要算 1 次新内容 + 每条候选（top_k=10），叠加成
        分钟级。冲突检测的语义是"主题级高重叠"，前 2000 字符已代表主题；
        短文本（<2000 字符）行为完全不变。
        """
        # t61/I1：**默认 off** 的 token 集缓存（纯函数 ⇒ 语义等价；见模块头 `_TOKEN_CACHE` 注释）
        _cache_on = _token_cache_enabled()
        _ckey = str(text)
        if _cache_on:
            _hit = _TOKEN_CACHE.get(_ckey)
            if _hit is not None:
                _TOKEN_CACHE_STATS["hits"] += 1
                _TOKEN_CACHE.move_to_end(_ckey)
                return _hit
            _TOKEN_CACHE_STATS["misses"] += 1
        try:
            import jieba
            tmax = int(os.environ.get("TRINITY_CONFLICT_TOKEN_MAX", "2000"))
            src = _ckey
            if tmax > 0 and len(src) > tmax:
                src = src[:tmax]
            tokens = [t.strip() for t in jieba.cut(src) if t.strip()]
        except Exception:
            tokens = [t.strip() for t in re.split(r"[\s,，。；;：:、]+", _ckey) if t.strip()]
        _out = frozenset(tokens)
        if _cache_on:
            _cap = _token_cache_max()
            if _cap > 0:
                _TOKEN_CACHE[_ckey] = _out
                _TOKEN_CACHE.move_to_end(_ckey)
                while len(_TOKEN_CACHE) > _cap:
                    _TOKEN_CACHE.popitem(last=False)
                    _TOKEN_CACHE_STATS["evictions"] += 1
        return set(_out)


    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """查询单条记忆（2026-08-15 v2：线程本地只读连接）。"""
        conn = self._get_read_conn()
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("content"):
            d["content"] = self._decrypt_text_resilient(
                d["content"], memory_id=d.get("memory_id"),
                tokenized=d.get("tokenized_content"), where="get_memory")
        return d
    def get_memory_owners(self, memory_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量查询记忆的归属与状态（hybrid 检索隔离后过滤用）。

        返回 {memory_id: {status, agent_id, persona_id, tenant_id}}；
        不在库中的 id 不出现（调用方据此区分"池记忆/幽灵"）。

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        """
        if not memory_ids:
            return {}
        conn = self._get_read_conn()
        if not conn:
            return {}
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, status, agent_id, persona_id, tenant_id "
            f"FROM memories WHERE memory_id IN ({placeholders})",
            list(memory_ids),
        ).fetchall()
        return {
            r["memory_id"]: {
                "status": r["status"],
                "agent_id": r["agent_id"],
                "persona_id": r["persona_id"],
                "tenant_id": r["tenant_id"],
            }
            for r in rows
        }
    def get_persona_memories(
        self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        if agent_id:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE persona_id = ? AND agent_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona_id, agent_id, limit))
        else:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE persona_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona_id, limit))
        rows = [dict(row) for row in cursor.fetchall()]
        return self._decrypt_rows_resilient(rows, where="get_persona_memories")
    @_safe_write
    def delete_memory(self, memory_id: str) -> bool:
        with self._write_lock:

            conn = self._conn
            if not conn:
                return False

            cursor = conn.execute(
                "SELECT memory_id, persona_id, sha256_hash FROM memories WHERE memory_id = ?",
                (memory_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            persona_id = row["persona_id"]
            content_hash = row["sha256_hash"]

            conn.execute(
                "UPDATE memories SET status = 'deleted', updated_at = datetime('now') WHERE memory_id = ?",
                (memory_id,)
            )
            conn.execute("""
                INSERT INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at)
                SELECT ? || '_del', memory_id, content, sha256_hash, 'DELETE', datetime('now')
                FROM memories WHERE memory_id = ?
            """, (memory_id, memory_id))

            # ── 审计日志 ────────────────────────────────────────────────
            self._write_audit_log(
                action="DELETE_MEMORY",
                memory_id=memory_id,
                persona_id=persona_id,
                content_hash=content_hash,
            )
            conn.commit()
            return True
    @_safe_write
    def purge_memory(self, memory_id: str, reason: str = "") -> Dict[str, Any]:
        """GDPR 硬擦除（2026-09-02, Fable 对照审计 P2-⑤⑦）。覆写销毁+行保留。"""
        with self._write_lock:
            conn = self._conn
            if not conn:
                return {"memory_id": memory_id, "purged": False, "error": "no conn"}
            row = conn.execute(
                "SELECT memory_id, persona_id, sha256_hash FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return {"memory_id": memory_id, "purged": False, "error": "not_found"}
            prior_hash = row["sha256_hash"]
            persona_id = row["persona_id"]
            now = datetime.now(timezone.utc).isoformat()
            sentinel = "[HARD_PURGED %s] %s" % (now, memory_id)
            meta = json.dumps({"hard_purged": True, "purged_at": now,
                               "reason": (reason or "")[:200]}, ensure_ascii=False)
            enc = self._encrypt_content(sentinel)
            sent_hash = self._compute_sha256(sentinel)
            conn.execute(
                "UPDATE memories SET content = ?, tokenized_content = ?,"
                " status = 'gdpr_deleted', sha256_hash = ?, content_hash = ?,"
                " metadata = ?, importance = 0, updated_at = ? WHERE memory_id = ?",
                (enc, sentinel, sent_hash, sent_hash, meta, now, memory_id),
            )
            conn.execute(
                "UPDATE memory_versions SET content = ? WHERE memory_id = ?",
                (sentinel, memory_id),
            )
            try:
                conn.execute(
                    "DELETE FROM memory_links WHERE memory_id = ?"
                    " OR from_memory_id = ? OR to_memory_id = ?",
                    (memory_id, memory_id, memory_id),
                )
            except Exception as _e:
                swallow(__name__, _e)
            conn.commit()
            return {"memory_id": memory_id, "purged": True,
                    "prior_sha256": prior_hash, "status": "gdpr_deleted",
                    "persona_id": persona_id}
    @_safe_write
    def update_importance(
        self,
        memory_id: str,
        importance: float,
        importance_score: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """轻量价值回写（EXECUTION 612，H2 修复）：仅更新 importance /
        importance_score / metadata(merge)，不动 content/version/FTS。

        替代历史 _deep_value 硬编码直连外部 PG 的 UPDATE：SQLite/隔离后端
        经当前 adapter 更新本库（隔离评测不再隐性连 live PG）。

        Returns:
            True 若行存在并更新；False 未找到/未连接。
        """
        with self._write_lock:
            conn = self._conn
            if not conn:
                return False
            row = conn.execute(
                "SELECT importance_score, metadata FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return False
            d = dict(row)
            try:
                old_meta = json.loads(d.get("metadata") or "{}")
            except Exception:
                old_meta = {}
            if not isinstance(old_meta, dict):
                old_meta = {}
            if metadata:
                old_meta.update(metadata)
            now = datetime.now(timezone.utc).isoformat()
            score = (float(importance_score)
                     if importance_score is not None else float(importance))
            conn.execute(
                "UPDATE memories SET importance = ?, importance_score = ?,"
                " metadata = ?, updated_at = ? WHERE memory_id = ?",
                (float(importance), score,
                 json.dumps(old_meta, ensure_ascii=False), now, memory_id),
            )
            conn.commit()
            return True

    @_safe_write
    def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing memory with version tracking (conflict-preserving).

        Old version rows stay in memory_versions untouched; a new version row
        with operation 'UPDATE' is appended. The memories row is bumped to
        version + 1 with recomputed sha256/content_hash/tokenized_content.
        An UPDATE_MEMORY audit log entry is written.

        Returns:
            The updated memory row as a dict, or None if memory_id not found.
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return None

            cursor = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            current = dict(row)

            now = datetime.now(timezone.utc).isoformat()
            version_id = f"ver_{uuid.uuid4().hex[:12]}"

            new_content = content if content is not None else self._decrypt_content(current.get("content", ""))
            new_importance = (
                importance if importance is not None
                else float(current.get("importance", 0.5))
            )
            current_tags = current.get("tags") or "[]"
            new_tags = (
                tags if tags is not None
                else (json.loads(current_tags) if isinstance(current_tags, str) else current_tags)
            )
            new_category = (
                category if category is not None
                else current.get("category", "general")
            )

            sha256_hash = self._compute_sha256(new_content)
            tokenized = self._tokenize_content_for_fts(new_content)
            plain_content = new_content
            # B5 存储加密：content 列写密文；tokenized 明文；hash 基于明文
            new_content = self._encrypt_content(new_content)
            tokenized = self._tokenized_for_storage(plain_content, tokenized)

            conn.execute("""
                UPDATE memories
                SET content = ?, tokenized_content = ?, importance = ?, tags = ?,
                    category = ?, sha256_hash = ?, content_hash = ?,
                    version = version + 1, updated_at = ?
                WHERE memory_id = ?
            """, (new_content, tokenized, new_importance,
                  json.dumps(normalize_tags(new_tags), ensure_ascii=False),  # EXECUTION 771
                  new_category, sha256_hash, sha256_hash, now, memory_id))

            conn.execute("""
                INSERT INTO memory_versions
                (version_id, memory_id, content, sha256_hash, operation, created_at)
                VALUES (?, ?, ?, ?, 'UPDATE', ?)
            """, (version_id, memory_id, new_content, sha256_hash, now))

            self._write_audit_log(
                action="UPDATE_MEMORY",
                memory_id=memory_id,
                persona_id=current.get("persona_id"),
                content_hash=sha256_hash,
                metadata={"old_version": current.get("version", 1)},
            )
            conn.commit()

            cursor = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            )
            updated = cursor.fetchone()
            if not updated:
                return None
            d = dict(updated)
            if d.get("content"):
                d["content"] = self._decrypt_text_resilient(
                d["content"], memory_id=d.get("memory_id"),
                tokenized=d.get("tokenized_content"), where="get_memory_by_hash")
            return d
    @_safe_write
    def archive_memories(self, memory_ids: List[str]) -> int:
        """批量将记忆标记为 archived（衰减压缩回写；与 PostgreSQLAdapter 同接口）。

        2026-09-29（判据接线 · C1）：与 PG 侧同步豁免 PROTECTED_ARCHIVE_CATEGORIES
        （身份锚点/自我公理等不得被归档链路清掉——实测 PG 侧 2026-09-22 掉过一条）。
        """
        if not memory_ids:
            return 0
        from trinity.adapters.base import PROTECTED_ARCHIVE_CATEGORIES as _PROT
        with self._write_lock:
            conn = self._conn
            if not conn:
                return 0
            now = datetime.now(timezone.utc).isoformat()
            placeholders = ",".join("?" * len(memory_ids))
            cat_ph = ",".join("?" * len(_PROT))
            cur = conn.execute(
                f"UPDATE memories SET status = 'archived', updated_at = ? "
                f"WHERE memory_id IN ({placeholders}) "
                f"AND COALESCE(category,'') NOT IN ({cat_ph})",
                [now] + list(memory_ids) + list(_PROT),
            )
            conn.commit()
            return cur.rowcount
    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY created_at ASC
        """, (memory_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        return self._decrypt_rows_resilient(rows, where="get_version_chain")

    def _decrypt_rows_resilient(self, rows: List[Dict[str, Any]], *,
                                where: str = "read") -> List[Dict[str, Any]]:
        """逐行解密 `content`：**单行失败不得让整次调用失效**。

        2026-09-29（外部审计修复，根因 A）：实测生产库 106,270 行 `enc:v1:` 中**恰好 1 行**
        （`mem_ed7014b05467444b`）解密抛 `cryptography.exceptions.InvalidTag`，而本文件的
        逐行解密**没有防护** ⇒ 整次 `get_all_memories` 抛错 ⇒ 调用方把它吞成一条
        **消息为空**的 warning（`str(InvalidTag())` 是空串）⇒ **向量通道恒返回空、
        BM25 索引 / PageTree / 摄取去重一并静默降级**。

        处置：逐行 try；失败时优先退回该行自己的 `tokenized_content`（明文），
        否则置空并标记 `decrypt_failed`；**每行指名 memory_id 记录**，末尾汇总计数。
        """
        bad = 0
        for r in rows:
            if not r.get("content"):
                continue
            try:
                r["content"] = self._decrypt_content(r["content"])
            except Exception as _e:                      # noqa: BLE001
                bad += 1
                _tok = r.get("tokenized_content") or ""
                r["content"] = _tok
                r["decrypt_failed"] = True
                r["decrypt_error"] = type(_e).__name__
                logger.warning(
                    "%s: 解密失败 memory_id=%s (%s)；%s",
                    where, r.get("memory_id"), type(_e).__name__,
                    "已退回该行 tokenized_content" if _tok else "content 置空")
        if bad:
            logger.warning("%s: %d/%d 行解密失败（已逐行降级，其余行不受影响）",
                           where, bad, len(rows))
        return rows

    def get_all_memories(self, agent_id: Optional[str] = None, limit: int = 200,
                          offset: int = 0) -> List[Dict[str, Any]]:
        """Get all active memories across all personas/tenants, optionally filtered by agent_id.

        2026-08-15（压测修复 v2）：线程本地只读连接（纯读，无锁）。
        2026-08-26（PageTree）：新增 offset 分页（页树全量建树用）。
        """
        conn = self._get_read_conn()
        if not conn:
            return []

        if agent_id:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active' AND agent_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (agent_id, limit, offset))
        else:
            cursor = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
        rows = [dict(row) for row in cursor.fetchall()]
        return self._decrypt_rows_resilient(rows, where="get_all_memories")

    def get_index_documents(self, limit: int = 200000) -> List[Tuple[str, str]]:
        """检索索引专用**精简取数**：只取 (memory_id, content) 两列并解密。

        2026-09-29（外部审计修复）：本结果与 `PostgreSQLAdapter.get_index_documents`
        （postgresql.py:1162）**同契约、同排序、同 LIMIT**，只是换 SQLite 的读连接。

        修的是什么
        ----------
        此前 SQLite 侧**没有**本方法，于是
        `trinity/core/client/_hybrid_index.py::_ensure_bm25_index` 的
        `getattr(self._adapter, "get_index_documents", None)` 取到 None，
        **静默回退**到 `get_all_memories`——`SELECT *`（本表 36 列）+ 逐行
        DictRow + 逐行解密。这条回退路径是 PG 侧 2026-09-15（R41-P23）早已用
        精简路径消掉的冷启动开销（实测 3.35s → 0.34s，10x），SQLite 侧却一直保留。

        更严重的是**可观测性**：回退路径一旦抛错，构建线程的
        `except Exception: swallow(...)` 会把它吞成**空索引**，而空索引在
        响应 `breakdown` 里与「没有匹配文档」**逐字不可区分**。外部审计实测
        生产 API 连续 **42/42** 次查询 `breakdown.bm25 == 0`（中英文皆然），
        而在同一份数据上离线重建索引对同样的查询是 8/8 有命中 ⇒ 缺陷在
        **构建/接线**，不在语料。

        口径：`status='active' ORDER BY created_at DESC LIMIT ?`，与
        `get_all_memories` 保持相同排序与上限，确保取到**同一批**文档。
        文本为**解密后的 content**（与 PG 逐字同义）；解密失败原样返回，
        由调用方决定降级。仅供索引构建等只读消费方使用。
        """
        conn = self._get_read_conn()
        if not conn:
            return []
        cursor = conn.execute(
            "SELECT memory_id, content FROM memories "
            "WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        out: List[Tuple[str, str]] = []
        for row in cursor:
            _mid, _txt = row[0], row[1]
            if not _mid:
                continue
            if _txt:
                try:
                    _txt = self._decrypt_content(_txt)
                except Exception:
                    _txt = _txt or ""
            else:
                _txt = ""
            out.append((str(_mid), _txt))
        return out

    @_safe_write
    def set_embedding(self, memory_id: str, query_vec: Any) -> bool:
        """写入单条记忆的 embedding（回填 / 增量用）。

        2026-09-29（外部审计修复，根因 A「存储层单一权威 + 嵌入冻结」）：

        此前 `set_embedding` **只存在于 PostgreSQLAdapter**（postgresql.py:775），
        而写入路径的嵌入回填又被 `if "postgres" in type(_adp).__name__.lower():`
        门控（`core/client/_ingestion.py:997`）⇒ **服务跑在 SQLite 上时，写入路径
        根本不生成 embedding**。实测后果：SQLite 的 embedding 写入恰好停在
        2026-08-26T03:33:52，此后 33 天零写入（近 24h：新增 1,456 条 active → 0 条嵌入），
        而服务读的正是 SQLite（`sqlite-only` id → 200，`pg-only` → 404）
        ⇒ 向量通道长期在只覆盖 6.5% 语料的冻结索引上检索。

        存储形态与 PG **不同且必须不同**：PG 用 `vector` 列类型，存的是文本字面量
        `[0.1,0.2,...]`；SQLite 无向量类型，`embedding` 列是 **BLOB**，
        存**原始小端 float32**。已实测确认：存量 active 主流形态为 4096 字节
        = 1024 维 × 4 字节，用 `<f4` 解出的向量与 bge-m3 重算结果**余弦 = 1.0000**。
        故此处必须逐字对齐该形态，否则新写的向量与存量不在同一空间（会静默降质）。

        契约与 PG 对齐：入参是可迭代的浮点向量；返回是否真的更新了行。
        幂等：重复写入同一 memory_id 只是覆盖同一列。
        """
        try:
            import numpy as _np
            vec = _np.asarray(query_vec, dtype="<f4").reshape(-1)
        except Exception:
            vec = _np.asarray(list(query_vec), dtype="<f4").reshape(-1)
        if vec.size == 0:
            return False
        blob = vec.tobytes()
        with self._write_lock:
            conn = self._conn
            if not conn:
                return False
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "UPDATE memories SET embedding = ?, updated_at = ? WHERE memory_id = ?",
                (blob, now, memory_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    @_safe_write
    def touch_memory(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。

        Args:
            memory_id: 要触达的记忆 ID。

        Returns:
            是否成功更新。
        """
        with self._write_lock:

            conn = self._conn
            if not conn:
                return False

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                UPDATE memories
                SET last_accessed_at = ?,
                    access_count = access_count + 1,
                    updated_at = ?
                WHERE memory_id = ?
            """, (now, now, memory_id))
            conn.commit()
            return cursor.rowcount > 0
    def _touch_batch(self, memory_ids: List[str]) -> None:
        """批量累积搜索命中的记忆访问（异步写，读路径零阻塞）。

        2026-08-15（压测修复）：原实现同步 UPDATE+commit（每次检索都写库，
        实测占读延迟 ~40%）。改为入内存队列，由 _touch_flush_loop 后台线程
        定期批量 flush（一次 UPDATE…IN + 一次 commit）。语义保持：
        access_count 按命中次数累加；last_accessed_at 取 flush 时刻。
        失败静默，不影响搜索主流程。
        """
        if not memory_ids:
            return
        with self._write_lock:
            for mid in memory_ids:
                self._touch_queue[mid] = self._touch_queue.get(mid, 0) + 1
        self._touch_pending.set()
    def _touch_flush_loop(self) -> None:
        """后台线程：周期 flush touch 队列（batch UPDATE + 一次 commit）。"""
        while not self._touch_stop.wait(1.0):
            try:
                self._flush_touch_queue()
            except Exception as _e:
                swallow(__name__, _e)  # 静默失败
    def _flush_touch_queue(self) -> None:
        """把累积的 touch 队列批量写入（幂等；空队列直接返回）。"""
        with self._write_lock:
            if not self._touch_queue:
                self._touch_pending.clear()
                return
            conn = self._conn
            if not conn:
                return
            queue = self._touch_queue
            self._touch_queue = {}
            self._touch_pending.clear()
            try:
                now = datetime.now(timezone.utc).isoformat()
                mids = list(queue.keys())
                counts = queue
                placeholders = ",".join("?" for _ in mids)
                # 单条 UPDATE 按计数累加（executemany + 一次 commit）
                conn.executemany(
                    "UPDATE memories SET access_count = access_count + ?, "
                    "last_accessed_at = ?, updated_at = ? WHERE memory_id = ?",
                    [(counts[mid], now, now, mid) for mid in mids],
                )
                conn.commit()
            except Exception as _e:
                # flush 失败：回填队列避免丢失（下一轮重试）
                # 2026-08-16 修复:必须先 rollback——python sqlite3 在 execute 异常后
                # 连接留在未提交事务中(不自动回滚), 悬挂写事务会永久占 SQLite 写锁
                # (worker 超时/锁复发根因, 与 skill 坑 #9 同源)。
                try:
                    conn.rollback()
                except Exception as _e:
                    swallow(__name__, _e)
                for mid, cnt in queue.items():
                    self._touch_queue[mid] = self._touch_queue.get(mid, 0) + cnt
    def age_memories(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count and details.
        """
        # 2026-08-16 修复:加 _write_lock + 异常 rollback——此前无锁保护且
        # UPDATE 抛异常时不回滚, 会悬挂写事务占锁(与 touch flush 同源)。
        with self._write_lock:
            conn = self._conn
            if not conn:
                return {"aged_count": 0, "error": "Not connected"}

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute("""
                SELECT memory_id FROM memories
                WHERE status = 'active'
                  AND ttl_seconds IS NOT NULL
                  AND created_at IS NOT NULL
                  AND datetime(created_at, '+' || ttl_seconds || ' seconds') < datetime(?)
            """, (now,))
            expired_ids = [row["memory_id"] for row in cursor.fetchall()]

            if not expired_ids:
                return {"aged_count": 0, "timestamp": now}

            try:
                placeholders = ",".join("?" for _ in expired_ids)
                conn.execute(f"""
                    UPDATE memories
                    SET status = 'expired', updated_at = ?
                    WHERE memory_id IN ({placeholders})
                """, [now] + expired_ids)
                conn.commit()
            except Exception as _e:
                try:
                    conn.rollback()
                except Exception as _e:
                    swallow(__name__, _e)
                raise

            return {"aged_count": len(expired_ids), "timestamp": now, "expired_ids": expired_ids}
