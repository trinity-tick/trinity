"""Trinity client - construction mixin & module-global state.

This module owns _TRINITY_STORE / _BRIDGE_CACHE (and the bridge
import helpers) because Trinity.__init__/_init_sqlite_adapter mutate
_TRINITY_STORE through a global statement; keeping writer and
readers in one module preserves the original shared-global semantics.
_ensure_vms also reads _TRINITY_STORE, so vms/_ensure_vms live here.
"""

import hashlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
from ._helpers import _find_trinity_store

logger = logging.getLogger("trinity.core.client")

# Preserve the old single-file __file__ semantics: the original module lived at
# trinity/core/client.py, so dirname(dirname(__file__)) resolved to <repo>/trinity.
# (used by _init_sqlite_adapter fallback: dirname(dirname(__file__))/data)
__file__ = os.path.join(os.path.dirname(os.path.dirname(__file__)), "client.py")

# ---- Locate output directory ----
# _TRINITY_STORE is the module-global binding mutated by __init__/_init_sqlite_adapter.
# (original line 53, verbatim)
_TRINITY_STORE = _find_trinity_store()

# ---- Dynamically import the trinity_call bridge module ----
def _import_trinity_bridge():
    """Dynamically import the trinity_call bridge module.

    2026-08-25（闭环自检修复）：trinity_call 是运行时部署的桥模块，
    环境缺失时导入抛 ModuleNotFoundError——bridge 是可选增强，不应
    阻断核心功能（reason/search 等）。缺失时返回 None（容错降级）。
    """
    try:
        sys.path.insert(0, _TRINITY_STORE)
        from trinity_call import trinity as _trinity
        return _trinity
    except Exception:
        # 桥模块缺失/损坏 → 降级（bridge 属性返回 None，调用方容错）
        return None

# ---- Cached bridge import ----
_BRIDGE_CACHE: Optional[Any] = None

def _get_cached_bridge():
    global _BRIDGE_CACHE
    if _BRIDGE_CACHE is None:
        _BRIDGE_CACHE = _import_trinity_bridge()
    return _BRIDGE_CACHE

