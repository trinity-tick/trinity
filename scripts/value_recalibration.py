#!/usr/bin/env python3
"""value_recalibration.py — 价值驱动编码批量补标（2026-09，EXECUTION 105）

对标 "Learning What to Remember" 多因素价值模型：用 LLM 评估记忆的
五因素价值并写回 importance / importance_score / metadata.value_model。
只处理：active 记忆 +（importance_score <= 0.5 或未打标）——低价值评估区
优先，避免动已高价值标记的记忆。幂等（metadata.value_model=v1 已打标跳过）。
失败静默保留原值（不破坏现状）。

用法:
  python scripts/value_recalibration.py --limit 10 --dry-run
  python scripts/value_recalibration.py --limit 20
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.brain.value_encoder import estimate_value, batch_estimate  # noqa: E402


def connect():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    # 候选：active + 未打标（metadata 无 value_model）或 importance_score <= 0.5
    cur.execute("""
        SELECT memory_id, content, importance_score,
               COALESCE(metadata->>'emotional_salience', '0')::float8 AS emo_sal
        FROM memories
        WHERE status = 'active'
          AND (importance_score IS NULL
               OR (importance_score::float8) <= 0.5  -- 历史列为 text，显式 cast
               OR metadata->>'value_model' IS NULL)
        ORDER BY created_at DESC
        LIMIT %s
    """, (args.limit,))
    rows = cur.fetchall()
    print(f"candidates: {len(rows)}")
    # 2026-09-01（价值闭环）：情绪 salience 混入价值（emotional_consolidation 打的标签）
    emo_map = {str(mid): float(emo or 0) for mid, _c, _i, emo in rows}

    done = skipped = failed = 0
    # 2026-09（EXECUTION 105.10）：批量评估（一次 LLM 调用 5 条，调用次数 -80%）
    batch_size = int(os.environ.get("TRINITY_VALUE_BATCH", "5"))
    imp_map = {str(mid): imp for mid, _c, imp in rows}
    valid = [(mid, content) for mid, content, _imp in rows
             if content is not None and str(content).strip()]
    skipped = len(rows) - len(valid)
    evals = []
    for i in range(0, len(valid), batch_size):
        chunk = valid[i:i + batch_size]
        evals.extend(batch_estimate([str(c) for _, c in chunk]))
    for (mid, content), ev in zip(valid, evals):
        t0 = time.time()
        dt = time.time() - t0
        if not ev:
            print(f"  {mid} EVAL FAILED — keep original")
            failed += 1
            continue
        if args.dry_run:
            print(f"  [dry] {mid} value={ev['value']} imp_old={imp_map.get(str(mid))} factors={ev['factors']} ({dt:.1f}s)")
            done += 1
            continue
        # 2026-09-01（价值闭环）：高情绪记忆价值加权（salience>=0.5 时 +15%/点）
        _emo = emo_map.get(str(mid), 0.0)
        if _emo >= 0.5:
            ev["value"] = min(1.0, float(ev["value"]) * (1.0 + 0.15 * _emo))
            ev["factors"] = dict(ev.get("factors") or {})
            ev["factors"]["emotional_salience"] = round(_emo, 2)
        meta = json.dumps({"value_model": ev["version"],
                           "value_factors": ev["factors"],
                           "value_reason": ev["reason"]}, ensure_ascii=False)
        cur.execute("""
            UPDATE memories
            SET importance = %s,
                importance_score = %s,
                metadata = CASE
                    WHEN jsonb_typeof(metadata) = 'object' THEN metadata || %s::jsonb
                    ELSE '{}'::jsonb || %s::jsonb
                END,
                updated_at = NOW()
            WHERE memory_id = %s
        """, (ev["value"], ev["value"], meta, meta, mid))
        print(f"  {mid} value={ev['value']} (was {imp_map.get(str(mid))}) factors={ev['factors']} ({dt:.1f}s)")
        done += 1
        time.sleep(0.2)  # LLM 限速友好

    print(f"DONE: updated={done} skipped={skipped} failed={failed}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
