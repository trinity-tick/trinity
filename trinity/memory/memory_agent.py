"""Memory Agent — 记忆巩固守护进程 (v8.8.0)。

将三个已实现但长期未接入业务管线的模块串联为一条异步后台闭环：

  - ``LayerClassifier``      三层记忆分类（semantic / episodic）→ 回填 ``memories.memory_layer``
  - ``EntityRelationExtractor`` 实体关系提取 → 写入 ``entities`` / ``relations``
  - ``MemoryConsolidator``   记忆巩固（合并 / 衰减 / 提升）

复用 ``BackgroundScanner`` 的守护进程模式，对外提供：

  - ``run_once()``            单次闭环执行（分层 → ER 提取 → 记忆共现链接 → 巩固）
  - ``start()`` / ``stop()`` / ``status()``  后台生命周期
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.memory.consolidator import MemoryConsolidator
from trinity.memory.er_extractor import EntityRelationExtractor
from trinity.memory.layer_classifier import LayerClassifier

logger = logging.getLogger(__name__)


class MemoryAgent:
    """异步记忆巩固守护进程。

    Parameters
    ----------
    db_path : str
        SQLite 数据库路径（默认 ``trinity_store.db``）。
    llm_call : callable, optional
        可选的回调 ``(prompt: str) -> str``，同时提供给分类器与实体提取器；
        缺省时二者分别降级为规则分类与正则提取。
    interval_seconds : int
        后台循环的执行间隔（秒），默认 3600。
    consolidation_dry_run : bool
        巩固环节是否默认只做 dry-run（只报告不改库），默认 True。
    """

    def __init__(
        self,
        db_path: str = "trinity_store.db",
        llm_call: Optional[Callable[[str], str]] = None,
        interval_seconds: int = 3600,
        consolidation_dry_run: bool = True,
    ) -> None:
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.consolidation_dry_run = consolidation_dry_run

        # connect() 会触发 _create_tables 的自动列迁移（含 memory_layer）。
        self._adapter = SQLiteAdapter(db_path=db_path)
        self._adapter.connect()

        self._classifier = LayerClassifier(llm_call=llm_call)
        self._extractor = EntityRelationExtractor(self._adapter, llm_call=llm_call)
        self._consolidator = MemoryConsolidator(self._adapter)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ── 1. 分层回填 ────────────────────────────────────────────────

    def _classify_unlayered(self) -> Dict[str, Any]:
        conn = self._adapter._conn
        rows = conn.execute(
            "SELECT memory_id, content, category FROM memories "
            "WHERE status = 'active' AND (memory_layer IS NULL OR memory_layer = '')"
        ).fetchall()
        classified = 0
        breakdown: Dict[str, int] = {}
        for row in rows:
            mid, content, category = row["memory_id"], row["content"], row["category"]
            layer = self._classifier.classify(
                content or "", category=category or "general"
            )
            conn.execute(
                "UPDATE memories SET memory_layer = ? WHERE memory_id = ?",
                (layer, mid),
            )
            breakdown[layer] = breakdown.get(layer, 0) + 1
            classified += 1
        conn.commit()
        return {"scanned": len(rows), "classified": classified, "breakdown": breakdown}

    # ── 2. ER 提取（entities + relations）──────────────────────────

    def _extract_entities(self) -> Dict[str, Any]:
        rows = self._adapter._conn.execute(
            "SELECT memory_id FROM memories WHERE status = 'active'"
        ).fetchall()
        ids = [r["memory_id"] for r in rows]
        if not ids:
            return {"entities_added": 0, "relations_added": 0, "processed": 0}
        result = self._extractor.extract_from_memories(ids)
        result["processed"] = len(ids)
        return result

    # ── 3. 记忆共现链接（memory_links）─────────────────────────────

    def _link_memories(self) -> Dict[str, Any]:
        conn = self._adapter._conn
        entity_rows = conn.execute("SELECT name FROM entities").fetchall()
        entity_names = [r["name"] for r in entity_rows if r["name"] and len(r["name"]) >= 2]
        if not entity_names:
            return {"links_added": 0}

        mem_rows = conn.execute(
            "SELECT memory_id, content FROM memories WHERE status = 'active'"
        ).fetchall()
        lowered = {n.lower(): n for n in entity_names}

        # memory_id -> [canonical entity names]
        mem_entities: Dict[str, List[str]] = {}
        for row in mem_rows:
            content = (row["content"] or "").lower()
            found = [name for key, name in lowered.items() if key in content]
            if found:
                mem_entities[row["memory_id"]] = found

        links_added = 0
        mids = list(mem_entities.keys())
        for i in range(len(mids)):
            for j in range(i + 1, len(mids)):
                shared = set(mem_entities[mids[i]]) & set(mem_entities[mids[j]])
                if shared:
                    r = self._adapter.create_memory_link(
                        mids[i], mids[j], "co_occurrence", strength=0.5
                    )
                    if "id" in r:
                        links_added += 1
        return {"links_added": links_added, "memories_linked": len(mids)}

    # ── 4. 巩固（合并 / 衰减 / 提升）───────────────────────────────

    def _consolidate(self, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        if dry_run is None:
            dry_run = self.consolidation_dry_run
        return self._consolidator.run_full_cycle(dry_run=dry_run)

    # ── 单次闭环 ──────────────────────────────────────────────────

    def run_once(self) -> Dict[str, Any]:
        """执行一次完整的记忆巩固闭环，返回各环节结果。"""
        started = datetime.now(timezone.utc).isoformat()
        layer = self._classify_unlayered()
        er = self._extract_entities()
        links = self._link_memories()
        consolidation = self._consolidate()
        return {
            "started_at": started,
            "layer": layer,
            "entity_relation": er,
            "memory_links": links,
            "consolidation": consolidation,
        }

    # ── 生命周期 ──────────────────────────────────────────────────

    def start(self) -> bool:
        """启动后台守护线程。"""
        if self._running:
            logger.info("MemoryAgent already running")
            return False
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="trinity-memory-agent", daemon=True
        )
        self._thread.start()
        logger.info("MemoryAgent started (interval=%ss)", self.interval_seconds)
        return True

    def stop(self) -> bool:
        """停止后台守护线程并断开连接。"""
        if not self._running:
            return False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._running = False
        self._adapter.disconnect()
        logger.info("MemoryAgent stopped")
        return True

    def status(self) -> Dict[str, Any]:
        """返回守护进程当前状态。"""
        return {
            "running": self._running,
            "db_path": self.db_path,
            "interval_seconds": self.interval_seconds,
            "consolidation_dry_run": self.consolidation_dry_run,
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                summary = self.run_once()
                logger.info("MemoryAgent cycle done: %s", summary)
            except Exception:
                logger.exception("MemoryAgent cycle failed")
            self._stop_event.wait(self.interval_seconds)


# ── Module-level self_test ────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """在临时库上验证 MemoryAgent 完整闭环（不触达真实数据）。"""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_agent.db")
        adapter = SQLiteAdapter(db_path=db_path)
        adapter.connect()
        adapter.store_memory(
            "老板今天讨论了 Trinity 项目，决定迁移到 PostgreSQL。",
            persona_id="p_test",
        )
        adapter.store_memory(
            "Trinity 项目使用 Python 和 SQLite，本周要上线。",
            persona_id="p_test",
        )
        adapter._flush_batch()
        # 先释放写入端连接，避免 Windows 下双连接占用文件导致临时目录无法清理。
        adapter.disconnect()

        agent = MemoryAgent(db_path=db_path, consolidation_dry_run=True)
        summary = agent.run_once()

        conn = agent._adapter._conn
        layer_filled = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_layer IS NOT NULL"
        ).fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        link_count = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        agent._adapter.disconnect()

        passed = layer_filled >= 1 and entity_count >= 1
        return {
            "module": "trinity.memory.memory_agent",
            "result": "PASS" if passed else "FAIL",
            "layer_filled": layer_filled,
            "entity_count": entity_count,
            "relation_count": relation_count,
            "link_count": link_count,
            "summary": summary,
        }