class _ConstructionMixin:
    def __init__(
        self,
        store_path: Optional[str] = None,
        tenant_id: str = "default",
        adapter: Optional[str] = None,
        use_ann: bool = False,
        evolution_enabled: bool = True,
    ):
        global _TRINITY_STORE
        if store_path:
            _TRINITY_STORE = store_path
        self.tenant_id = tenant_id
        self._bridge = None
        self._adapter = None
        self._engine = None
        # 2026-08-24（R9 P0-1）：引擎初始化错误（connect 失败等）——
        # 非 None 时表示引擎不可用，诊断/健康检查应报 degraded 而非 healthy。
        self._engine_error: Optional[str] = None

        # ── 自进化记忆系统 ──────────────────────────────────────────
        self.evolution_enabled = evolution_enabled
        self._scheduler = None

        # ── 写入加工管线串行锁（2026-08-15 二轮压测修复）────────
        # postprocess 后台线程化后，8 并发加工线程同时 sklearn fit +
        # 抢 _write_lock 会拖垮写入线程（GIL 风暴 + 锁饥饿，实测响应
        # p95 3.7s）。加工是后台异步工作，本就无需并发：全局串行化，
        # 同一时刻至多一个加工线程（embedding 引擎也只 fit 一次）。
        import threading as _thr
        self._postprocess_lock = _thr.Lock()

        # ── 分层检索配置 ──────────────────────────────────────────
        self.half_life_days: float = 7.0       # 时间衰减半衰期（天）
        self.agent_weight_default: float = 1.0  # 未配置 Agent 的默认权重
        self.push_half_life_days: float = 30.0  # 推送记忆时间衰减半衰期（天）

        # ── 向量搜索缓存 ──────────────────────────────────────────
        self._embedding_engine = None
        self._vector_index = None
        self._ann_index = None
        # 性能（2026-08-15）：ANN 索引持久化缓存（版本键：维度+条数+最新updated_at）。
        # 此前每次 use_ann 搜索都全量编码+重建索引——缓存后首次构建、后续直查。
        self._ann_cache = None
        # ①落盘持久化（2026-08-15）：索引 save/load 到 ~/.trinity/data/ann_index.bin，
        # 跨进程/重启免 30s 重建；写入增量维护（脏计数阈值触发 save）。
        self._ann_index_path = os.path.join(
            os.path.expanduser("~/.trinity"), "data", "ann_index.bin"
        )
        self._ann_dirty = 0

        # ── ANN 配置 ──────────────────────────────────────────────
        self.use_ann: bool = use_ann  # 启用 hnswlib/FAISS HNSW ANN 索引

        # ── 混合检索（向量 + BM25 + 图谱）─────────────────────────
        self._hybrid_retriever = None
        self._bm25_index = None
        self._bm25_ready = False  # 后台预构建完成标记（2026-08-15）
        self._bm25_lock = _thr.Lock()  # 构建原子化（2026-08-15 二轮）

        # ── 跨模态检索（文字 ↔ 图片记忆）─────────────────────────
        self._cross_modal_retriever = None

        # ── 个性化引擎（PAHF 双反馈, R3 P0-2, 2026-08-15）────────
        # 惰性实例化；TRINITY_PERSONALIZE=on 时 search 注入偏好上下文。
        self._personalization = None

        # ── SAGE 自进化图记忆（R5 P0, 2026-08-15, MindMemOS 对齐）──
        # 惰性实例化；写入时可同步图记忆、查询图证据路径、触发自进化。
        self._sage = None

        # ── DCPM 双过程认知记忆（R5 P0, 2026-08-15, Dual-Process 对齐）
        # 惰性实例化；System1 信念修订链 + System2 夜间 schema 归纳。
        self._dcpm = None

        # ── 记忆压缩引擎 ──────────────────────────────────────────
        self._compressor = None

        if adapter == "postgresql":
            self._init_postgres_adapter()
        elif adapter == "sqlite":
            self._init_sqlite_adapter()
        elif adapter is None:
            # Default: use SQLite with store_path, but honor TRINITY_DB_PATH env var.
            # store_path 语义：目录 → 内部生成 trinity_store.db；已是 .db 文件 → 直接使用。
            from trinity.adapters.sqlite import SQLiteAdapter
            _db_path = os.environ.get("TRINITY_DB_PATH")
            if not _db_path:
                if _TRINITY_STORE and os.path.isfile(_TRINITY_STORE):
                    _db_path = _TRINITY_STORE
                elif _TRINITY_STORE:
                    _db_path = os.path.join(_TRINITY_STORE, "trinity_store.db")
                else:
                    # 2026-08-15：不再用相对路径（曾落到 cwd 小库），固定权威路径
                    _db_path = os.path.join(
                        str(Path.home() / ".trinity" / "store"), "trinity_store.db"
                    )
            try:
                self._adapter = SQLiteAdapter(db_path=_db_path)
                self._adapter.connect()
            except Exception as exc:
                # 2026-08-24（R9 P0-1）：不再静默降级——记录详细错误与
                # 库路径（此前 except: pass 吞异常导致 API 在引擎全挂时
                # 自报 healthy、检索静默 0 hits 的"健康假象"）。
                logger.error(
                    "Trinity SQLite adapter connect FAILED for %s: %s: %s",
                    _db_path, type(exc).__name__, exc,
                )
                self._adapter = None
                self._engine_error = f"{type(exc).__name__}: {exc}"
        else:
            raise ValueError(f"Unknown adapter: {adapter}")
    def _init_sqlite_adapter(self):
        from trinity.adapters.sqlite import SQLiteAdapter
        global _TRINITY_STORE
        # 统一到权威大库（~/.trinity/store/trinity_store.db），不再拼 data/ 子目录
        # （2026-08-15：曾生成 ~/.trinity/store/data/trinity_store.db 等小库，双库并存）
        _store_dir = _TRINITY_STORE or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data"
        )
        os.makedirs(_store_dir, exist_ok=True)
        db_path = os.path.join(_store_dir, "trinity_store.db")
        try:
            self._adapter = SQLiteAdapter(db_path=db_path)
            self._adapter.connect()
        except Exception as exc:
            # 2026-08-24（R9 P0-1）：同默认路径，不静默降级
            logger.error(
                "Trinity SQLite adapter connect FAILED for %s: %s: %s",
                db_path, type(exc).__name__, exc,
            )
            self._adapter = None
            self._engine_error = f"{type(exc).__name__}: {exc}"
    def _init_postgres_adapter(self):
        from trinity.adapters.postgresql import PostgreSQLAdapter
        self._adapter = PostgreSQLAdapter(
            host=os.environ.get("TRINITY_PG_HOST", "localhost"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        )
        self._adapter.connect()
    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = _get_cached_bridge()
        return self._bridge
    @property
    def vms(self):
        """VMS instance (lazy-initialised)."""
        return self._ensure_vms()
    def _ensure_vms(self):
        if getattr(self, "_vms", None) is None:
            from trinity.vms import VMS
            from trinity.vms.backends.sqlite_backend import SQLiteVMSBackend
            from trinity.adapters.sqlite import SQLiteAdapter
            import os

            db_path = os.environ.get("TRINITY_DB_PATH") or os.path.join(
                _TRINITY_STORE or str(Path.home() / ".trinity" / "store"), "trinity_store.db"
            )
            adapter = SQLiteAdapter(db_path=db_path)
            adapter.connect()

            backend = SQLiteVMSBackend(adapter=adapter)
            self._vms = VMS.from_defaults(memory_store=backend)
        return self._vms
