"""MemoryAggregator - embedding / FAISS vector index mixin (split from aggregator.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

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

from ._constants import (logger, _HAS_FAISS, VECTOR_PERSIST_FILENAME,
                         MIN_ROWS_FOR_INDEX_RATIO, MIN_INDEX_LANDING_RATIO)
from ._base import _AggregatorMixinBase

# 2026-09-21 §1014：**送进 embedding 模型的批量上限**（事故修复，见 _rebuild_index 内的完整说明）。
# 事故实测：该批量曾硬编码 4096 ⇒ 4096 条文本一次性进 ONNX transformer，激活内存约
# O(batch × heads × seq²)，把 API 进程 commit 推到 **177.5GB**、系统 commit-free 归零
# （py-spy 现场栈 _prewarm_ann_index → _rebuild_index → embed_batch → onnxruntime）。
# 峰值随批量近似线性 ⇒ 默认 64；TRINITY_ANN_EMBED_CHUNK 可覆盖（回滚 = 4096 恢复旧行为）。
try:
    _ANN_EMBED_CHUNK = int(os.environ.get("TRINITY_ANN_EMBED_CHUNK", "64") or 64)
except Exception:  # noqa: BLE001
    _ANN_EMBED_CHUNK = 64
if _ANN_EMBED_CHUNK < 1:
    _ANN_EMBED_CHUNK = 64

# 2026-09-21 §1025：**增量重建开关**（默认 on）。
# 动机：ANN 全量重建在 19k+ 条池上是重活（小批量 64 条 x ~300 次 ONNX 调用），期间 API 健康探测超时
# 被 supervisor 判死（§1019/§1024 实测每 6 分钟一轮）。增量只 embed「不在索引里」的新条目，
# 使重建成本与**新增量**成正比而不是与池大小成正比。
# 回滚：TRINITY_ANN_INCREMENTAL=0（强制走原全量路径）。
try:
    _ANN_INCREMENTAL = os.environ.get("TRINITY_ANN_INCREMENTAL", "1") == "1"
except Exception:  # noqa: BLE001
    _ANN_INCREMENTAL = True

# 2026-09-22 §1247：预热重建的**全局去重闸**（模块级，不是实例级）。
# 来由（§1246 全线程快照实测）：一个进程里同时有 **3 个 `agg-ann-prewarm`** 线程各跑一份
# `_rebuild_index`，同时请求路径也在重建 ⇒ 三方都在排队等**串行处理**的 Ollama
# （单次 HTTP 上限 30s × 分块 64 条 ⇒ 上百次往返），表现为「卡住」而不是报错。
# 为什么模块级：`_start_warmup` 已经保证**单个实例**只起一个预热线程，真正的重复来自
# **多个聚合器实例**（测试里逐个构造；生产走 auto 单例）。
# 为什么只包 prewarm、且用**非阻塞** acquire：请求路径（`vector_search` → `_rebuild_index`）
# 必须先拿到新鲜索引才返回，不能降级，它也**不碰**这个闸 ⇒ 不引入新的锁序对
# （本文件 §2026-09-14 记过一次锁序反转事故，别再添互等的锁）。
# 抢不到闸的预热线程直接退出：预热是 best-effort（docstring 自述「幂等、失败静默」），
# 请求路径仍会按需重建 = 正确性不受影响，只是少排队。
# 回滚：TRINITY_ANN_PREWARM_DEDUPE=off（每次仍各建一份，恢复改动前行为）。
_ANN_PREWARM_GATE = threading.Lock()

# 2026-09-22 §1271：**索引自愈落盘**的进程级闸。
# 为什么单独一把（不复用 `_ANN_PREWARM_GATE`）：那把闸的语义是「谁在重建索引」，
# 这把的语义是「谁在写索引文件」——两件事可以同时发生（重建完了要落盘），
# 合成一把会让预热线程在落盘期间被误判成「已在重建」而跳过。
# 非阻塞获取：抢不到说明**别人正在写同一个文件**（同进程同 pid ⇒ tmp 名相同），跳过即可；
# 真正的落盘由持有者完成，这里丢一次没有副作用。
_VEC_PERSIST_GATE = threading.Lock()

# 2026-09-23 §1274（D11 选项 C′）：**重建之间**的进程级闸（**不含读者**）。
# 语义与另两把都不同：`_ANN_PREWARM_GATE` = 「谁在预热」、`_VEC_PERSIST_GATE` = 「谁在写索引文件」，
# 这把 = 「谁在**算 embedding**」。动因：`_rebuild_index` 把长耗时的 embedding 挪到
# `self._lock` **之外**后，本来因抢锁而互相排队的多个重建者会变成**真并发** ⇒ 重复付出
# N 次 embedding、并把 ONNX 峰值内存叠起来（§1014 记过 commit 推到 177.5GB 那次；
# §1246 记过 3 个预热线程各跑一份全量、一起排在 Ollama 上）。这把闸把重建者重新串行，
# 而读者只碰 `self._lock`（发布那一瞬）⇒ 不再被堵 50–90 分钟。
# 锁序：恒为「先本闸、后 `self._lock`」（本仓 §2026-09-14 有过锁序反转事故，别添互等的锁）。
# 阻塞式获取（不是 try-acquire）：重建是请求路径正确性的一部分，不能像预热那样「抢不到就退出」。
_ANN_REBUILD_GATE = threading.Lock()


def _vec_selfheal_on() -> bool:
    """回滚开关：`TRINITY_VEC_SELFHEAL=off` ⇒ 回到「只在池脏写时落盘」（§1271 之前的行为）。"""
    try:
        return os.environ.get("TRINITY_VEC_SELFHEAL", "on").strip().lower() not in \
            ("0", "off", "false", "no")
    except Exception:  # noqa: BLE001
        return True


def _prewarm_dedupe_on() -> bool:
    try:
        return os.environ.get("TRINITY_ANN_PREWARM_DEDUPE", "on").strip().lower() not in ("0", "off", "false", "no")
    except Exception:  # noqa: BLE001
        return True


def _inline_rebuild_on() -> bool:
    """**请求路径内全量重建**的开关（默认 **off**，2026-09-29 用户授权 ①）。

    ## 为什么默认关（实测证据，不是推断）

    生产实测 `data/aggregator_vectors.pkl` 只覆盖 **1/19,347** 条 ⇒ 索引恒空
    ⇒ `vector_search` 的"冷启动自愈"分支**每个查询**都同步全量重建。代价：

    | 读数 | 值 |
    |---|---|
    | 一次查询返回 | **221,159 ms**（并发窗口） |
    | 同一查询、**单客户端** | **302,002 ms 后连接被服务器重置** |
    | 监督器处置 | `UNHEALTHY beyond grace` ⇒ **kill + restart**（19:39:23 / 19:47:41） |
    | 重建本身 | 19k 条 × 0.15–0.30 s/行 ⇒ **25–97 分钟**（本仓既有记录） |

    ⇒ 请求路径**不可能**等得起这次重建；把它留在这里等于"每次查询赌一次
    25–97 分钟的重活，赌输就把整个服务拖到被健康守卫杀掉"。

    ## 改成什么

    请求路径**只读**已有索引；索引为空就如实降级（`reason="index_rebuilding"` +
    一次性指名告警）并由融合里的其它通道兜底。重建改走**离线**路径
    （`python scripts/ann_index_persist_rebuild.py --apply`），产物由
    `__init__._load()` 在启动时恢复 —— 这正是本仓 §1271 已经设计好的那条路。

    回滚（一行）：`TRINITY_AGG_INLINE_REBUILD=on`。
    """
    try:
        return os.environ.get("TRINITY_AGG_INLINE_REBUILD", "off").strip().lower() not in \
            ("0", "off", "false", "no")
    except Exception:  # noqa: BLE001
        return False


def _prewarm_rebuild_on() -> bool:
    """**进程内后台全量重建**（`_prewarm_ann_index`）开关（默认 **off**，2026-09-29 用户授权 ①）。

    ## 为什么连后台重建也要默认关（三项独立症状，都是实测）

    它不是"请求路径"，但同样致命：19,351 条的全量重建把**串行嵌入器**占满，
    请求侧的 `fn(query)` 只能排队。三项独立症状同时指向它：

    1. 单次查询 **221,159 ms** 返回；同一查询单客户端 **302,002 ms** 后连接被重置；
    2. 判据 `ensure_warm(timeout=60)` 超时 —— `test_ann_prewarm` 在**新默认**与**回滚**
       两臂下都是 `1 failed, 39 passed`（同一条件、同一失败 ⇒ 环境性，不是本改动引入）；
    3. 受控重启后新进程 `/health` **60 s 无响应** ⇒ 监督器以 `UNHEALTHY beyond grace` kill。

    ## 改成什么

    索引由**独立进程**离线构建并落盘
    （`python scripts/ann_index_persist_rebuild.py --apply`），服务启动时
    `trinity/agents/aggregator/__init__.py::_load()` 从磁盘恢复 —— 这正是本仓 §1271
    已经设计好的路径，且不与检索争抢嵌入器。

    回滚（一行）：`TRINITY_AGG_PREWARM_REBUILD=on`。
    """
    try:
        return os.environ.get("TRINITY_AGG_PREWARM_REBUILD", "off").strip().lower() not in \
            ("0", "off", "false", "no")
    except Exception:  # noqa: BLE001
        return False


def _query_embed_timeout_s() -> float:
    """查询嵌入的**墙钟上限**（秒，默认 5.0）。0/负数 ⇒ 不限（回到改动前行为）。

    为什么要调用侧上限而不是只给 HTTP 客户端设 timeout：嵌入后端是
    `create_engine(backend="auto")`，可能是**进程内 ONNX**（无网络超时可设），
    也可能是 Ollama HTTP；而实测"卡住"发生在**排队**阶段（§1246：3 个预热线程
    各排一份全量重建在串行嵌入器上）⇒ 只有调用侧的墙钟能真正兜住。
    """
    try:
        _v = float(os.environ.get("TRINITY_AGG_QUERY_EMBED_TIMEOUT_S", "5") or 5)
        return _v if _v > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 5.0


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

# Optional FAISS import (must be bound in this module's namespace, like the
# pre-split monolith had it at module level)
try:
    import faiss  # noqa: F401
except ImportError:
    faiss = None


class _VectorMixin(_AggregatorMixinBase):

    def _prewarm_ann_index(self) -> None:
        """后台预热 ANN 向量索引（embedding 就绪后 rebuild）。

        2026-08-15 (压测优化)：生产路径索引靠 ingest 增量加，但预热期间
        ingest 跳过索引 → 首次检索可能索引空/冷启动。此线程在 embedding
        ready 后全量 rebuild（池空时短暂重试），让首次检索索引就绪。
        幂等、失败静默。
        """
        try:
            if not self._embedding_ready.wait(timeout=60):
                return
            # 池可能尚未填充：短暂等待后重试（最多 ~10s）
            for _ in range(10):
                if self._pool:
                    break
                time.sleep(1.0)
            if self._pool:
                # 2026-09-29（用户授权 ①，第二半）：**进程内全量重建默认关闭**。
                # 依据与三项实测症状见模块级 `_prewarm_rebuild_on()` 的 docstring。
                # 回滚一行：TRINITY_AGG_PREWARM_REBUILD=on。
                if not _prewarm_rebuild_on():
                    logger.info(
                        "ANN index prewarm rebuild skipped (TRINITY_AGG_PREWARM_REBUILD=off, pool=%d) "
                        "—— 离线构建：python scripts/ann_index_persist_rebuild.py --apply",
                        len(self._pool))
                    return
                # §1247：抢不到闸就**不排队**（另一个实例正在重建同一份索引）。
                _gate_held = False
                if _prewarm_dedupe_on():
                    _gate_held = _ANN_PREWARM_GATE.acquire(blocking=False)
                    if not _gate_held:
                        logger.info("ANN index prewarm skipped (another warmup is rebuilding)")
                        return
                try:
                    self._rebuild_index()
                    # §1271：**刚建好就顺手落盘**（缺文件/比池旧时才写）。
                    # 放在 `_rebuild_index` 之后、**不持任何锁**的位置：`_save()` 自己会拿
                    # `self._lock` 做快照（§1027：慢 I/O 不许在别人的临界区里做）。
                    self._persist_index_if_missing()
                finally:
                    if _gate_held:
                        _ANN_PREWARM_GATE.release()
                logger.info("ANN index prewarmed (%d vectors)", len(self._index_id_map))
        except Exception as _e:
            swallow(__name__, _e)

    def _prewarm_embedding(self) -> None:
        """后台预热 embedding 引擎（sklearn 首次 fit 较慢，移到启动期）。

        2026-08-15 (压测优化)：避免首次 ingest 触发 10s 级冷启动。
        预热完成后置 ready 标记；失败静默（后续 ingest 仍惰性初始化）。
        """
        try:
            self._get_embedding_fn()
            self._get_embedding_fn()("预热")
            logger.info("embedding prewarmed (dim=%d)", self._vector_dim)
        except Exception as _e:
            swallow(__name__, _e)
        finally:
            self._embedding_ready.set()

    def _get_embedding_fn(self):
        """Lazy-init embedding callable via trinity.embeddings or hash fallback.

        2026-08-15 (压测优化)：backend 从 "auto" 改为 "sklearn"——auto 会先探测
        Ollama（本机未开时每次 embed 等 ~300ms 超时，是写入 p50 2s 的根因）。
        sklearn TF-IDF 确定性、毫秒级，写入路径提速 ~10x。

        2026-09-14（flake 修复：aggregator._factory Test 15 间歇性 FAIL）
        ────────────────────────────────────────────────────────────────
        原实现有三个缺陷，叠加后使 `_vector_dim` 与 `_embedding_fn` 可能来自
        **两个不同的拟合**，于是 `_add_to_index`（旧 L136
        `vec.shape[1] != self._vector_dim`）对**每一条**记忆提前 return，
        索引恒为空：pool>0 但 _index_id_map==[] → vector_search 返回 0 条
        → Test 15 断言
          "vector index empty after ensure (pool=N, idx=0, dim=D)" 失败。

        实测证据（Python314 + sklearn backend，本机复跑）：
          SklearnEmbeddingEngine._lazy_init 用**首个传入文本**拟合
          TfidfVectorizer（trinity/embeddings/engine.py L405-411），故 embed()
          宽度 = 首个文本决定的词表大小：
              embed("test")                               -> dim 9
              embed("user prefers dark mode in all...")   -> dim 57
              embed(默认语料)                              -> dim 4
          宽度在同一 engine 内此后冻结，但**两次 create_engine 宽度可不同**。

        缺陷 1（竞态，本次复发主因）：本方法原先无锁。`agg-embed-prewarm`
          线程（_init.py L172）与主线程（ingest → _add_to_index）会并发进入，
          各自建 engine 并各自覆写 `_vector_dim`/`_embedding_fn`，交错后二者
          可能来自不同的 engine。
        缺陷 2（回退分支不自洽）：原 except 分支只换 `_embedding_fn`（hash，
          384 维），**未重置 `_vector_dim`**；若此前成功探测已把它写成 9，
          就得到 dim=9 + fn 输出 384，同样全量跳过。
        缺陷 3（探测不可靠）：用 `embed("test")` 探测宽度，而 "test" 的拟合
          结果取决于它是否首个文本，不是稳定契约。

        修复：用**专用初始化锁** `_embedding_init_lock`（不与 self._lock 互相嵌套）
        做双检；`_vector_dim` 与 `_embedding_fn` 由同一次调用、同一个 engine 产出，
        并以**单个元组原子发布**，任何读者都只会看到自洽的一对；回退分支显式把
        `_vector_dim` 重置为该回退实现的真实宽度 384；并对实际调用宽度做一次
        自洽校验，不一致即整体回退。

        2026-09-14 追加（锁序修复，必须保留）：**禁止在本方法内持有 `self._lock`**。
        首版修复曾用 `with self._lock:` 包住整段初始化，造成锁序反转死锁：
        主线程执行 `import trinity` 时持有 **import lock** 并等待 `self._lock`
        （auto_discovery → `_stats.statistics()` L222 `with self._lock`），
        而 `agg-embed-prewarm` 线程持有 `self._lock` 并在 `create_engine` 的
        惰性 import 上等待 **import lock** → 双方互等、进程永久挂起
        （实测 `import trinity` 70s 无进展，faulthandler 显示上述两帧）。
        因此本方法只使用 `_embedding_init_lock`，绝不触碰 `self._lock`。
        """
        # 快速路径：已就绪则直接返回（读单个引用是原子的，无需加锁）
        fn = self._embedding_fn
        if fn is not None:
            return fn
        # 慢路径：用专用锁串行化初始化。**不得**使用 self._lock（锁序反转）。
        init_lock = getattr(self, "_embedding_init_lock", None)
        if init_lock is None:
            # 极端情形（未走 __init__ 直接调用）：就地补一个，避免 AttributeError
            init_lock = threading.Lock()
            self._embedding_init_lock = init_lock
        with init_lock:
            if self._embedding_fn is not None:   # 双检：别的线程已完成
                return self._embedding_fn
            hash_dim = int(getattr(self._hash_embed, "__defaults__", (384,))[0])
            try:
                from trinity.embeddings import create_engine
                # 2026-09-20（Marvis 修复 A）：原为 backend="sklearn"（TF-IDF）。
                # 缺陷：真正调用时以首次 probe 文本 "test" 触发 fit ⇒ 词表被
                # 单条文本锁死，后续 19k+ 条记忆 transform 后几乎全为 0 向量，
                # 检索无区分度（不同查询返回同一批记忆）。
                # 改走 auto（bge-m3：ONNX 进程内 / Ollama HTTP），输出 1024 维
                # 真语义向量；auto 不可用时其内部链路仍会降级，外层 except 保底
                # 回退 hash 384 维。
                _eng = create_engine(backend="auto")
                # 以该 engine 的**真实输出宽度**为唯一依据（而非任何假定值）：
                probe = _eng.embed("test")
                _dim = len(probe) if isinstance(probe, (list, np.ndarray)) else None
                if not _dim:
                    raise ValueError(f"embedding probe gave unusable width: {probe!r}")
                def _fn(text):  # t76：原为 lambda（曾被一条 E731 抑制指令压掉）；改 def 去掉抑制，语义不变
                    return np.array(_eng.embed(text), dtype=np.float32)
                _actual = len(_fn("test"))
                if _actual != _dim:
                    raise ValueError(
                        f"embedding width inconsistent: probe={_dim} actual={_actual}"
                    )
                # 成对赋值：二者同源，且在同一临界区内连续写入，读者不会看到
                # "新维度 + 旧 fn" 的中间态。
                self._vector_dim = _dim
                self._embedding_fn = _fn
                # 2026-09-15（P0 冷启动）**批量入口与逐条 fn 同源发布**。
                # 动机：`_rebuild_index` 逐条调用 `_add_to_index` ⇒ 池内**每条**
                # 记忆各触发一次 `TfidfVectorizer.transform`，而 sklearn 每次调用
                # 都有固定开销（check_array / narwhals / inspect / 参数校验）。
                # 实测（池 10,817 条，Python314，cProfile 真实路径）：首次
                # `search_hybrid` 的耗时 **≈100%** 落在这条逐条链上。
                # `embed_batch` 走**同一次** transform；词表此刻已由上方 probe
                # 拟合冻结，故每行向量与逐条路径**逐位一致**，只是省掉 N-1 次
                # 调用固定开销。不可用时置 None，`_rebuild_index` 自动回落逐条。
                _eb = getattr(_eng, "embed_batch", None)
                self._embedding_batch_fn = (
                    (lambda texts: np.asarray(_eb(list(texts)), dtype=np.float32))
                    if callable(_eb) else None
                )
            except Exception as exc:
                logger.info(
                    "embeddings module unavailable; using hash-based pseudo-vectors (%s)", exc
                )
                # 缺陷 2 修复：回退时**必须**同步重置维度，否则残留的探测值
                # （如 9）与 hash 输出的 384 维不匹配 → 索引全量跳过。
                self._vector_dim = hash_dim
                self._embedding_fn = self._hash_embed
                # 回退分支同样要成对重置批量入口，避免读者拿到残留的 sklearn 批量 fn
                self._embedding_batch_fn = None
            return self._embedding_fn

    @staticmethod
    def _hash_embed(text: str, dim: int = 384) -> np.ndarray:
        """确定性伪向量（内容哈希回退）。

        2026-09-15（R41-P23）：**改用 sha256 派生**。原实现用 Python 内建
        `hash(text)`，而 str 哈希**每进程加盐随机化**（PYTHONHASHSEED）——
        实测（temp/_probe_hash_embed.py）同一文本在 3 个进程得到 **3 个不同向量**，
        只有固定种子才一致。后果：一旦 embeddings 模块不可用而走本回退分支
        （`_get_embedding_fn` 的 except 路径），聚合器 ANN 索引**每个进程都不一样**
        ⇒ 聚合池向量检索结果**不可复现**，与本仓的可证明性主张冲突；且任何
        跨进程复用的索引在该向量空间里都无意义。
        另：原实现把**单个** hash 的 32 位按 `i % 32` 循环填充 dim 维 ⇒ 分量以 32
        为周期重复、熵仅 32 位。现按 sha256 摘要逐位取符号，counter 模式扩展到
        任意 dim，周期消失。

        契约不变：dim 维、单位范数（norm>0 时归一化），且
        `__defaults__[0] == 384`——`_get_embedding_fn` 依赖它推断回退宽度。
        """
        import hashlib
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        vec = np.empty(dim, dtype=np.float32)
        pos = 0
        counter = 0
        while pos < dim:
            block = digest if counter == 0 else hashlib.sha256(
                digest + bytes([counter & 0xFF])).digest()
            for byte in block:
                for shift in range(8):
                    if pos >= dim:
                        break
                    vec[pos] = 1.0 if (byte >> shift) & 1 else -1.0
                    pos += 1
                if pos >= dim:
                    break
            counter += 1
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def _add_to_index(self, dv: DimensionVector) -> None:
        """Add a DimensionVector's embedding to the vector index.

        不变式（2026-09-14 专项根治，必须保持）：
          **所有对 `_faiss_index` / `_index_id_map` / `_embedding_dim_locked`
          的读改写都在 `self._lock` 临界区内完成**，且容器与 id 列表始终
          作为**一对**发布。

        旧实现的真实缺陷（整链 Test 15 间歇性 FAIL 的根因，此前两轮修复
        都未命中）：
          `_rebuild_index()` 在**持锁前**执行
          `self._faiss_index = None; self._index_id_map = []`（L242-243），
          而 `_add_to_index` 却**不持锁**向已建好的容器 append。
          `agg-ann-prewarm` 对 12743 条池数据做全量重建期间（实测持锁
          ~27s，见下）主线程并发写入，会出现：
              主线程 append 完 → prewarm 把 _index_id_map 清成 [] →
              `vector_search` 看到 idx=0 → 返回 []
          Test 15 断言 `pool=N, idx=0, dim=9` 正是这一帧。
          （旧注释把原因归给"宽度不一致"，实测宽度始终自洽 =9；
            真正被丢弃的是**已建成的索引内容**。）

        修法：把整段"按宽度建/换容器 + append + 记账"收进临界区，
        使 `_index_id_map` 长度与容器行数永远一致、且不会被重建线程
        在中间态抹掉。
        """
        # 2026-08-15 (压测优化)：embedding 未预热完成时不阻塞写入——
        # 跳过本次索引（后续 _rebuild_index 全量重建补齐）。
        #
        # 2026-09-29（用户授权 ②）：**静默跳过是这条事故链的第一环**。
        # 实测：未就绪时逐条调用 `_add_to_index` —— **0 行落地、0 报错、0 日志**；
        # 若此时 `_rebuild_index` 正在跑，它产出的就是一个**近乎空的索引**
        # （实测 2/19,354 行），随后被 `_save()` 落盘并覆盖好索引。
        # 行为不变（仍跳过），但**必须留痕**：计数 + 一次性指名告警。
        if not getattr(self, "_embedding_ready", None) or not self._embedding_ready.is_set():
            _sk = "_add_to_index_skipped_not_ready"
            setattr(self, _sk, getattr(self, _sk, 0) + 1)
            if not getattr(self, "_add_to_index_not_ready_warned", False):
                self._add_to_index_not_ready_warned = True
                logger.warning(
                    "embedding 未就绪 ⇒ `_add_to_index` 逐条静默跳过（已计数 %s，此后不再刷屏）。"
                    "若这发生在全量重建期间，产出将是**近乎空的索引** —— 见 "
                    "_rebuild_index / _save 的『近空索引拒绝发布/拒绝落盘』守卫（2026-09-29 ②）",
                    _sk)
            return
        # 2026-09-14（flake 修复）：以 fn 的**实际输出宽度**为准建立/续建索引，
        # 不再拿可能过期的 self._vector_dim 做静默跳过。旧逻辑一旦
        # _vector_dim 与 fn 宽度不一致，就会跳过**所有**向量、索引恒空
        # （pool>0 而 idx=0），即 Test 15 间歇性 FAIL 的直接现象。
        # 不变式：索引容器的宽度 == 当前 fn 的输出宽度。
        # 注意：embedding 计算放在锁外（可能较慢，且 _get_embedding_fn 有
        # 自己的专用锁，嵌套会引入锁序风险）；只有状态改写进锁。
        fn = self._get_embedding_fn()
        vec = fn(dv.content).reshape(1, -1).astype(np.float32)
        width = int(vec.shape[1])
        with self._lock:
            if getattr(self, "_embedding_dim_locked", None) != width:
                # 首次记账，或宽度发生变化 → 丢弃旧容器，按新宽度重建
                self._vector_dim = width
                self._embedding_dim_locked = width
                self._faiss_index = None
                self._index_id_map = []

            if _HAS_FAISS:
                if self._faiss_index is None:
                    self._faiss_index = faiss.IndexFlatIP(width)
                    self._index_id_map = []
                self._faiss_index.add(vec)
            else:
                # numpy fallback: store raw vectors
                if self._faiss_index is None:
                    self._faiss_index = np.empty((0, width), dtype=np.float32)
                    self._index_id_map = []
                self._faiss_index = np.vstack([self._faiss_index, vec])
            self._index_id_map.append(dv.memory_id)

    def _drop_vector_row(self, memory_id: str) -> bool:
        """把某个 id 从**向量索引**里摘掉（**不动池、不动其它索引**）。返回是否真摘掉了。

        用途（§1267）：`merge_memories` 把被合并者的正文追加进 keeper ⇒ keeper 的向量**过期**。
        重算它需要重新 embed；若为此做一次**全量**重建，在 19k 池上就是 §1019/§1024 那条
        「重建期间健康探测超时、被 supervisor 判死」的路径。摘掉这一行之后，**下一次重建的
        增量路径**会把它当作「池里有、索引里没有」的新条目补回来（成本与新增量成正比）。
        与 `_remove_from_pool` 的索引部分同构，但**只碰索引**（池里那一条必须留着 ——
        它是合并后的 keeper）。§1268 起 `_remove_from_pool` **也**持 `self._lock`
        且**复用本方法**（索引手术只留一份），两者共用同一把可重入锁。
        """
        with self._lock:
            if memory_id not in self._index_id_map:
                return False
            idx = self._index_id_map.index(memory_id)
            if _HAS_FAISS and self._faiss_index is not None:
                self._faiss_index.remove_ids(np.array([idx], dtype=np.int64))
            elif self._faiss_index is not None:
                self._faiss_index = np.delete(self._faiss_index, idx, axis=0)
            self._index_id_map.pop(idx)
            return True

    def _try_incremental_index(self) -> bool:
        """§1025/§1028：只把「不在 _index_id_map 里」的新条目 embed 后 add 进现有索引。

        返回 True = 本函数已处理。任何前提不满足都返回 False，由全量路径兜底。

        §1028 关键修正（锁车队）：原实现把「分批 ONNX 计算」也放在 self._lock 里，
        于是刷新期间这把锁被持住数秒；而同锁的 /metrics -> statistics()（O(池) 统计）与
        需要聚合器的 /health 只能排队 ⇒ 健康探测超时 ⇒ supervisor 判不服务并杀（§1027 现场栈实证）。
        现在：锁内只取快照、只做 append，计算全部在锁外。
        """
        with self._lock:                       # 快照（快）
            idx = self._faiss_index
            ids = list(self._index_id_map)
            if idx is None or not ids:
                return False
            _known = set(ids)
            # §1258：索引里有**池里已不存在**的 id ⇒ 必须返回 False 交给全量路径清掉。
            # 理由：增量只会「加」，那些行会永远留着。`_search.py` 事后按 `mid in self._pool`
            # 过滤，所以检索结果不至于错，但那些行会白占 top_k 的名额（召回被稀释）。
            # 为什么会有这种行：`_ingest.py::merge_memories` 合并后删过池（该处已按 §1258
            # 改走 `_remove_from_pool`）；而**维护链的 `_remove_from_pool` 自己就摘了索引行**
            # ⇒ 不命中本条件，故 §1025 要省的「纯追加」场景一点不受影响。
            if not _known <= self._pool.keys():
                return False
            _new = [dv for dv in self._pool.values() if dv.memory_id not in _known]
        if not _new:
            return True                        # 索引已覆盖池内全部条目
        _bfn = getattr(self, "_embedding_batch_fn", None)
        if _bfn is None:
            return False
        _texts = [dv.content for dv in _new]
        _mats = []
        for _s in range(0, len(_texts), _ANN_EMBED_CHUNK):   # 锁外算（本修正的要点）
            _mats.append(np.asarray(
                _bfn(_texts[_s:_s + _ANN_EMBED_CHUNK]), dtype=np.float32))
        mat = np.ascontiguousarray(np.vstack(_mats), dtype=np.float32)
        if mat.ndim != 2 or mat.shape[0] != len(_new) or mat.shape[1] != int(self._vector_dim or 0):
            return False
        with self._lock:                       # 只在 append + 延长 id_map 时持锁
            if self._faiss_index is not idx:
                return False                   # 期间索引被别人换掉 ⇒ 放弃这次（下次再来）
            if _HAS_FAISS:
                idx.add(mat)
            else:
                self._faiss_index = np.ascontiguousarray(np.vstack([idx, mat]), dtype=np.float32)
            self._index_id_map = ids + [dv.memory_id for dv in _new]
        logger.info("ANN index extended incrementally (+%d vectors, total=%d)",
                    len(_new), len(self._index_id_map))
        return True


    def _persist_index_if_missing(self, defer: bool = False) -> None:
        """§1271：**索引刚建好 ⇒ 顺手把它落盘**（文件缺失、或比池文件旧时）。

        ## 为什么（这是 D10 选项 B 的**接线**，但接线方式换了）

        §1270 只把「池写了、索引静默没写」变成**可见**，没解决「谁来补」：
        `_save()` 由**池的脏写**触发，而写池的进程（worker / 维护链）常常**没有热索引**
        ⇒ 索引文件一旦丢失，持有完整索引的检索进程**不会**因为「我刚建好索引」而落盘。

        D10 原本的选项 B（挂维护链跑一次 50–90 分钟的重活）在**尾段 1200s 预算**下
        **根本跑不完** —— 本文件上方 §812-§820 就是「38 个任务挤 1800s 被系统性饿死」的前科，
        不能重蹈。换成：**把「刚建好索引」这个唯一确定拥有完整索引的时刻用起来** ——
        成本 ≈ 一次 74MB 写（秒级），与 50–90 分钟的重新 embed 完全不是一个量级。

        ## 判据（可失败）

        `tests/unit/test_vector_persist_honesty.py`：
          · 索引文件缺失 ⇒ 写盘（`persisted_after_rebuild` +1）；
          · 文件**比池新** ⇒ **故意不写**（`skipped_fresh` +1）—— 不许每次重建都写 ~150MB；
          · 文件**比池旧** ⇒ 写盘；
          · 反事实：`TRINITY_VEC_SELFHEAL=off` ⇒ 三种情形都不写（回到 §1271 之前）。

        ## 安全

        全程 try/except（`swallow` + `selfheal_error` 计数）：**持久化失败不许影响检索** ——
        这与 `_prewarm_ann_index` 的「幂等、失败静默」同一哲学，区别是这里**留痕**。
        抢不到 `_VEC_PERSIST_GATE` 就直接返回：说明同进程已有人在写同一个文件
        （tmp 名带 pid ⇒ 并发写会撞车），丢这一次没有副作用 —— 持有者会写完.

        ## §1272 修：请求路径必须 `defer=True`

        `query()` 是在 `with self._lock:` **之内**调 `vector_search()` 的
        （`_search.py` L83 → L113，缩进 16 ⇒ 在锁内；`_lock` 是 RLock ⇒ 内层重入成功），
        所以请求路径上的自愈会**持着调用方的锁**写 74MB ⇒ 破坏「持锁不做好 I/O」（§1027）。
        ⇒ 请求路径传 `defer=True`（交给一次性守护线程，调用方立刻返回），
        预热线程走内联（它不持别人的锁）。
        """
        if defer:
            threading.Thread(target=self._persist_index_if_missing, daemon=True,
                             name="agg-vec-persist-once").start()
            return
        if not _vec_selfheal_on():
            return
        from . import VECTOR_PERSIST_STATS      # 惰性：包 __init__ 导入本模块，模块级导入会成环
        try:
            path = getattr(self, "_persist_path", None)
            if not path or self._faiss_index is None or not self._index_id_map:
                return
            vec = os.path.join(os.path.dirname(path), VECTOR_PERSIST_FILENAME)
            if os.path.exists(vec) and os.path.getmtime(vec) >= os.path.getmtime(path):
                VECTOR_PERSIST_STATS["skipped_fresh"] += 1
                return
            if not _VEC_PERSIST_GATE.acquire(blocking=False):
                return
            try:
                self._save()          # 复用既有落盘实现（池 + 索引 + 原子替换 + 重试都在里面）
                VECTOR_PERSIST_STATS["persisted_after_rebuild"] += 1
                logger.info("vector index persisted after rebuild (%d vectors)",
                            len(self._index_id_map))
            finally:
                _VEC_PERSIST_GATE.release()
        except Exception as _e:  # noqa: BLE001
            VECTOR_PERSIST_STATS["selfheal_error"] += 1
            swallow(__name__, _e)

    def _rebuild_index(self) -> None:
        """Rebuild vector index from all pool memories.

        §1025：**先试增量**（避免下面的全量重置把好索引清掉）—— 那一段是本方法体的
        第一段代码；试不成（返回 False / 抛异常）再走下面的全量重置。

        2026-09-14（flake 修复）：宽度记账一并重置，使本次重建按
        `_get_embedding_fn()` 的**当前**输出宽度重新建立索引。此前若
        `_vector_dim` 是竞态残留值，重建会产出 0 行索引（pool>0, idx=0），
        正是 aggregator._factory Test 15 间歇性 FAIL 的直接现象。

        2026-09-14 追加（专项根治）：容器与 id 列表的清空**必须**与
        后续逐条填充处于**同一个** `self._lock` 临界区内，且整段填充
        对读者表现为"要么旧索引、要么完整新索引"。旧实现在锁外先清空，
        于是 prewarm 线程全量重建（12743 条，持锁数十秒）期间，任何
        并发 `vector_search` 都会看到 `idx=0` 的空索引——这正是整链
        环境下 Test 15 偶发失败的那一帧。

        2026-09-15（P0 冷启动）：**批量重建优先**。上述"同一临界区、
        要么旧要么全新"的不变式**原样保留**（清空 → 填充 → 发布仍在
        单次持锁内完成，读者不会看到中间态）。改的只是填充方式：
        若有批量入口，则一次 `embed_batch`（分块）+ **一次** `add()`，
        取代逐条 `_add_to_index` 的 N 次 sklearn transform 固定开销。
        向量本身与逐条路径逐位一致（同一词表、同一归一化），故检索
        结果不变；批量入口不可用时自动回落原逐条循环，零行为差异。
        """
        # 2026-09-21 §1025：**先试增量**（避免下面的全量重置把好索引清掉）。
        # 动机：ANN 全量重建在 19k+ 条池上是重活（小批量 64 条 × ~300 次 ONNX 调用），
        # 期间 API 健康探测超时被 supervisor 判死（§1019/§1024 实测每 6 分钟一轮）。
        # 增量只 embed「不在索引里」的新条目 ⇒ 重建成本与**新增量**成正比，而不是与池大小成正比。
        # 回滚：TRINITY_ANN_INCREMENTAL=0（强制走下面的原全量路径）。
        #
        # ⚠️ 2026-09-22 §1258：这段代码**原先被整段写在上面那段 docstring 里**
        # （三引号闭合之前），于是 `_ANN_INCREMENTAL` 从未被读取 —— 开关是死的、
        # §1025 的修复从未生效（症状：`agg-ann-prewarm` 仍每轮全量重建，§1246 全线程快照里
        # 3 个预热线程各自跑一份全量、一起排在 Ollama 上）。抓出它的是
        # `scripts/env_audit.py::find_dead_reads` 的 DEAD_B 判据（「赋给从未 Load 的名字」）
        # 经 `tests/unit/test_surface_freeze_scope.py` 报红 —— **那不是误报**。
        # 别把这段挪回 docstring，也别用字符串常量绕过那条判据；
        # 回归判据：tests/unit/test_ann_incremental.py::test_开关在函数体里被真正读取。
        # 闸在**最前面**（连增量判定一起包）：两路并发时后到的那一路在闸上等，等到的是一份
        # **已经发布好的新索引** ⇒ 它的增量判定命中「没有新条目」⇒ 0 次额外 embedding。
        # 这才是 §1270 那条「并发不得重复劳动」的**设计**来源：旧实现靠的是「第二路在
        # `self._lock` 上排队、等第一路发布完再判增量」这个**副作用**；把 embedding 挪出锁之后
        # 副作用消失（第二路会在陈旧索引上判增量），所以串行点必须前移到这把闸上。
        # 判据：`tests/unit/test_ann_incremental.py::test_并发进入重建不得重复劳动`
        # （含反事实：增量 off ⇒ 必须真的重复一遍 = 4 批）。
        with _ANN_REBUILD_GATE:
            # 2026-09-21 §1025：**先试增量**（避免下面的全量重置把好索引清掉）。
            # 动机：ANN 全量重建在 19k+ 条池上是重活（小批量 64 条 × ~300 次 ONNX 调用），
            # 期间 API 健康探测超时被 supervisor 判死（§1019/§1024 实测每 6 分钟一轮）。
            # 增量只 embed「不在索引里」的新条目 ⇒ 重建成本与**新增量**成正比，而不是与池大小成正比。
            # 回滚：TRINITY_ANN_INCREMENTAL=0（强制走下面的原全量路径）。
            #
            # ⚠️ 2026-09-22 §1258：这段代码**原先被整段写在上面那段 docstring 里**
            # （三引号闭合之前），于是 `_ANN_INCREMENTAL` 从未被读取 —— 开关是死的、
            # §1025 的修复从未生效（症状：`agg-ann-prewarm` 仍每轮全量重建，§1246 全线程快照里
            # 3 个预热线程各自跑一份全量、一起排在 Ollama 上）。抓出它的是
            # `scripts/env_audit.py::find_dead_reads` 的 DEAD_B 判据（「赋给从未 Load 的名字」）
            # 经 `tests/unit/test_surface_freeze_scope.py` 报红 —— **那不是误报**。
            # 别把这段挪回 docstring，也别用字符串常量绕过那条判据；
            # 回归判据：tests/unit/test_ann_incremental.py::test_开关在函数体里被真正读取。
            if _ANN_INCREMENTAL:
                try:
                    if self._try_incremental_index():
                        return
                except Exception as _e:  # noqa: BLE001
                    swallow(__name__, _e)
            # ── 2026-09-23 §1274（D11 选项 C′ 施工）：**锁外算 embedding、锁内原子发布** ──
            # 动因（§1272/§1273 实测）：下面这段原先是**一个** `with self._lock:` 一路包到分块
            # embedding 结束 ⇒ 冷索引时那次全量重建是**持着聚合器锁**做的；夹具实测别的线程
            # 0.5s 内拿不到 `_lock`、`statistics()`（`/metrics` 与 `/health` 的消费者）被堵 0.68s，
            # 而真实规模（§1270：19,313 条 × 0.15–0.30 s/行）下这个数就是**剩余重建时间 50–90 分钟**
            # —— 这正是「健康探测判死、每 6 分钟一轮」（§1019/§1024）与「API 停摆约 20 分钟」（§1184）的机制。
            #
            # 改法与**不变式**：
            #   · 读者仍只看到「**旧索引**」或「**完整新索引**」（严格强于旧实现：旧实现在重建一开始
            #     就把 `_faiss_index` 清成 None，读者在整段重建期间看到的是**空索引**）；
            #   · `self._lock` 只被占**发布那一瞬**（三个字段一次性换新）；
            #   · 重建者之间由 `_ANN_REBUILD_GATE` 串行（读者**不碰**它），锁序恒为
            #     「先 gate、后 self._lock」，与 `_ANN_PREWARM_GATE` 同向。
            # **代价**（D11 已登记、动手前认下）：`items` 快照与发布之间池可能变化 ⇒ 新索引可能
            # **滞后**于池，缺的条目由既有**增量路径**补回（§1267 修的就是那条）。
            # 回滚：把下面两段合并回「单临界区」即可（快照 output/_pre_edit_snapshots_20260923/）。
            with self._lock:
                items = list(self._pool.values())
            _bfn = getattr(self, "_embedding_batch_fn", None)
            _published = None  # (index_obj_or_mat, id_map, width)
            if _bfn is not None and items:
                try:
                    # 分块：避免超大池一次性物化 (N × dim) 稠密矩阵
                    #
                    # 2026-09-21 §1014 **事故修复（P0）**：原分块大小 4096 只约束了
                    # 「输出矩阵」的大小，**完全没有约束 embedding 本身的峰值内存** ——
                    # 4096 条文本一次性送进 ONNX Runtime 的 transformer（bge-m3 级），
                    # 注意力激活约 O(batch × heads × seq²)，实测把 API 进程的 **commit
                    # 推到 177.5GB、系统 commit-free 归零**（py-spy 现场栈：
                    # _prewarm_ann_index → _rebuild_index → embed_batch →
                    # onnxruntime InferenceSession.run），机器两次濒临崩溃。
                    # 修法：把「送进模型的批量」压到 _ANN_EMBED_CHUNK（默认 64，
                    # 环境变量 TRINITY_ANN_EMBED_CHUNK 可调）——内存峰值随批量近似线性下降
                    # （4096→64 即 ~64×），而**每条文本的向量与批量大小无关**，故检索结果不变
                    # （与本函数上方「批量入口与逐条 fn 逐位一致」的既有论证同一依据）。
                    # 回滚：TRINITY_ANN_EMBED_CHUNK=4096 恢复旧行为。
                    _mats = []
                    for _s in range(0, len(items), _ANN_EMBED_CHUNK):
                        _mats.append(np.asarray(
                            _bfn([dv.content for dv in items[_s:_s + _ANN_EMBED_CHUNK]]),
                            dtype=np.float32))
                    mat = np.ascontiguousarray(np.vstack(_mats), dtype=np.float32)
                    if mat.ndim == 2 and mat.shape[0] == len(items) and mat.shape[1] > 0:
                        _width = int(mat.shape[1])
                        # faiss 的构建（IndexFlatIP + add）也在锁外：19k×1024 float32 ≈ 78MB 的
                        # 一次拷贝，实测毫秒级；**只有在发布那一刻**才让读者看到它。
                        if _HAS_FAISS:
                            _idx = faiss.IndexFlatIP(_width)
                            _idx.add(mat)
                        else:
                            _idx = mat
                        _published = (_idx, [dv.memory_id for dv in items], _width)
                except Exception as _e:  # noqa: BLE001
                    swallow(__name__, _e)
            with self._lock:
                if _published is not None:
                    # 2026-09-29（用户授权 ②）：**拒绝发布"近乎空的索引"**。
                    # 它由 `_add_to_index` 的就绪守卫静默产生（实测 2/19,354 行），
                    # 一旦发布就会被 `_save()` 落盘，覆盖掉磁盘上的好索引
                    # ⇒ 之后每次启动都判 `idx=2 vs pool=19354` 并丢弃 ⇒ 通道恒 0。
                    # 处置：**保留上一份索引**（不发布、不落盘），并响亮留痕。
                    if (len(items) >= MIN_ROWS_FOR_INDEX_RATIO
                            and len(_published[1]) < len(items) * MIN_INDEX_LANDING_RATIO):
                        logger.warning(
                            "**拒绝发布近乎空的索引**：池 %d 行只产出 %d 行（< %.0f%%）⇒ "
                            "保留上一份索引、且不落盘（宁可暂时没有索引，也不覆盖好索引）。"
                            "根因通常是 embedding 未就绪时 _add_to_index 逐行跳过。",
                            len(items), len(_published[1]), MIN_INDEX_LANDING_RATIO * 100)
                        return
                    self._faiss_index, self._index_id_map, _w = _published
                    self._vector_dim = _w
                    self._embedding_dim_locked = _w
                    logger.info("ANN index rebuilt in batch (%d vectors, dim=%d)",
                                len(self._index_id_map), _w)
                    return
                # 批量入口不可用 / 形状可疑 / 池为空 ⇒ 维持原「清空 → 逐条填充」的单临界区语义
                # （这条回落路径**逐条**改 `_faiss_index`，读者不能看到中间态，故仍整段持锁）。
                # 2026-09-29（②）：先存一份，落地率过低时**回滚**，不让一次失败的重建抹掉好索引。
                _prev = (self._faiss_index, self._index_id_map,
                         self._vector_dim, self._embedding_dim_locked)
                self._faiss_index = None
                self._index_id_map = []
                self._embedding_dim_locked = None
                for dv in items:
                    self._add_to_index(dv)
                if (len(items) >= MIN_ROWS_FOR_INDEX_RATIO
                        and len(self._index_id_map) < len(items) * MIN_INDEX_LANDING_RATIO):
                    self._faiss_index, self._index_id_map, self._vector_dim, \
                        self._embedding_dim_locked = _prev
                    logger.warning(
                        "**逐条重建落地率过低 ⇒ 回滚到上一份索引**：池 %d 行只落地 %d 行"
                        "（< %.0f%%），已恢复原索引、不落盘。",
                        len(items), len(_prev[1] or []), MIN_INDEX_LANDING_RATIO * 100)

    def _embed_query_bounded(self, fn, query: str):
        """在**有界时间内**算查询嵌入；超时返回 None（2026-09-29，用户授权 ①）。

        实测量级：单次查询 221,159 ms 返回（并发窗口）/ 302,002 ms 后连接被重置；
        profiler 显示时间花在**共享串行嵌入器**的排队上（§1246/§1272 已有记录）。

        上限用 `TRINITY_AGG_QUERY_EMBED_TIMEOUT_S`（默认 5.0s，0=不限）。

        ## 如实记录代价（不做"看起来完美"的假象）

        Python 无法安全杀线程 ⇒ 超时后那个嵌入调用仍在后台跑完，只是**调用方不再等它**。
        这换来的是"检索不被单个通道拖死"，代价是极端情况下会多一个在途嵌入请求；
        用一次性告警避免刷屏，并把原因写进 `vector_search_status().reason`。
        """
        _t = _query_embed_timeout_s()
        if _t <= 0:
            return fn(query)
        _box: dict = {}

        def _run() -> None:
            try:
                _box["v"] = fn(query)
            except Exception as _e:  # noqa: BLE001 由调用侧原样抛出（保持改动前语义）
                _box["e"] = _e

        _th = threading.Thread(target=_run, daemon=True, name="agg-query-embed")
        _th.start()
        _th.join(_t)
        if _th.is_alive():
            if not getattr(self, "_query_embed_timeout_warned", False):
                self._query_embed_timeout_warned = True
                logger.warning(
                    "查询嵌入超过 %.1fs 未返回 ⇒ 本次按『该通道不可用』降级（不让它拖死检索）。"
                    "根因通常是共享嵌入器被占满（全量索引重建）；离线重建入口："
                    "python scripts/ann_index_persist_rebuild.py --apply。"
                    "调整上限：TRINITY_AGG_QUERY_EMBED_TIMEOUT_S（当前 %.1f）", _t, _t)
            return None
        if "e" in _box:
            raise _box["e"]
        return _box.get("v")

    def _note_index_missing_once(self) -> None:
        """索引为空且请求路径**不再重建** ⇒ 一次性指名告警（含补救入口与回滚开关）。

        与 `_emb_ready_missing_warned` 同一哲学：**事实要留痕，但只喊一次**
        （否则每个查询刷一条，真告警会被自己淹掉）。
        """
        if getattr(self, "_idx_missing_warned", False):
            return
        self._idx_missing_warned = True
        logger.warning(
            "聚合器向量索引为空（pool=%d）⇒ 本次检索按『索引重建中』降级返回空；"
            "**请求路径不再同步重建**（实测 19k 条重建 25–97 分钟，曾把单次查询拖到 "
            "221–302 s 并被健康守卫以 UNHEALTHY 超宽限 kill 进程）。"
            "离线补救：python scripts/ann_index_persist_rebuild.py --apply"
            "（完成后重启服务，启动时从磁盘加载）。要恢复旧行为："
            "TRINITY_AGG_INLINE_REBUILD=on", len(getattr(self, "_pool", {}) or {}))

    def _vs_note(self, reason: str, n: int = 0) -> None:
        """记录最近一次 vector_search 的三态状态（A2 降级诚实性）。

        动机：本方法的两条"返回空"路径——**嵌入/索引未就绪** 与 **确实没有匹配**——
        此前都表现为裸 `[]`，调用方无法区分，静默降级无人可见。
        本状态**只标注、不改返回内容**（与 trinity/retrieval/evidence_gate.py 同款哲学）。
        """
        self._last_vs_status = {
            "attempted": True,
            "succeeded": reason == "ok",
            "failed": 0 if reason == "ok" else 1,
            "reason": reason,
            "results": int(n),
        }

    def vector_search_status(self) -> dict:
        """读取最近一次 vector_search 的状态（A2）。

        reason 取值：
          - ``ok``                  检索成功
          - ``index_not_ready``     嵌入尚未就绪（**此前与"没结果"无法区分**）
          - ``no_index``            已就绪但索引为空（确实没有可检索内容）
          - ``index_rebuilding``    索引为空**且请求路径不再重建**（2026-09-29 ①）——
                                    可恢复降级：等离线重建落盘 + 重启即可用
          - ``query_embed_timeout`` 查询嵌入超过墙钟上限（2026-09-29 ①，共享嵌入器被占满）
        """
        return dict(getattr(self, "_last_vs_status", {}) or {
            "attempted": False, "succeeded": False, "failed": 0,
            "reason": "not_called", "results": 0,
        })

    def vector_search(self, query: str, top_k: int = 10) -> List[Tuple[float, str]]:
        """Search vector index for top-k nearest neighbors.

        Returns list of (score, memory_id) sorted by similarity descending.
        """
        # 2026-09-15（R41-P23）：**首次真正使用聚合器 → 启动预热**。
        # 聚合器构造期不再无条件预热（见 _init.py 的依据说明），这里补上按需触发；
        # 幂等，且不阻塞本调用（本调用自身的同步路径 + 冷启动自愈仍完整可用）。
        self._start_warmup()
        # ── v7.1.0: Tracing ──
        if self._tracer:
            self._tracer.start_span("vector_search", query=query[:80])
        fn = self._get_embedding_fn()
        # 2026-09-29（用户授权 ①，硬上限）：查询嵌入是**共享串行资源**上的一次调用。
        # 实测：有全量重建在跑时，单次查询在这里排到 221–302 s（随后被健康守卫
        # 以 UNHEALTHY 超宽限 kill）。故给它一个**墙钟上限**：超时 ⇒ 本通道按
        # 不可用降级返回空，融合里的其它通道（fts/bm25/vector）照常工作。
        _qv = self._embed_query_bounded(fn, query)
        if _qv is None:
            with self._lock:
                self._vs_note("query_embed_timeout")
            if self._tracer:
                self._tracer.end_span("vector_search")
            return []
        qv = _qv.reshape(1, -1).astype(np.float32)

        # 2026-09-07: 冷启动自愈——异步 agg-ann-prewarm(rebuild) 未完成时，
        # 索引空但池已有数据，就地重建一次（幂等；_rebuild_index 内部持锁，
        # 此处不持锁防死锁）。每进程最多补建一次，避免维度不匹配时反复重建。
        #
        # 2026-09-14 专项根治：旧实现用**一次性** `_cold_ensure_done` 标志，
        # 且该标志被记录为 "MISSING" —— 说明 `_init.py` 从未初始化它，
        # 而 `_add_to_index` 一旦成功（pool 非空），第一次 `vector_search`
        # 不会进入本分支，标志也就永不置位。真正的问题是：
        # **prewarm 线程可能在本方法判定"索引非空"之后、读取索引之前
        # 把索引清空**（见 _rebuild_index 旧实现在锁外清空）。
        # 现改为：在 `self._lock` 内**同一临界区**判定并消费索引，
        # 若索引为空且池非空，则标记需补建并在锁外补建后**重试一次**。
        # 这样"池非空 ⇒ vector_search 不返回空"成为硬保证，而不是时序运气。
        _skipped_inline = False
        for _attempt in range(2):
            with self._lock:
                if self._faiss_index is not None and self._index_id_map:
                    return self._search_locked(qv, top_k)
                _need_rebuild = bool(self._pool)
            if not _need_rebuild:
                break
            # 2026-09-29（外部审计 / 用户授权 ①）：**请求路径不再同步全量重建**。
            # 实测：`data/aggregator_vectors.pkl` 只覆盖 1/19,347 条 ⇒ 索引恒空
            # ⇒ 原来这里**每个查询**都同步重建 19k 条向量（本机 25–97 分钟；
            # 观测到单次查询 221,159 ms 返回、单客户端 302,002 ms 后连接被重置，
            # 监督器随后以 UNHEALTHY 超宽限 kill 进程两次）。重建改走离线脚本
            # 并落盘，由 `__init__._load()` 在启动时恢复。
            # 回滚（一行）：TRINITY_AGG_INLINE_REBUILD=on。
            if not _inline_rebuild_on():
                self._note_index_missing_once()
                _skipped_inline = True
                break
            ready = getattr(self, "_embedding_ready", None)
            if ready is not None and not ready.is_set():
                # 2026-09-29（外部审计 Round 30）：**此处原为 `ready.wait(timeout=30)`**。
                # 实测代价：profiler 显示一次检索 **28.48s 全部**花在
                # `_thread.lock.acquire`（即 `Event.wait` 内部）；且 `aggregator_vectors.pkl`
                # **文件不存在** ⇒ 索引恒为空（idx=1 vs pool=19347）⇒ **每次检索**都白等 30 秒、
                # 再同步全量重建 19k 嵌入 ⇒ 所有经过聚合器的查询停摆 ~28s。
                #
                # **决定性证据**：本行**上方**的 `qv = fn(query)` 已经成功嵌入 ——
                # 嵌入器显然可用 ⇒ 等这个就绪事件没有意义（它在本部署**从未被置位**）。
                # ⇒ 只在嵌入尚未被证明可用时才等；此处已被证明，故**不等**。
                # 同时**响亮记录一次**，避免"就绪信号从未触发"被静默吸收。
                if not getattr(self, "_emb_ready_missing_warned", False):
                    setattr(self, "_emb_ready_missing_warned", True)
                    __import__("logging").getLogger(__name__).warning(
                        "_embedding_ready 从未置位，但查询嵌入已成功 ⇒ 跳过就绪等待"
                        "（原实现每次检索白等最多 30s）。若索引仍为空，说明 "
                        "aggregator_vectors.pkl 缺失 ⇒ 用 "
                        "python scripts/ann_index_persist_rebuild.py --apply 补建。")
            try:
                self._rebuild_index()
                # §1271：请求路径补建之后同样顺手落盘（同一个自愈）。
                # §1272 修：这里**必须 defer** —— `query()` 是持着 `self._lock` 调进来的
                # （_search.py L83 → L113），内联写 74MB 会把锁占住（§1027：持锁不做好 I/O）。
                self._persist_index_if_missing(defer=True)
            except Exception as _e:
                swallow(__name__, _e)
                break
        with self._lock:
            if self._faiss_index is None or not self._index_id_map:
                if self._tracer:
                    self._tracer.end_span("vector_search")
                _rdy = getattr(self, "_embedding_ready", None)
                # A2：区分「嵌入未就绪」与「已就绪但索引为空」。前者是可恢复的降级，
                # 后者是事实上的"没有可检索内容"——此前两者都是裸 []，无法区分。
                if _skipped_inline:
                    # 2026-09-29（①）：索引为空且**没有**在请求路径重建 ⇒ 这是"稍后会好"
                    # 的可恢复降级，必须与 `no_index`（确实没内容）区分开 —— 与 A2 同一条哲学。
                    self._vs_note("index_rebuilding")
                else:
                    self._vs_note("index_not_ready" if (_rdy is not None and not _rdy.is_set())
                                  else "no_index")
                return []
            return self._search_locked(qv, top_k)

    def _search_locked(self, qv: np.ndarray, top_k: int):
        """在**已持有 self._lock** 的调用方内执行检索（调用方负责持锁）。

        抽出来是为了让 `vector_search` 的"判定 + 检索"落在同一临界区，
        消除"判空通过 → 被并发 rebuild 清空 → 读到空索引"的窗口。
        """
        if self._faiss_index is None or not self._index_id_map:
            if self._tracer:
                self._tracer.end_span("vector_search")
            _rdy2 = getattr(self, "_embedding_ready", None)
            self._vs_note("index_not_ready" if (_rdy2 is not None and not _rdy2.is_set())
                          else "no_index")
            return []
        if _HAS_FAISS:
            scores, indices = self._faiss_index.search(qv, min(top_k, len(self._index_id_map)))
            results = []
            for s, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._index_id_map):
                    results.append((float(s), self._index_id_map[idx]))
            if self._tracer:
                self._tracer.end_span("vector_search")
            self._vs_note("ok", len(results))
            return results
        # numpy cosine similarity
        vecs = self._faiss_index  # (N, dim)
        qv_norm = qv / (np.linalg.norm(qv) + 1e-10)
        vecs_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10)
        sims = np.dot(vecs_norm, qv_norm.T).flatten()
        top_indices = np.argsort(sims)[::-1][:top_k]
        if self._tracer:
            self._tracer.end_span("vector_search")
        _np_res = [(float(sims[i]), self._index_id_map[i]) for i in top_indices if i < len(self._index_id_map)]
        self._vs_note("ok", len(_np_res))
        return _np_res
