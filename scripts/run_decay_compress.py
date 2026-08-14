#!/usr/bin/env python3
"""
Trinity — 记忆衰减与压缩守护任务
====================================
连接 PostgreSQL，扫描所有活跃记忆，计算衰减分数，
对低于阈值的记忆批次执行 LLM 压缩。

Usage:
    python scripts/run_decay_compress.py                           # 默认 PG 连接
    python scripts/run_decay_compress.py --host localhost --dbname trinity --user postgres
    python scripts/run_decay_compress.py --dry-run                 # 只扫描不压缩
    python scripts/run_decay_compress.py --lambda-handoff 0.08     # 调整 handoff 衰减速率

Environment variables:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("decay_compress")


# ── Trinity path injection ───────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)


# ── Imports (late, after path injection) ─────────────────────────────

def _import_modules():
    """Late-import Trinity modules."""
    from trinity.daemon.memory_decay import (
        DecayConfig, DecayStatus, MemoryDecayEngine, DecayResult, DecayScanReport,
    )
    from trinity.daemon.memory_compressor import (
        MemoryCompressor, CompressionStatus, CompressionReport, mock_llm_compress,
    )
    from trinity.adapters.postgresql import PostgreSQLAdapter
    return (
        DecayConfig, DecayStatus, MemoryDecayEngine, DecayResult, DecayScanReport,
        MemoryCompressor, CompressionStatus, CompressionReport, mock_llm_compress,
        PostgreSQLAdapter,
    )


# ── PostgreSQL Connection ─────────────────────────────────────────────

def connect_postgresql(
    host: str = "localhost",
    port: int = 5432,
    dbname: str = "trinity",
    user: str = "postgres",
    password: str = "postgres",
) -> Any:
    """Connect to PostgreSQL and return adapter."""
    host = host or os.environ.get("PGHOST", "localhost")
    port = int(port or os.environ.get("PGPORT", "5432"))
    dbname = dbname or os.environ.get("PGDATABASE", os.environ.get("PGDBNAME", "trinity"))
    user = user or os.environ.get("PGUSER", "postgres")
    password = password or os.environ.get("PGPASSWORD", "postgres")

    logger.info("Connecting to PostgreSQL: %s:%s/%s as %s", host, port, dbname, user)

    from trinity.adapters.postgresql import PostgreSQLAdapter
    adapter = PostgreSQLAdapter(
        host=host, port=port, dbname=dbname, user=user, password=password,
        min_conn=1, max_conn=3,
    )
    adapter.connect()
    logger.info("Connected. Pool: %d-%d", adapter._min_conn, adapter._max_conn)
    return adapter


# ── Memory Fetch ──────────────────────────────────────────────────────

def fetch_active_memories(adapter: Any, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch all active (non-archived) memories from PostgreSQL."""
    import psycopg2.extras

    with adapter._get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT memory_id, session_id, persona_id, tenant_id,
                       content, role, importance, tags, category,
                       sha256_hash, status, version, created_at, updated_at
                FROM memories
                WHERE status = 'active'
                ORDER BY created_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                # Convert UUID / datetime to string for portability
                d["memory_id"] = str(d["memory_id"])
                d["session_id"] = str(d["session_id"])
                if hasattr(d.get("created_at"), "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
                if hasattr(d.get("updated_at"), "isoformat"):
                    d["updated_at"] = d["updated_at"].isoformat()
                results.append(d)
            return results


# ── Main Pipeline ─────────────────────────────────────────────────────

def run_decay_compress(
    adapter: Any,
    engine: Any,        # MemoryDecayEngine
    compressor: Any,    # MemoryCompressor
    dry_run: bool = False,
    limit: int = 500,
) -> Dict[str, Any]:
    """Execute full decay scan + compression pipeline.

    Returns:
        Pipeline statistics dict.
    """
    stats = {
        "pipeline_run_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "total_active_memories": 0,
        "decay_healthy": 0,
        "decay_decaying": 0,
        "pending_compression": 0,
        "compression_batches": 0,
        "compressed_summaries": 0,
        "archived_memories": 0,
        "compression_failures": 0,
        "errors": [],
    }

    # ── Step 1: Fetch active memories ──────────────────────────
    logger.info("=" * 60)
    logger.info("Step 1/4: Fetching active memories from PostgreSQL ...")
    memories = fetch_active_memories(adapter, limit=limit)
    stats["total_active_memories"] = len(memories)
    logger.info("  Fetched %d active memories", len(memories))

    if not memories:
        logger.info("  No active memories found. Exiting.")
        return stats

    # ── Step 2: Decay scan ─────────────────────────────────────
    logger.info("Step 2/4: Running decay scan ...")
    report: DecayScanReport = engine.scan_memories(memories)

    stats["decay_healthy"] = report.healthy_count
    stats["decay_decaying"] = report.decaying_count
    stats["pending_compression"] = report.pending_compression_count

    logger.info(
        "  Scan complete: healthy=%d, decaying=%d, pending_compression=%d",
        report.healthy_count, report.decaying_count, report.pending_compression_count,
    )

    # Log decay details for pending items
    pending = engine.get_pending_compression(report)
    if pending:
        logger.info("  Pending compression details:")
        for item in pending[:10]:
            logger.info(
                "    %s | type=%s | importance=%.3f | days=%.1f | score=%.6f",
                item.memory_id[:8], item.memory_type,
                item.importance, item.days_since_creation, item.decay_score,
            )
        if len(pending) > 10:
            logger.info("    ... and %d more", len(pending) - 10)

    if not pending:
        logger.info("  No memories require compression. Exiting.")
        return stats

    if dry_run:
        logger.info("  DRY RUN — skipping compression. Would compress %d memories.", len(pending))
        return stats

    # ── Step 3: Batch creation ─────────────────────────────────
    logger.info("Step 3/4: Creating compression batches ...")
    batches = engine.create_compression_batches(pending)
    stats["compression_batches"] = len(batches)
    logger.info("  Created %d compression batches", len(batches))

    # ── Step 4: Compress each batch ────────────────────────────
    logger.info("Step 4/4: Executing LLM compression ...")

    # Build memory-id → full dict lookup
    mem_lookup: Dict[str, Dict[str, Any]] = {}
    for m in memories:
        mem_lookup[str(m.get("memory_id"))] = m

    for batch_idx, batch in enumerate(batches):
        batch_ids = [r.memory_id for r in batch]
        batch_mems = [mem_lookup[mid] for mid in batch_ids if mid in mem_lookup]
        memory_type = batch[0].memory_type if batch else "general"

        logger.info(
            "  Batch %d/%d: %d memories, type=%s",
            batch_idx + 1, len(batches), len(batch_mems), memory_type,
        )

        try:
            result = compressor.compress_batch(batch_mems, memory_type)

            if result.status == CompressionStatus.SUCCESS:
                stats["compressed_summaries"] += 1
                stats["archived_memories"] += len(result.archived_ids)
                c = result.compressed
                logger.info(
                    "    SUCCESS — summary_id=%s, archived=%d, "
                    "importance=%.3f, elapsed=%.2fs",
                    c.summary_id[:8] if c else "?", len(result.archived_ids),
                    c.importance if c else 0.0, result.elapsed_seconds,
                )
            else:
                stats["compression_failures"] += 1
                logger.warning(
                    "    %s — %s", result.status.value, result.error_message,
                )
        except Exception as e:
            stats["compression_failures"] += 1
            stats["errors"].append(str(e)[:200])
            logger.error("    FAILED — %s", e)

    # ── Final summary ──────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Pipeline complete.")
    logger.info(
        "  Active: %d | Pending: %d | Batches: %d | "
        "Summaries: %d | Archived: %d | Failures: %d",
        stats["total_active_memories"],
        stats["pending_compression"],
        stats["compression_batches"],
        stats["compressed_summaries"],
        stats["archived_memories"],
        stats["compression_failures"],
    )

    return stats


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Trinity Memory Decay & Compression Daemon Task"
    )
    parser.add_argument("--host", default="localhost", help="PostgreSQL host")
    parser.add_argument("--port", type=int, default=5432, help="PostgreSQL port")
    parser.add_argument("--dbname", default="trinity", help="PostgreSQL database name")
    parser.add_argument("--user", default="postgres", help="PostgreSQL user")
    parser.add_argument("--password", default="postgres", help="PostgreSQL password")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, skip compression")
    parser.add_argument("--limit", type=int, default=500, help="Max memories to scan")
    parser.add_argument("--threshold", type=float, default=0.15, help="Compression threshold")
    parser.add_argument("--lambda-handoff", type=float, default=0.05, help="Handoff decay rate")
    parser.add_argument("--lambda-knowledge", type=float, default=0.01, help="Knowledge decay rate")
    parser.add_argument("--lambda-general", type=float, default=0.02, help="General decay rate")
    parser.add_argument("--output", default="", help="Save stats JSON to file")
    args = parser.parse_args()

    # ── Import modules ────────────────────────────────────────
    (
        DecayConfig, DecayStatus, MemoryDecayEngine, DecayResult, DecayScanReport,
        MemoryCompressor, CompressionStatus, CompressionReport, mock_llm_compress,
        PostgreSQLAdapter,
    ) = _import_modules()

    # ── Build decay config ────────────────────────────────────
    decay_config = DecayConfig(
        compression_threshold=args.threshold,
    )
    decay_config.lambda_per_type.update({
        "handoff": args.lambda_handoff,
        "knowledge": args.lambda_knowledge,
        "general": args.lambda_general,
    })

    engine = MemoryDecayEngine(config=decay_config)
    logger.info("Decay engine initialized: %s", json.dumps(engine.summary(), indent=2))

    # ── Connect to PostgreSQL ─────────────────────────────────
    adapter = connect_postgresql(
        host=args.host, port=args.port, dbname=args.dbname,
        user=args.user, password=args.password,
    )

    # ── Build compressor ─────────────────────────────────────
    compressor = MemoryCompressor(
        pg_adapter=adapter,
        llm_callable=mock_llm_compress,  # default mock; replace with real LLM
    )
    logger.info("Compressor initialized (mock LLM mode)")

    # ── Run pipeline ──────────────────────────────────────────
    try:
        stats = run_decay_compress(
            adapter=adapter,
            engine=engine,
            compressor=compressor,
            dry_run=args.dry_run,
            limit=args.limit,
        )

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2, ensure_ascii=False, default=str)
            logger.info("Stats written to %s", args.output)
        else:
            print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))

        if stats.get("errors"):
            sys.exit(1)

    finally:
        adapter.disconnect()
        logger.info("Disconnected from PostgreSQL")


if __name__ == "__main__":
    main()
