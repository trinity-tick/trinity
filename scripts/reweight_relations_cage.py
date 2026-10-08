#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reweight_links_cage.py — CAGE 式记忆边权重重估（2026-09-07，实验通道）。

对照 CAGE(Coherence-Aware Graph Encoding)：边强度由"两端语义连贯性"决定，
而非仅抽取共现。目标表 memory_links（source/target=memory_id，含 strength）：
  new = 0.5*old + 0.5*cos(embed(src_content), embed(dst_content))
embedding 不可用/失败 → 保旧值。幂等可回滚（只写 strength 数值列）。

用法:
  python scripts/reweight_relations_cage.py --dry-run --limit 5000
  python scripts/reweight_relations_cage.py --apply --limit 5000
"""
import argparse
import os
import sys
import time
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)


def _pg():
    import psycopg2
    creds = {}
    try:
        import yaml
        with open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig") as fh:
            # 2026-10-03：**修"先过滤后取 refs"**——原写法先按顶层键过滤（`refs` 键已被丢弃），
            # 之后才 `creds.get("refs")` ⇒ **恒空** ⇒ 回落 `postgres` / 空口令 ⇒ 认证失败。
            # 正解：**先合并 refs 到完整 dict，再过滤**。同 scripts/brain_cycle.py 的说明。
            raw = yaml.safe_load(fh) or {}
            raw = {**(raw.get("refs") or {}), **raw}
            creds = {k: v for k, v in raw.items() if k.startswith("TRINITY_PG_")}
    except Exception as _e:
        swallow(__name__, _e)
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST") or creds.get("TRINITY_PG_HOST") or "127.0.0.1",
        port=int(os.environ.get("TRINITY_PG_PORT") or creds.get("TRINITY_PG_PORT") or 5432),
        dbname=os.environ.get("TRINITY_PG_DB") or creds.get("TRINITY_PG_DB") or "trinity",
        user=os.environ.get("TRINITY_PG_USER") or creds.get("TRINITY_PG_USER") or "postgres",
        password=os.environ.get("TRINITY_PG_PASSWORD") or creds.get("TRINITY_PG_PASSWORD") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()
    if args.apply == args.dry_run:
        print("choose one of --dry-run / --apply")
        return 2
    conn = _pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM memory_links")
    total = cur.fetchone()[0]
    print("memory_links total:", total, "limit:", args.limit)
    cur.execute(
        "SELECT ml.id, ml.source_id, ml.target_id, ml.strength, "
        "COALESCE(s.content,'') AS src_c, COALESCE(t.content,'') AS dst_c "
        "FROM memory_links ml LEFT JOIN memories s ON s.memory_id = ml.source_id "
        "LEFT JOIN memories t ON t.memory_id = ml.target_id "
        "ORDER BY ml.created_at DESC LIMIT %s", (args.limit,))
    rows = cur.fetchall()
    print("sample rows:", len(rows))
    # embedding engine（本地 sklearn/缓存；失败则全部保旧）
    embed = None
    try:
        from trinity.embeddings.engine import create_engine
        eng = create_engine(backend="sklearn")
        def embed(t):  # t76：原为 lambda（曾被一条 E731 抑制指令压掉）；改 def 去掉抑制，语义不变
            return eng.embed(t)
    except Exception as exc:
        print("embedding engine unavailable:", exc)
    import numpy as np
    cache = {}
    changed = same = noemb = 0
    t0 = time.time()
    for rid, sid, oid, old, src_c, dst_c in rows:
        if not src_c or not dst_c or not embed:
            noemb += 1
            continue
        def _vec(text):
            if text not in cache:
                try:
                    v = np.asarray(embed(str(text)[:400]), dtype=np.float32).flatten()
                    n = np.linalg.norm(v)
                    cache[text] = v / n if n > 0 else v
                except Exception:
                    cache[text] = None
            return cache[text]
        vs, vo = _vec(src_c), _vec(dst_c)
        if vs is None or vo is None:
            noemb += 1
            continue
        cos = float(np.dot(vs, vo))
        old_f = float(old) if old is not None else 0.5
        new_f = round(0.5 * old_f + 0.5 * cos, 4)
        if abs(new_f - old_f) < 1e-4:
            same += 1
            continue
        changed += 1
        if args.apply:
            cur.execute("UPDATE memory_links SET strength=%s WHERE id=%s", (new_f, rid))
    print(f"done rows={len(rows)} changed={changed} same={same} no_embed={noemb} elapsed={time.time()-t0:.1f}s")
    if args.apply:
        print("APPLIED (only strength column updated; entities/relations 内容未动)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
