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
        create_llm_compress_callable,
    )
    from trinity.adapters.postgresql import PostgreSQLAdapter
    from trinity.adapters.sqlite import SQLiteAdapter
    return (
        DecayConfig, DecayStatus, MemoryDecayEngine, DecayResult, DecayScanReport,
        MemoryCompressor, CompressionStatus, CompressionReport, mock_llm_compress,
        create_llm_compress_callable, PostgreSQLAdapter, SQLiteAdapter,
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

def fetch_active_memories_sqlite(adapter: Any, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch active (non-archived) memories from the SQLite runtime store."""
    conn = getattr(adapter, "_conn", None)
    if conn is None:
        return []
    rows = conn.execute("""
        SELECT memory_id, session_id, persona_id, tenant_id,
               content, role, importance, tags, category,
               sha256_hash, status, version, created_at, updated_at,
               access_count, last_accessed_at
        FROM memories
        WHERE status = 'active'
        ORDER BY access_count ASC, created_at ASC
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
    # 2026-08-17（记忆周期优化 P0-1）：大库被 api/mcp/collector 等常驻进程
    # 共享写入时，connect() 建表/INSERT tenants 可能撞写锁（8-16 每日链全挂
    # 根因 database is locked）。busy_timeout=30s 让建表与写入等待锁释放。
    try:
        conn = getattr(adapter, "_conn", None)
        if conn is not None:
            conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    logger.info("Connected to SQLite store: %s (busy_timeout=30s)", db_path)
    return adapter


def fetch_active_memories(adapter: Any, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch all active (non-archived) memories from PostgreSQL."""
    import psycopg2.extras

    with adapter._get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT memory_id, session_id, persona_id, tenant_id,
                       content, role, importance, tags, category,
                       sha256_hash, status, version, created_at, updated_at,
                       access_count, last_accessed_at
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


# ── Real-LLM Summary Generation（2026-08-18，decay 压缩接真实 LLM）──────
# 默认仍走 mock（不设任何 decay 专用 key 时与现状完全一致）；设置
# TRINITY_DECAY_API_KEY（或 TRINITY_API_KEY）后 auto → real，对"待压缩记忆组"
# 生成 5-10 行结构化中文摘要。摘要生成抽成可注入函数 _llm_summarize，
# 单测可 monkey-patch / 注入假实现，绝不真调外部 API。

# 中文结构化摘要 system prompt：要求 5-10 行、保留事实/时间/数值、不编造。
_DECAY_SUMMARY_SYSTEM_PROMPT = """你是一个记忆压缩引擎。请把下面的多条待压缩记忆合并为一篇简洁、
结构化的中文摘要。

要求：
1. 摘要输出 5 到 10 行，每行一句，条理清晰。
2. 必须保留：事实、时间/日期、数值/金额/度量、实体名、关键决策与结论。
3. 不得编造原文没有的信息；不确定的信息不要补全。
4. 删除冗余表述与重复内容。
5. 只输出摘要正文本身；若确实需要，可输出一个 JSON 对象
   {"summary": "<摘要正文>"}，但不要输出任何其他解释文字或 markdown 标题。"""


def _build_summary_user_prompt(
    texts: List[str],
    memory_type: str = "general",
) -> str:
    """构建中文结构化摘要的用户 prompt（把待压缩记忆内容逐条列出）。"""
    lines = []
    for i, t in enumerate(texts, 1):
        lines.append(f"[{i}] {str(t)[:600]}")
    return (
        f"待压缩记忆类型：{memory_type}\n"
        f"共 {len(texts)} 条记忆：\n"
        + "\n".join(lines)
        + "\n\n请输出结构化中文摘要（保留事实/时间/数值，不要编造）："
    )


def _mock_batch_summary(texts: List[str], memory_type: str = "general") -> str:
    """mock 降级摘要：抽取式拼接，输出格式与 trinity.daemon.memory_compressor.
    mock_llm_compress 完全一致（保证默认行为不变）。"""
    if not texts:
        return "Compressed summary not available (no entries)."
    snippets = [str(t)[:120] for t in texts]
    combined = " | ".join(snippets[:5])
    return f"[AUTO-COMPRESSED] {len(texts)} memories merged: {combined[:1500]}"


def _parse_summary_response(raw: str) -> str:
    """解析 LLM 摘要响应：先试 JSON {summary: ...}，失败用整段文本。"""
    if not raw or not raw.strip():
        return ""
    t = raw.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:].strip()
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            s = data.get("summary")
            if s and str(s).strip():
                return str(s).strip()
    except (ValueError, TypeError):
        pass
    try:
        # 容错：可能是截断的 JSON，先尝试补全右花括号再解析
        data = json.loads(t + "}" * (t.count("{") - t.count("}")))
        if isinstance(data, dict):
            s = data.get("summary")
            if s and str(s).strip():
                return str(s).strip()
    except (ValueError, TypeError):
        logger.warning("decay LLM summary JSON parse failed, fallback to raw text: %s", raw[:120])
    return t


def _llm_chat(system: str, user: str, cfg: Dict[str, Any], timeout: float = 60.0) -> str:
    """调用 OpenAI 兼容 /chat/completions（stdlib urllib）。

    风格复用 trinity.memory.proposition_extractor（无 key 抛异常由调用方降级）。
    Returns:
        LLM 返回的 content 文本。
    """
    import urllib.request

    api_key = cfg.get("api_key")
    if not api_key:
        raise RuntimeError("no decay LLM key configured")
    payload = {
        "model": cfg.get("model") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("TRINITY_DECAY_MAX_TOKENS", "1500")),
    }
    req = urllib.request.Request(
        (cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as e:
        raise RuntimeError(f"decay LLM 响应格式异常: {body}") from e


def _llm_summarize(
    texts: List[str],
    cfg: Dict[str, Any],
    memory_type: str = "general",
) -> str:
    """对待压缩记忆组生成摘要（可注入 / 可替换的核心函数）。

    Args:
        texts: 待压缩记忆的内容列表。
        cfg: LLM 配置 dict，含 mode/api_key/base_url/model。
        memory_type: 记忆类型（仅用于 prompt 提示）。

    Returns:
        摘要文本。real 模式缺 key、调用失败或返回空 → 逐条降级 mock（不抛异常，
        不中断任务）；mock 模式与现状完全一致。
    """
    mode = cfg.get("mode", "mock")
    if mode == "real" and cfg.get("api_key"):
        try:
            raw = _llm_chat(
                _DECAY_SUMMARY_SYSTEM_PROMPT,
                _build_summary_user_prompt(texts, memory_type),
                cfg,
            )
            if raw and raw.strip():
                summary = _parse_summary_response(raw)
                if summary and summary.strip():
                    return summary
            logger.warning("decay real LLM returned empty summary, fallback mock")
        except Exception as e:  # noqa: BLE001
            logger.warning("decay real LLM summary failed: %s, fallback mock", e)
    return _mock_batch_summary(texts, memory_type)


def _resolve_llm_mode(requested: str) -> str:
    """解析 --llm 模式：auto 根据 decay 专用 key 是否存在决定 real/mock。

    Env: TRINITY_DECAY_API_KEY 或 TRINITY_API_KEY 存在 → real；否则 mock。
    显式 --llm mock/real 强制。
    """
    if requested in ("mock", "real"):
        return requested
    # 2026-08-27（Claude-Mem P1）：维护链注入的是 TRINITY_LLM_API_KEY（凭证兜底），
    # 一并识别——否则 auto 永远解析为 mock（真实 LLM 摘要空转）。
    has_key = bool(
        os.environ.get("TRINITY_DECAY_API_KEY")
        or os.environ.get("TRINITY_API_KEY")
        or os.environ.get("TRINITY_LLM_API_KEY")
    )
    return "real" if has_key else "mock"


def _build_llm_cfg(mode: str, model_override: str = "") -> Dict[str, Any]:
    """构造 LLM 配置 dict。

    base_url 默认 https://api.deepseek.com/v1；model 默认 deepseek-chat。
    """
    return {
        "mode": mode,
        "api_key": os.environ.get("TRINITY_DECAY_API_KEY")
        or os.environ.get("TRINITY_API_KEY"),
        "base_url": (
            os.environ.get("TRINITY_DECAY_BASE_URL") or "https://api.deepseek.com/v1"
        ).rstrip("/"),
        "model": model_override or os.environ.get("TRINITY_DECAY_MODEL") or "deepseek-chat",
    }


def _resolve_llm_config(requested: str, model_override: str = "") -> tuple:
    """返回 (mode, cfg) —— auto 解析后的最终模式与 LLM 配置。"""
    mode = _resolve_llm_mode(requested)
    return mode, _build_llm_cfg(mode, model_override)


def _prompt_entries_to_texts(user_prompt: str) -> List[str]:
    """从 compressor 的 user_prompt 中抽取记忆内容字符串列表。

    复用 mock_llm_compress 的解析规则（匹配 '[n] ' / '] ' 行）。
    """
    texts: List[str] = []
    for line in user_prompt.split("\n"):
        line = line.strip()
        if line.startswith("[") and "] " in line:
            texts.append(line.split("] ", 1)[1])
    return texts


def _make_llm_callable(_llm_summarize: Any, cfg: Dict[str, Any], mock_ctor: Any) -> Any:
    """构造注入 MemoryCompressor 的 llm_callable。

    实模式：忽略 compressor 内置英文 prompt，改走 _llm_summarize（中文结构化摘要，
    单条失败自动降级 mock，不中断任务）。mock 模式：直接用 mock_ctor（现状不变）。
    Returns:
        (system_prompt, user_prompt) -> str
    """
    if cfg.get("mode") == "real":
        def _real_call(system_prompt: str, user_prompt: str) -> str:  # noqa: ARG001
            texts = _prompt_entries_to_texts(user_prompt)
            return _llm_summarize(texts, cfg)
        return _real_call
    return mock_ctor


# ── Main Pipeline ─────────────────────────────────────────────────────

def run_decay_compress(
    adapter: Any,
    engine: Any,        # MemoryDecayEngine
    compressor: Any,    # MemoryCompressor
    dry_run: bool = False,
    limit: int = 500,
    store: str = "pg",
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
    logger.info("Step 1/4: Fetching active memories from %s ...", store)
    if store == "sqlite":
        memories = fetch_active_memories_sqlite(adapter, limit=limit)
    else:
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

            if result.status.name == "SUCCESS":  # 不引用 CompressionStatus（它只在 main() 局部绑定）
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
    parser.add_argument("--store", choices=["pg", "sqlite"], default="pg",
                        help="目标存储：pg=PostgreSQL（默认，维护/批处理层）；"
                             "sqlite=SQLite 运行时大库（权威，Option A 方向）")
    parser.add_argument("--sqlite-path", default=os.path.expanduser("~/.trinity/store/trinity_store.db"),
                        help="SQLite 大库路径（--store sqlite 时使用）")
    parser.add_argument("--llm", choices=["mock", "real", "auto"], default="auto",
                        help="压缩器 LLM：auto（默认，有 TRINITY_DECAY_API_KEY/TRINITY_API_KEY 则 real，"
                             "否则回退 mock）、mock（离线抽取式摘要）或 real（OpenAI 兼容 API，"
                             "需 TRINITY_DECAY_API_KEY/TRINITY_API_KEY 环境变量）")
    parser.add_argument("--llm-model", default="",
                        help="真实 LLM 模型名（缺省读 TRINITY_DECAY_MODEL，再缺省 deepseek-chat）")
    args = parser.parse_args()

    # ── Build compressor ─────────────────────────────────────
    (
        DecayConfig, DecayStatus, MemoryDecayEngine, DecayResult, DecayScanReport,
        MemoryCompressor, CompressionStatus, CompressionReport, mock_llm_compress,
        create_llm_compress_callable, PostgreSQLAdapter, SQLiteAdapter,
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

    # ── Connect to store ──────────────────────────────────────
    if args.store == "sqlite":
        adapter = connect_sqlite(args.sqlite_path)
    else:
        adapter = connect_postgresql(
            host=args.host, port=args.port, dbname=args.dbname,
            user=args.user, password=args.password,
        )

    # ── Build compressor ─────────────────────────────────────
    # auto（生产默认）：decay 专用 key（TRINITY_DECAY_API_KEY 或 TRINITY_API_KEY）
    # 存在 → real（生成中文结构化摘要，单条失败自动降级 mock）；否则 mock，
    # 与现状完全一致，无人值守维护链永不因缺 key 崩溃。
    # base_url 默认 https://api.deepseek.com/v1，model 默认 deepseek-chat。
    _llm_mode, _llm_cfg = _resolve_llm_config(args.llm, model_override=args.llm_model)
    if _llm_mode == "real":
        llm_callable = _make_llm_callable(_llm_summarize, _llm_cfg, mock_llm_compress)
        logger.info("Compressor initialized (REAL LLM mode, model=%s)", _llm_cfg.get("model"))
    else:
        llm_callable = mock_llm_compress
        logger.info("Compressor initialized (mock LLM mode)")

    compressor = MemoryCompressor(
        pg_adapter=adapter,
        llm_callable=llm_callable,
    )

    # ── Run pipeline ──────────────────────────────────────────
    try:
        stats = run_decay_compress(
            adapter=adapter,
            engine=engine,
            compressor=compressor,
            dry_run=args.dry_run,
            limit=args.limit,
            store=args.store,
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
        logger.info("Disconnected from %s", "SQLite" if args.store == "sqlite" else "PostgreSQL")


if __name__ == "__main__":
    main()
