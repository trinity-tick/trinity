"""语料向量索引的进程级持有与持久化（2026-10-04）。

## 为什么单独成模块

`_get_vector_index()` 原先**只 `create_index`、从不 load** ⇒ 每个进程都要把约 1.9 万条
语料全量重嵌一次（实测：单核打满约 73 分钟，`POST /memory/selfcheck` 4~39 分钟）。
持久化本身很短，但**接线点有三处**（加载、播种增量判据、脏计数落盘），
塞进 `_search.py` 会顶破该文件 1400 行的能力契约门（实测 1423 行 ⇒ `budget:` 判红）。

因此集中在本模块：
  · `load_corpus_index()`  —— 建索引 + 尝试加载（失败静默回落空索引）
  · `seed_seen()`          —— **从已加载索引播种 `_vec_index_seen`**（关键：不播种则照旧全量重嵌）
  · `maybe_save()`         —— 脏计数触发落盘
  · `save_now()`           —— 进程退出时补一次

落盘格式与校验见 `trinity/vector_index/index.py::VectorIndex.save/load`。
路径 `~/.trinity/data/corpus_vec.{manifest.json,vec.npz,meta.jsonl}` 与既有的
`ann_index.bin*`（`use_ann` 路径）**互不影响**。

开关：`TRINITY_CORPUS_INDEX_PERSIST=0` 关闭加载；`TRINITY_CORPUS_INDEX_SAVE_EVERY` 改阈值。
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any, Dict, Optional

try:
    from trinity._swallow import swallow
except Exception:  # noqa: BLE001
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        return None

logger = logging.getLogger(__name__)

CORPUS_INDEX_NAME = "corpus_vec"
_DEFAULT_SAVE_EVERY = 5000

_state: Dict[str, Any] = {"idx": None, "hit": False, "dirty": 0, "seeded": False,
                          "atexit": False}
_lock = threading.Lock()


def corpus_index_path() -> str:
    """`~/.trinity/data/corpus_vec`（基名，save/load 会追加扩展名）。"""
    return os.path.join(os.path.expanduser("~/.trinity"), "data", CORPUS_INDEX_NAME)


def _enabled() -> bool:
    return os.environ.get("TRINITY_CORPUS_INDEX_PERSIST", "1") != "0"


def _save_every() -> int:
    try:
        return int(os.environ.get("TRINITY_CORPUS_INDEX_SAVE_EVERY",
                                  str(_DEFAULT_SAVE_EVERY)) or _DEFAULT_SAVE_EVERY)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SAVE_EVERY


def _on_exit() -> None:
    """进程退出时补一次落盘（防"攒不够脏计数阈值就退出 ⇒ 这几轮增量白嵌"）。"""
    try:
        if _enabled():
            save_now()
    except Exception:  # noqa: BLE001 — 退出路径绝不抛
        return None


def load_corpus_index(dim: int = 1024) -> Optional[Any]:
    """建索引并尝试从磁盘加载。加载失败/无缓存 ⇒ 返回**空索引**（行为同改动前）。"""
    try:
        from trinity.vector_index.index import HNSWConfig, create_index
    except Exception as _ie:  # noqa: BLE001 — 把真因说出来，别让上层只看到 None
        logger.warning("corpus index: import 失败 ⇒ 将返回 None: %r", _ie)
        return None
    try:
        idx = create_index(
            backend="auto",
            dim=dim,
            metric="cosine",
            index_type="hnsw",
            hnsw_config=HNSWConfig(M=32, efConstruction=200, efSearch=64),
        )
    except Exception as _ce:  # noqa: BLE001 — 同上：静默会让"索引为 None"变成无法解释
        logger.warning("corpus index: create_index(dim=%s) 抛异常 ⇒ 返回 None: %r", dim, _ce)
        return None
    try:
        if idx is None:
            logger.warning("corpus index: create_index 返回 None（dim=%s）", dim)
            return None
        p = corpus_index_path()
        try:
            if idx.load(p, expect_dim=dim):
                _state["hit"] = True
                logger.info("corpus vector index loaded from %s (rows=%d, dim=%d)",
                            p, idx.size(), dim)
            else:
                logger.info("corpus vector index not loaded (no/invalid cache at %s) "
                            "— will build on first full query", p)
        except Exception as _e:  # noqa: BLE001 — 加载失败不得影响索引可用性
            swallow(__name__, _e)
        _state["idx"] = idx
        if not _state["atexit"]:
            _state["atexit"] = True
            atexit.register(_on_exit)
        return idx
    except Exception as _oe:  # noqa: BLE001
        logger.warning("corpus index: 意外失败 ⇒ 返回 None: %r", _oe)
        return None


def loaded_from_disk() -> bool:
    """本次进程是否**命中磁盘缓存**（观测/取证用，不参与任何判据）。"""
    return bool(_state["hit"])


def preload_and_seed(mem: Any, dim: int = 1024) -> Optional[Any]:
    """**在请求路径的预算早返回之前**加载磁盘索引并播种 `_vec_index_seen`。

    为什么需要它（2026-10-05 实测的**接线漏洞**）：
      `_search.py` 的早返回判据是 `_budget == 0 and not _vec_index_seen`，
      而 `_vec_index_seen` 是**进程内**的、重启即空 ⇒ 即便 `corpus_vec.*` 已经由维护链/
      独立预热备好，新进程里它仍为空 ⇒ 早返回 ⇒ **向量通道被静默挡掉、回退成词法检索**
      （实测：`breakdown.vector=5` 但 `vector_channel=lexical:SQLiteAdapter.search_memories`；
      独立进程里 `_vector_search` 返 0 条且 `_vector_index=NoneType`）。
      ⇒ 落盘白做。本函数让"**已备好的索引**"能真正启用向量通道；
      而"没有可用索引"时仍返回 None ⇒ 调用方走原政策（请求路径**不嵌语料**），
      不会重现"与预热抢锁"的老问题（那由 `corpus_budget_scope` 的线程局部语义负责）。

    迁出到本模块的原因：`_search.py` 有**行数预算**（`docs/STRUCTURE_BUDGETS.json`，
    政策要求"优先拆分/迁出，而不是上调数字"）。

    返回：可用索引对象（调用方应赋给 `mem._vector_index`）；不可用返回 None。
    """
    try:
        idx = getattr(mem, "_vector_index", None)
        if idx is None:
            from ._helpers import _get_vector_index
            idx = _get_vector_index(dim=dim)
        if idx is None:
            return None
        seen: Dict[str, Any] = {}
        if seed_seen(seen, idx):
            mem._vec_index_seen = seen
            logger.info("corpus index preloaded before budget guard (rows=%d)",
                        len(getattr(idx, "_entries", None) or {}))
        return idx
    except Exception as _e:  # noqa: BLE001 — 预加载失败必须退回原政策（不抛、不阻塞）
        logger.warning("corpus index preload 失败（按无索引处理）: %r", _e)
        return None


def seed_seen(seen: Dict[str, Any], index: Optional[Any]) -> bool:
    """把已加载索引里已有的 id 播种进 `seen`（`_vec_index_seen`）。只做一次。

    为什么必须播种：`_vec_index_seen` 是**进程内**增量判据，重启即空。
    不播种的话，即便索引从磁盘加载成功，`_search.py` 仍会把全部行判为"没见过"
    而**再嵌一遍** —— 落盘就白做了。
    哨兵值用 `"\\x00loaded"`（≠ 任何真实 `updated_at`）⇒ 该行若真变了仍会被重嵌，语义安全。

    返回是否真的播种了。
    """
    with _lock:
        if _state["seeded"]:
            return False
        _state["seeded"] = True
    try:
        entries = getattr(index, "_entries", None) or {}
        for mid, entry in entries.items():
            seen[str(mid)] = "\x00loaded"
        return bool(entries)
    except Exception as _e:  # noqa: BLE001 — 播种失败只损失性能，不影响正确性
        swallow(__name__, _e)
        return False


def maybe_save(n_new: int) -> bool:
    """脏计数累加 `n_new`；到达阈值就落盘。返回是否落盘成功。

    `TRINITY_CORPUS_INDEX_SAVE_EVERY <= 0` ⇒ **每轮都落盘**（单调性守卫保证不会写小视图）。
    """
    if n_new <= 0 or not _enabled():
        return False
    with _lock:
        _state["dirty"] = int(_state["dirty"]) + int(n_new)
        dirty = int(_state["dirty"])
    every = _save_every()
    if every > 0 and dirty < every:
        return False
    return save_now()


def save_now() -> bool:
    """立即落盘（幂等）。成功 True。"""
    if not _enabled():
        # 2026-10-06（t18）：**函数自身**必须看开关。此前只有两个调用者
        # （`maybe_save` :186 / `_on_exit` :67）看开关 ⇒ 开关今天有效，但任何
        # **直接调用** `save_now()` 的路径都会绕过它（"门禁可被绕过"）。
        # 判据：tests/unit/test_corpus_index_persist_switch_20261006.py（含负向实测）。
        return False
    idx = _state.get("idx")
    if idx is None:
        return False
    try:
        if not idx.size():
            return False
        ok = bool(idx.save(corpus_index_path()))
        if ok:
            with _lock:
                _state["dirty"] = 0
            # 用 WARNING 级别：本仓 API 的日志配置下 info 不一定落盘，
            # 而"落盘是否真的发生"必须是**可观测**的（否则只能靠翻文件 mtime 猜）。
            logger.warning("corpus vector index saved (rows=%d) -> %s",
                           idx.size(), corpus_index_path())
        else:
            logger.warning("corpus vector index save returned False (rows=%d, path=%s)",
                           idx.size(), corpus_index_path())
        return ok
    except Exception as _e:  # noqa: BLE001 — 落盘尽力而为
        swallow(__name__, _e)
        return False
