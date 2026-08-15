#!/usr/bin/env python3
"""
Trinity — 三层记忆生命周期守护任务
=======================================
连接 PostgreSQL，扫描所有记忆，按加权评分自动分配层级。
执行 Core → Recall → Archival 的自动升级/降级策略。

Usage:
    python scripts/run_memory_tiers.py                              # 默认 PG 连接
    python scripts/run_memory_tiers.py --host localhost --dbname trinity
    python scripts/run_memory_tiers.py --dry-run                    # 预览不执行
    python scripts/run_memory_tiers.py --core-token-limit 400       # 自定义 token 限制
    python scripts/run_memory_tiers.py --scan-interval 3600         # 设置扫描间隔（秒）

Scoring model (recency + importance + access_frequency):
  score = 0.40 × recency_score + 0.35 × importance + 0.25 × access_freq_score

  where:
    recency_score = exp(-age_days / 30)
    access_freq_score = min(1.0, access_freq / peak_freq)

Environment variables:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("memory_tiers")


# ── Trinity path injection ───────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

# 把内存分层模块的名字绑定为模块级全局：此前只在 main() 里局部解包，
# 导致 populate_* 等辅助函数引用 MemoryBlock / MemoryTier / BlockType 时
# 报 NameError（这些名字不在模块级作用域）。路径注入已完成，直接导入即可。
from trinity.daemon.memory_tiers import (
    MemoryTier, BlockType, MemoryBlock, CoreMemory,
    RecallMemory, ArchivalMemory, MemoryTierManager,
    TierMigrationRecord, create_memory_tier_manager,
    DEFAULT_CORE_TOKEN_LIMIT,
)


# ── Imports (late, after path injection) ─────────────────────────────

def _import_modules():
    """Late-import Trinity modules."""
    from trinity.daemon.memory_tiers import (
        MemoryTier, BlockType, MemoryBlock, CoreMemory,
        RecallMemory, ArchivalMemory, MemoryTierManager,
        TierMigrationRecord, create_memory_tier_manager,
        DEFAULT_CORE_TOKEN_LIMIT,
    )
    return (
        MemoryTier, BlockType, MemoryBlock, CoreMemory,
        RecallMemory, ArchivalMemory, MemoryTierManager,
        TierMigrationRecord, create_memory_tier_manager,
        DEFAULT_CORE_TOKEN_LIMIT,
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

def fetch_all_memories_sqlite(adapter: Any, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch active memories from the SQLite runtime store（分层用，含 version_count）。"""
    conn = getattr(adapter, "_conn", None)
    if conn is None:
        return []
    rows = conn.execute("""
        SELECT m.memory_id, m.session_id, m.persona_id, m.tenant_id,
               m.content, m.role, m.importance, m.tags, m.category,
               m.sha256_hash, m.status, m.version, m.created_at, m.updated_at,
               (SELECT COUNT(*) FROM memory_versions v WHERE v.memory_id = m.memory_id) AS version_count
        FROM memories m
        WHERE m.status = 'active'
        ORDER BY m.created_at ASC
        LIMIT ?
    """, (limit,)).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        for k in ("created_at", "updated_at"):
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        results.append(d)
    return results


def connect_sqlite(db_path: str) -> Any:
    """Connect to the SQLite runtime store (权威大库) and return adapter."""
    from trinity.adapters.sqlite import SQLiteAdapter
    adapter = SQLiteAdapter(db_path=db_path)
    adapter.connect()
    logger.info("Connected to SQLite store: %s", db_path)
    return adapter


