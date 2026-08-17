#!/usr/bin/env python3
"""
Trinity — A5 压缩经济学报告（2026-08-15）
============================================
采样大库长记忆，对比 mock 与真实 LLM（DeepSeek）压缩的
token 节省 vs 信息保留，产出成本-质量曲线数据。

用法：
    python scripts/compress_economics_report.py                 # mock + real（需 TRINITY_LLM_*）
    python scripts/compress_economics_report.py --mock-only
    python scripts/compress_economics_report.py --samples 20
    python scripts/compress_economics_report.py --output .trinity/logs/compress_econ.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("compress_economics")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算（CJK 每字 1 token + 空白词）。"""
    import re
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + words


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity A5 compression economics")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--min-len", type=int, default=120, help="采样最小内容长度（字符）")
    parser.add_argument("--mock-only", action="store_true")
    parser.add_argument("--output", default=os.path.expanduser("~/.trinity/logs/compress_econ.json"))
    args = parser.parse_args()

    import sqlite3
    conn = sqlite3.connect(args.sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, content FROM memories WHERE status='active' "
        "AND length(content) >= ? ORDER BY length(content) DESC LIMIT ?",
        (args.min_len, args.samples * 4),
    ).fetchall()
    conn.close()
    # 取长度覆盖广的样本（均匀采样）
    samples = rows[:: max(1, len(rows) // args.samples)][: args.samples]
    logger.info("sampled %d long memories", len(samples))

    from trinity.daemon.memory_compressor import mock_llm_compress, create_llm_compress_callable

    real_llm = None
    if not args.mock_only:
        try:
            real_llm = create_llm_compress_callable()
            logger.info("real LLM callable ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("real LLM unavailable: %s", exc)

    entries = []
    t0 = time.time()
    for i, row in enumerate(samples):
        content = row["content"]
        orig_tokens = _estimate_tokens(content)
        entry = {"memory_id": row["memory_id"], "orig_tokens": orig_tokens}

        # mock 压缩
        try:
            mock_out = mock_llm_compress("压缩以下记忆", content)
            entry["mock_compressed_tokens"] = _estimate_tokens(mock_out)
            entry["mock_save_pct"] = round(
                100 * (1 - entry["mock_compressed_tokens"] / max(1, orig_tokens)), 1
            )
        except Exception as exc:  # noqa: BLE001
            entry["mock_error"] = str(exc)[:80]

        # real LLM 压缩
        if real_llm is not None:
            try:
                real_out = real_llm("压缩以下记忆为简洁摘要，保留关键事实", content)
                entry["real_compressed_tokens"] = _estimate_tokens(real_out)
                entry["real_save_pct"] = round(
                    100 * (1 - entry["real_compressed_tokens"] / max(1, orig_tokens)), 1
                )
            except Exception as exc:  # noqa: BLE001
                entry["real_error"] = str(exc)[:80]
        entries.append(entry)
        if (i + 1) % 5 == 0:
            logger.info("processed %d/%d", i + 1, len(samples))

    def _avg(key):
        vals = [e[key] for e in entries if key in e and isinstance(e.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    report = {
        "benchmark": "A5 compression-economics",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "samples": len(entries),
        "mock": {"avg_save_pct": _avg("mock_save_pct")},
        "real_llm": {"avg_save_pct": _avg("real_save_pct")},
        "notes": "save_pct = 100*(1 - compressed/orig tokens); 质量保留为抽样人工/LLM 复核项",
        "entries": entries,
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        logger.info("report written: %s", args.output)
    print(json.dumps({k: v for k, v in report.items() if k != "entries"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
