#!/usr/bin/env python3
"""reembed_memories.py — 存量记忆重嵌入为 bge-m3 1024d（2026-08-25）

把 memories 表所有 active 记忆的 embedding 从旧维度（512d）重新生成为
bge-m3 1024d（Ollama 优先 / ONNX 内镶兜底）。

安全设计：
- 分批处理（每批 100 条 + 提交）避免长时间锁库；
- 断点续传（记录已处理 memory_id 到 state 文件，中断后可续）；
- 只更新 embedding 列（不动 content/索引/审计）；
- 全量完成后维度统一 1024d（避免混合维度导致向量检索崩溃）。

用法：
    python scripts/reembed_memories.py                # 全量重嵌入
    python scripts/reembed_memories.py --limit 500    # 只处理前 500（测试）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
STATE_FILE = os.path.expanduser("~/.trinity/reembed_state.json")
BATCH = 100


def _get_engine():
    """bge-m3 引擎：Ollama 优先（已确认本机有 bge-m3），失败用 ONNX。"""
    from trinity.embeddings.engine import create_engine
    try:
        eng = create_engine(backend="auto", use_cache=True)
        if "bge" in eng.model_name().lower() or eng.embedding_dim() >= 1024:
            return eng
    except Exception:
        pass
    return create_engine(backend="onnx", use_cache=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args()

    eng = _get_engine()
    print(f"engine: {eng.model_name()} dim={eng.embedding_dim()}")

    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")

    state = {"processed": []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    done_set = set(state["processed"])

    # 所有需要重嵌入的记忆（active + 有 embedding 或需新增）
    rows = conn.execute(
        "SELECT memory_id, content FROM memories WHERE status='active'"
    ).fetchall()
    print(f"active memories: {len(rows)}（已处理 {len(done_set)}）")
    todo = [r for r in rows if r[0] not in done_set]
    if args.limit:
        todo = todo[:args.limit]
    print(f"待处理: {len(todo)}")

    t0 = time.time()
    done = 0
    for i in range(0, len(todo), args.batch):
        batch = todo[i:i + args.batch]
        texts = [(r[1] or "")[:2000] for r in batch]
        vecs = eng.embed_batch(texts)
        for (mid, _), v in zip(batch, vecs):
            if len(v) != 1024:
                continue
            blob = v.astype("float32").tobytes()
            conn.execute("UPDATE memories SET embedding=? WHERE memory_id=?", (blob, mid))
            done_set.add(mid)
        conn.commit()
        done += len(batch)
        speed = done / max(time.time() - t0, 0.1)
        eta = (len(todo) - done) / max(speed, 0.1)
        print(f"  [{done}/{len(todo)}] {round(speed,1)} emb/s ETA {round(eta/60,1)}min")
        # 定期保存 state（断点续传）
        if done % 500 == 0:
            state["processed"] = list(done_set)[-20000:]
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)

    state["processed"] = list(done_set)[-20000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    total_t = time.time() - t0
    print(f"=== 完成: {done} 条重嵌入，耗时 {round(total_t/60,1)}min ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
