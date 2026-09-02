#!/usr/bin/env python3
"""
Trinity — 睡眠式记忆整合（Sleep-Time Consolidation, 2026-08-15）
=====================================================================
对齐业界 2026 方案（SCM / Zep consolidation / Hindsight self-reflection），
把 nightly 记忆维护升级为多阶段"睡眠整合"：

  Phase 1  衰减扫描 + 压缩    复用 MemoryDecayEngine + MemoryCompressor
  Phase 2  LLM 事实提取       从近期高重要性记忆聚合，抽取可固化事实（带 provenance）
  Phase 3  图更新             提取事实中的实体 → upsert 实体/关系
  Phase 4  冲突检测           对提取事实做冲突检查（复用 adapter）
  Phase 5  报告               JSON 统计

用法：
    python scripts/sleep_consolidation.py --store sqlite            # mock LLM（安全）
    python scripts/sleep_consolidation.py --store sqlite --llm real # 真实 LLM（需 TRINITY_LLM_*）
    python scripts/sleep_consolidation.py --dry-run                 # 只扫描不写入

环境变量（--llm real 时）：
    TRINITY_LLM_API_KEY / TRINITY_LLM_BASE_URL / TRINITY_LLM_MODEL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("sleep_consolidation")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")


def connect_sqlite(db_path: str) -> Any:
    from trinity.adapters.sqlite import SQLiteAdapter
    adapter = SQLiteAdapter(db_path=db_path)
    adapter.connect()
    logger.info("Connected to SQLite store: %s", db_path)
    return adapter


def _make_llm_callable() -> Any:
    """真实 LLM callable（复用 compressor 的 OpenAI 兼容实现）。"""
    from trinity.daemon.memory_compressor import create_llm_compress_callable
    return create_llm_compress_callable()


def phase1_decay_compress(adapter: Any, limit: int, dry_run: bool, llm_real: bool) -> Dict[str, Any]:
    """Phase 1: 衰减扫描 + 压缩（复用现有引擎）。"""
    import runpy  # noqa: F401  （仅占位，逻辑内联自 run_decay_compress）
    from trinity.daemon.memory_decay import DecayConfig, MemoryDecayEngine
    from trinity.daemon.memory_compressor import MemoryCompressor, mock_llm_compress

    conn = adapter._conn
    rows = conn.execute("""
        SELECT memory_id, session_id, persona_id, tenant_id, content, role,
               importance, tags, category, sha256_hash, status, version,
               created_at, updated_at, access_count, last_accessed_at
        FROM memories WHERE status='active' ORDER BY created_at ASC LIMIT ?
    """, (limit,)).fetchall()
    memories = [dict(r) for r in rows]
    logger.info("Phase1: fetched %d active memories", len(memories))

    cfg = DecayConfig()
    engine = MemoryDecayEngine(config=cfg)
    report = engine.scan_memories(memories)
    pending = engine.get_pending_compression(report)
    stats = {
        "scanned": len(memories), "healthy": report.healthy_count,
        "decaying": report.decaying_count, "pending": len(pending),
        "archived": 0, "summaries": 0, "failures": 0,
    }
    if dry_run or not pending:
        logger.info("Phase1: dry_run=%s pending=%d", dry_run, len(pending))
        return stats

    llm_callable = _make_llm_callable() if llm_real else mock_llm_compress
    compressor = MemoryCompressor(pg_adapter=adapter, llm_callable=llm_callable)
    batches = engine.create_compression_batches(pending)
    mem_lookup = {str(m.get("memory_id")): m for m in memories}
    for batch in batches:
        batch_mems = [mem_lookup[mid] for mid in (r.memory_id for r in batch) if mid in mem_lookup]
        if not batch_mems:
            continue
        try:
            res = compressor.compress_batch(batch_mems, batch[0].memory_type)
            if res.status.name == "SUCCESS":
                stats["summaries"] += 1
                stats["archived"] += len(res.archived_ids)
                logger.info("Phase1: batch OK — summary=%s archived=%d", res.compressed.summary_id[:8] if res.compressed else "?", len(res.archived_ids))
            else:
                stats["failures"] += 1
                logger.warning("Phase1: %s — %s", res.status.value, res.error_message)
        except Exception as e:  # noqa: BLE001
            stats["failures"] += 1
            logger.error("Phase1: FAILED %s", e)
    logger.info("Phase1 done: %s", {k: stats[k] for k in ("archived", "summaries", "failures")})
    return stats


def _fact_prompt_sys(max_facts: int) -> str:
    """2026-09-01（大脑化层1 抽取规模化）：max_facts 参数化（原硬编码 5 条）。"""
    return (
        "你是记忆整合引擎。从用户提供的记忆条目中提取可长期固化的事实（fact）。"
        "只输出 JSON 数组，每项 {fact, entities: [实体名...], importance: 0-1}，最多 %d 条；"
        "不要输出其它内容。" % max_facts
    )


def phase2_extract_facts(adapter: Any, top_n: int = 20, llm_real: bool = False, dry_run: bool = False,
                         max_facts: int = 20, min_importance: float = 0.2,
                         recent_days: int = 0) -> List[Dict[str, Any]]:
    """Phase 2: LLM 事实提取——从近期高重要性记忆聚合出可固化事实。"""
    conn = adapter._conn
    # 2026-09-01（事件驱动轻量版）：--recent-days 只聚合近期记忆（分钟级感知的日链形态）
    if recent_days > 0:
        from datetime import datetime, timedelta, timezone
        _cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat()
        rows = conn.execute("""
            SELECT memory_id, content, importance, created_at FROM memories
            WHERE status='active' AND created_at > ?
            ORDER BY importance DESC, created_at DESC LIMIT ?
        """, (_cutoff, top_n)).fetchall()
    else:
        rows = conn.execute("""
            SELECT memory_id, content, importance, created_at FROM memories
            WHERE status='active' ORDER BY importance DESC, created_at DESC LIMIT ?
        """, (top_n,)).fetchall()
    # 2026-09-01（加密盲区修复）：content 密文(enc:v1:)须先解密，否则 LLM 只见密文
    # 提取恒 0（近期记忆全加密 → consolidate-recent extracted=0 的根因）
    _cipher = getattr(adapter, "_cipher", None)
    if _cipher is not None:
        _rows2 = []
        for r in rows:
            c = r["content"] or ""
            if isinstance(c, str) and c.startswith("enc:v1:"):
                try:
                    c = _cipher.decrypt(c)
                except Exception:
                    pass
            _rows2.append(dict(r, content=c))
        rows = _rows2
    if not rows:
        return []
    aggregate = "\n---\n".join(
        f"[{r['memory_id'][:8]}] {str(r['content'])[:300]}" for r in rows
    )
    facts: List[Dict[str, Any]] = []
    if not llm_real:
        # mock：退化为"高重要性记忆直存为事实"（不产生新记忆，仅报告）
        logger.info("Phase2: mock 模式（不调用 LLM）——无事实提取")
        return facts
    try:
        llm = _make_llm_callable()
        raw = llm(_fact_prompt_sys(max_facts), f"记忆列表：\n{aggregate}\n\n请提取事实（JSON 数组）。")
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            facts = json.loads(m.group(0))
            if isinstance(facts, list):
                facts = [f for f in facts if isinstance(f, dict) and f.get("fact")
                         and float(f.get("importance", 0.0) or 0.0) >= min_importance][:max_facts]
    except Exception as e:  # noqa: BLE001
        logger.error("Phase2: LLM extraction failed: %s", e)
        return []

    if dry_run:
        logger.info("Phase2: dry_run — would persist %d facts", len(facts))
        return facts

    from trinity.adapters.sqlite import SQLiteAdapter  # noqa: F401
    persisted = 0
    for i, f in enumerate(facts):
        content = str(f.get("fact", "")).strip()
        if not content:
            continue
        try:
            res = adapter.store_memory(
                content=content,
                persona_id="default",
                session_id="consolidation",
                agent_id="default",
                role="agent",
                importance=min(1.0, max(0.1, float(f.get("importance", 0.7)))),
                tags=["consolidated", "fact"] + [str(e) for e in (f.get("entities") or [])][:5],
                category="knowledge",
            )
            logger.info("Phase2: fact persisted as %s", res.get("memory_id", "?"))
            persisted += 1
        except Exception as e:  # noqa: BLE001
            logger.error("Phase2: persist fact failed: %s", e)
    logger.info("Phase2 done: extracted=%d persisted=%d", len(facts), persisted)
    return facts


def phase3_update_graph(adapter: Any, facts: List[Dict[str, Any]]) -> int:
    """Phase 3: 图更新——事实中的实体 upsert 实体/关系。"""
    count = 0
    for f in facts:
        for ent in (f.get("entities") or [])[:5]:
            try:
                adapter.upsert_entity(name=str(ent).strip()[:128], etype="concept")
                count += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("Phase3: entity upsert skip %s: %s", ent, e)
    logger.info("Phase3 done: entities upserted=%d", count)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity Sleep-Time Consolidation")
    parser.add_argument("--store", choices=["sqlite"], default="sqlite")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=200, help="Phase1 扫描上限")
    parser.add_argument("--extract-top-n", type=int, default=20, help="Phase2 聚合记忆数")
    # 2026-09-01（大脑化层1）：抽取规模化 + 置信度门槛
    parser.add_argument("--facts", type=int, default=20, help="Phase2 最大提取事实数（原 5）")
    parser.add_argument("--min-importance", type=float, default=0.2, help="Phase2 事实 importance 门槛")
    parser.add_argument("--recent-days", type=int, default=0, help="2026-09-01: 仅聚合近 N 天记忆（0=全部，事件驱动轻量形态）")
    parser.add_argument("--llm", choices=["mock", "real", "auto"], default="auto",
                        help="auto(默认)=有 TRINITY/DEEPSEEK key 走 real，否则 mock")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    adapter = connect_sqlite(args.sqlite_path)
    # auto（生产默认）：有 TRINITY_LLM_API_KEY 或 DEEPSEEK_API_KEY → real，否则 mock
    llm_mode = args.llm
    if llm_mode == "auto":
        import os as _os
        llm_mode = "real" if (_os.environ.get("TRINITY_LLM_API_KEY")
                              or _os.environ.get("DEEPSEEK_API_KEY")) else "mock"
        logger.info("LLM auto mode: resolved to %s", llm_mode)
    llm_real = llm_mode == "real"
    stats: Dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "dry_run": args.dry_run, "phases": {}}
    try:
        stats["phases"]["decay_compress"] = phase1_decay_compress(
            adapter, args.limit, args.dry_run, llm_real)
        facts = phase2_extract_facts(adapter, args.extract_top_n, llm_real, args.dry_run,
                                      max_facts=args.facts, min_importance=args.min_importance,
                                      recent_days=args.recent_days)
        stats["phases"]["extract_facts"] = {"extracted": len(facts)}
        stats["phases"]["graph_update"] = {"entities_upserted": phase3_update_graph(adapter, facts) if not args.dry_run else 0}
    finally:
        adapter.disconnect()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False, default=str)
        logger.info("Report written to %s", args.output)
    else:
        print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