def fetch_all_memories(adapter: Any, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch all active memories from PostgreSQL.

    Includes access simulation: for tiering purposes, we estimate
    access count based on memory version count.
    """
    import psycopg2.extras

    with adapter._get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                    m.memory_id, m.session_id, m.persona_id, m.tenant_id,
                    m.content, m.role, m.importance, m.tags, m.category,
                    m.sha256_hash, m.status, m.version, m.created_at, m.updated_at,
                    COALESCE(v.version_count, 0) AS version_count
                FROM memories m
                LEFT JOIN (
                    SELECT memory_id, COUNT(*) AS version_count
                    FROM memory_versions
                    GROUP BY memory_id
                ) v ON m.memory_id = v.memory_id
                WHERE m.status = 'active'
                ORDER BY m.created_at ASC
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


# ── Auto-Tiering: Score & Assign ─────────────────────────────────────

def compute_tier_score(
    memory: Dict[str, Any],
    w_recency: float = 0.40,
    w_importance: float = 0.35,
    w_access: float = 0.25,
) -> Tuple[float, Dict[str, float]]:
    """为单条记忆计算加权层级评分。

    评分公式:
      score = w_recency × recency_score + w_importance × importance
            + w_access × access_frequency_score

    Args:
        memory: 记忆字典（含 created_at, importance, version_count）
        w_recency: 时近权重
        w_importance: 重要性权重
        w_access: 访问频率权重

    Returns:
        (score, components_dict)
    """
    import math

    now = datetime.now(timezone.utc)

    # Parse created_at
    created_str = str(memory.get("created_at", ""))
    try:
        created = datetime.fromisoformat(created_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        created = now - timezone.utc.localize(
            datetime.fromtimestamp(0).replace(tzinfo=timezone.utc)
        )

    age_seconds = (now - created).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)

    # Recency score (exponential decay over 30 days)
    recency_score = math.exp(-age_days / 30.0)

    # Importance
    importance = float(memory.get("importance", 0.5))
    importance = max(0.0, min(1.0, importance))

    # Access frequency proxy: version_count as edit activity
    version_count = int(memory.get("version_count", 0))
    hours = max(age_seconds / 3600.0, 0.01)
    access_freq = version_count / hours
    af_score = min(1.0, access_freq / 10.0)

    # Weighted composite
    score = (
        w_recency * recency_score
        + w_importance * importance
        + w_access * af_score
    )
    score = max(0.0, min(1.0, score))

    components = {
        "age_days": round(age_days, 1),
        "recency_score": round(recency_score, 4),
        "importance": importance,
        "version_count": version_count,
        "access_freq_per_hour": round(access_freq, 4),
        "af_score": round(af_score, 4),
    }

    return score, components


def assign_tier(
    score: float,
    category: str,
    core_threshold: float = 0.55,
    recall_threshold: float = 0.20,
) -> str:
    """根据评分和类别分配记忆层级。

    Args:
        score: 加权评分
        category: 记忆类别
        core_threshold: Core 最低评分
        recall_threshold: Recall 最低评分（低于此进入 Archival）

    Returns:
        "core" / "recall" / "archival"
    """
    # System / handoff memories default to core if score is high enough
    if category in ("system", "handoff") and score >= 0.30:
        return "core"

    if score >= core_threshold:
        return "core"
    elif score >= recall_threshold:
        return "recall"
    else:
        return "archival"


# ── Main Pipeline ─────────────────────────────────────────────────────

def run_memory_tiers(
    adapter: Any,
    manager: Any,          # MemoryTierManager
    dry_run: bool = False,
    limit: int = 500,
    core_threshold: float = 0.55,
    recall_threshold: float = 0.20,
    output_file: str = "",
    store: str = "pg",
) -> Dict[str, Any]:
    """执行完整三层记忆分层守护任务。

    Pipeline:
      1. 从 PostgreSQL 获取所有活跃记忆
      2. 为每条记忆计算加权评分
      3. 自动分配层级（Core / Recall / Archival）
      4. 执行 Core 溢出 eviction
      5. 执行 Recall → Core promotion
      6. 执行 Recall → Archival demotion
      7. 输出分层操作日志

    Returns:
        任务统计字典
    """
    stats: Dict[str, Any] = {
        "task_run_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "total_memories": 0,
        "assigned_core": 0,
        "assigned_recall": 0,
        "assigned_archival": 0,
        "evictions": 0,
        "promotions": 0,
        "demotions": 0,
        "errors": [],
        "tiering_details": [],
    }

    # ── Step 1: Fetch memories ─────────────────────────────────
    logger.info("=" * 60)
    logger.info("Step 1/4: Fetching active memories from %s ...", store)
    if store == "sqlite":
        memories = fetch_all_memories_sqlite(adapter, limit=limit)
    else:
        memories = fetch_all_memories(adapter, limit=limit)
    stats["total_memories"] = len(memories)
    logger.info("  Fetched %d active memories", len(memories))

    if not memories:
        logger.info("  No active memories found. Exiting.")
        return stats

    # ── Step 2: Score and assign tiers ─────────────────────────
    logger.info("Step 2/4: Computing tier scores and assigning tiers ...")

    for mem in memories:
        score, components = compute_tier_score(
            mem,
            w_recency=manager.w_recency,
            w_importance=manager.w_importance,
            w_access=manager.w_access,
        )
        category = str(mem.get("category", "general"))
        tier = assign_tier(score, category, core_threshold, recall_threshold)
        mem_id = str(mem.get("memory_id", ""))[:12]

        tiering_info = {
            "memory_id": mem_id,
            "category": category,
            "score": round(score, 4),
            "assigned_tier": tier,
            "components": components,
            "content_preview": str(mem.get("content", ""))[:80],
        }
        stats["tiering_details"].append(tiering_info)

        if tier == "core":
            stats["assigned_core"] += 1
        elif tier == "recall":
            stats["assigned_recall"] += 1
        else:
            stats["assigned_archival"] += 1

    logger.info(
        "  Tier assignment: core=%d, recall=%d, archival=%d",
        stats["assigned_core"],
        stats["assigned_recall"],
        stats["assigned_archival"],
    )

    # Log tier distribution details
    if stats["tiering_details"]:
        logger.info("  Tiering details (first 10):")
        for item in stats["tiering_details"][:10]:
            comps = item["components"]
            logger.info(
                "    %s | cat=%-12s | score=%.4f → %s | "
                "days=%.0f imp=%.2f freq=%.4f",
                item["memory_id"], item["category"],
                item["score"], item["assigned_tier"],
                comps["age_days"], comps["importance"],
                comps["access_freq_per_hour"],
            )

    # ── Step 3: Populate manager with memories ─────────────────
    logger.info("Step 3/4: Populating MemoryTierManager ...")

    # Seed Core blocks from high-score memories
    core_count = 0
    for mem in memories:
        tier_info = None
        for ti in stats["tiering_details"]:
            if ti["memory_id"] == str(mem.get("memory_id", ""))[:12]:
                tier_info = ti
                break

        if tier_info is None:
            continue

        content = str(mem.get("content", ""))
        mem_id = str(mem.get("memory_id", ""))
        tags = mem.get("tags") or []
        importance = float(mem.get("importance", 0.5))
        category = str(mem.get("category", "general"))

        if tier_info["assigned_tier"] == "core":
            label = f"pg_core_{mem_id[:8]}"
            is_readonly = "readonly" in tags or category in ("system",)

            # Check if persona block
            block_type = BlockType.KNOWLEDGE
            if category == "system":
                block_type = BlockType.PERSONA

            try:
                manager.core.set_block(
                    label=label,
                    content=content,
                    block_type=block_type,
                    importance=importance,
                    readonly=is_readonly,
                    tags=tags,
                )
                core_count += 1
            except ValueError as e:
                logger.warning("  Core block '%s' skipped: %s", label, e)

        elif tier_info["assigned_tier"] == "recall":
            manager.recall.add_entry(
                content=content,
                label=f"pg_recall_{mem_id[:8]}",
                importance=importance,
                tags=tags,
                source_memory_id=mem_id,
            )
        else:
            block = MemoryBlock(
                block_id=f"pg_arch_{mem_id[:8]}",
                label=f"pg_archival_{mem_id[:8]}",
                content=content,
                tier=MemoryTier.ARCHIVAL,
                block_type=BlockType.KNOWLEDGE,
                importance=importance,
                tags=tags,
                source_memory_id=mem_id,
            )
            manager.archival.archive_block(block)

    logger.info(
        "  Populated: core=%d, recall=%d, archival=%d",
        core_count,
        manager.recall.size,
        manager.archival.size,
    )

    if dry_run:
        logger.info("  DRY RUN — skipping lifecycle execution.")
        return stats

    # ── Step 4: Execute lifecycle ──────────────────────────────
    logger.info("Step 4/4: Executing memory tier lifecycle ...")

    migrations = manager.run_lifecycle()

    for m in migrations:
        if m.from_tier.value == "core" and m.to_tier.value == "recall":
            stats["evictions"] += 1
        elif m.from_tier.value == "recall" and m.to_tier.value == "core":
            stats["promotions"] += 1
        elif m.from_tier.value == "recall" and m.to_tier.value == "archival":
            stats["demotions"] += 1

        logger.info(
            "  MIGRATION | %s → %s | block=%s | reason=%s",
            m.from_tier.value, m.to_tier.value,
            m.block_id[:16], m.reason,
        )

    # ── Final snapshot ─────────────────────────────────────────
    snapshot = manager.snapshot()
    stats["snapshot"] = snapshot

    logger.info("=" * 60)
    logger.info("Lifecycle complete.")
    logger.info(
        "  Evictions: %d | Promotions: %d | Demotions: %d",
        stats["evictions"], stats["promotions"], stats["demotions"],
    )
    core_s = snapshot["core"]["blocks"]
    logger.info(
        "  State: Core=%d blk/%d tok (%s%%), Recall=%d, Archival=%d",
        core_s["total_blocks"], core_s["total_tokens"],
        core_s["utilization_pct"],
        snapshot["recall"]["total_blocks"],
        snapshot["archival"]["total_blocks"],
    )

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False, default=str)
        logger.info("  Stats written to %s", output_file)

    return stats


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Trinity Three-Tier Memory Lifecycle Daemon Task"
    )
    parser.add_argument("--host", default="localhost", help="PostgreSQL host")
    parser.add_argument("--port", type=int, default=5432, help="PostgreSQL port")
    parser.add_argument("--dbname", default="trinity", help="PostgreSQL database name")
    parser.add_argument("--user", default="postgres", help="PostgreSQL user")
    parser.add_argument("--password", default="postgres", help="PostgreSQL password")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, skip lifecycle execution")
    parser.add_argument("--limit", type=int, default=500, help="Max memories to fetch")
    parser.add_argument("--core-token-limit", type=int, default=500,
                        help="Core Memory token limit (default: 500)")
    parser.add_argument("--core-threshold", type=float, default=0.55,
                        help="Minimum score for Core tier (default: 0.55)")
    parser.add_argument("--recall-threshold", type=float, default=0.20,
                        help="Minimum score for Recall tier (default: 0.20)")
    parser.add_argument("--promotion-threshold", type=int, default=5,
                        help="Access count threshold for Recall→Core promotion (default: 5)")
    parser.add_argument("--demotion-threshold", type=float, default=0.02,
                        help="Frequency threshold for Recall→Archival demotion (default: 0.02)")
    parser.add_argument("--w-recency", type=float, default=0.40,
                        help="Recency weight in tier score (default: 0.40)")
    parser.add_argument("--w-importance", type=float, default=0.35,
                        help="Importance weight in tier score (default: 0.35)")
    parser.add_argument("--w-access", type=float, default=0.25,
                        help="Access weight in tier score (default: 0.25)")
    parser.add_argument("--output", default="", help="Save stats JSON to file")
    parser.add_argument("--store", choices=["pg", "sqlite"], default="pg",
                        help="目标存储：pg=PostgreSQL（默认）；sqlite=SQLite 运行时大库（权威，Option A 方向）")
    parser.add_argument("--sqlite-path", default=os.path.expanduser("~/.trinity/store/trinity_store.db"),
                        help="SQLite 大库路径（--store sqlite 时使用）")
    args = parser.parse_args()

    # ── Import modules ────────────────────────────────────────
    (
        MemoryTier, BlockType, MemoryBlock, CoreMemory,
        RecallMemory, ArchivalMemory, MemoryTierManager,
        TierMigrationRecord, create_memory_tier_manager,
        DEFAULT_CORE_TOKEN_LIMIT,
    ) = _import_modules()

    # ── Connect to store ──────────────────────────────────────
    if args.store == "sqlite":
        adapter = connect_sqlite(args.sqlite_path)
    else:
        adapter = connect_postgresql(
            host=args.host, port=args.port, dbname=args.dbname,
            user=args.user, password=args.password,
        )

    # ── Build MemoryTierManager ───────────────────────────────
    manager = MemoryTierManager(
        pg_adapter=adapter,
        core_token_limit=args.core_token_limit,
        promotion_threshold=args.promotion_threshold,
        demotion_threshold=args.demotion_threshold,
        w_recency=args.w_recency,
        w_importance=args.w_importance,
        w_access=args.w_access,
    )
    logger.info("MemoryTierManager initialized: core_limit=%d tokens", args.core_token_limit)

    # ── Run pipeline ──────────────────────────────────────────
    try:
        stats = run_memory_tiers(
            adapter=adapter,
            manager=manager,
            dry_run=args.dry_run,
            limit=args.limit,
            core_threshold=args.core_threshold,
            recall_threshold=args.recall_threshold,
            output_file=args.output,
            store=args.store,
        )

        if not args.output:
            # Print tiering summary
            print(json.dumps({
                "status": "complete",
                "dry_run": args.dry_run,
                "total": stats["total_memories"],
                "core": stats["assigned_core"],
                "recall": stats["assigned_recall"],
                "archival": stats["assigned_archival"],
                "evictions": stats["evictions"],
                "promotions": stats["promotions"],
                "demotions": stats["demotions"],
            }, indent=2, ensure_ascii=False))

        if stats.get("errors"):
            sys.exit(1)

    finally:
        adapter.disconnect()
        logger.info("Disconnected from PostgreSQL")


if __name__ == "__main__":
    main()
