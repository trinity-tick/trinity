# -*- coding: utf-8 -*-
"""情节→语义泛化（EXECUTION 120，大脑化路线图 P2 完成项）。

从 PG 主存储取情节记忆（episodic）→ MemoryReplayTrainer 重放管线
（查询对 + 对比三元组）→ 语义泛化摘要记忆写回 PG + 嵌入质量评估。

用法: python scripts/memory_replay_consolidate.py [--write] [--max 200]
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--max", type=int, default=200)
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")

    from trinity.adapters.postgresql import PostgreSQLAdapter
    from trinity.modules.memory_replay_trainer import MemoryReplayTrainer
    from trinity.core.client._helpers import _get_embedding_engine

    # 1) 从 PG 收集情节记忆（高重要性、非归档）
    a = PostgreSQLAdapter(auto_connect=True)
    a.connect()
    try:
        import psycopg2.extras
        with a._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT memory_id, content, importance, category, tags, created_at
                    FROM memories
                    WHERE status='active' AND importance >= 0.5
                    ORDER BY created_at DESC LIMIT %s
                """, (args.max,))
                rows = [dict(r) for r in cur.fetchall()]
        print(f"episodic memories collected: {len(rows)}")
        if not rows:
            print("no episodic memories"); return 0

        # 2) 构造 trainer（embedding 用 PG 同款引擎）
        eng = _get_embedding_engine()
        def _embed(text):
            try:
                return eng.embed(str(text))
            except Exception:
                return None
        trainer = MemoryReplayTrainer(embedding_engine=_embed)
        # 直接喂收集的记忆
        from trinity.modules.memory_replay_trainer import TrainingMemory
        mems = [TrainingMemory(memory_id=str(r["memory_id"]), content=str(r["content"]),
                category=str(r.get("category") or "general"), importance=float(r.get("importance") or 0.5))
                for r in rows if r.get("content")]
        trainer._collected_memories = mems

        # 3) 生成查询对 + 对比三元组（重放 = 情节→语义线索提取）
        pairs = trainer.generate_query_pairs(mems, queries_per_memory=2)
        triplets = trainer.compute_contrastive_pairs(pairs, mems, negative_count=3)
        print(f"query pairs: {len(pairs)} | contrastive triplets: {len(triplets)}")
        # 2026-09 (EXECUTION 146): 对比强化训练——用三元组批量 Hebbian
        # （正样本强化 + 负样本远离 = 轻量对比学习）
        try:
            from trinity.brain.hebbian import batch_contrastive
            _trip = []
            for _t in triplets[:30]:
                _q = getattr(_t, "query", None)
                _p = getattr(_t, "positive_memory_id", None) or getattr(_t, "positive", None)
                _n = getattr(_t, "negative_memory_id", None) or getattr(_t, "negative", None)
                if _q and _eng is not None:
                    _qv = _eng.embed(str(_q)[:200])
                    _trip.append((_qv, _p, _n))
            if _trip:
                _r = batch_contrastive(a, _trip)
                print(f"contrastive training: {_r}")
        except Exception as e:
            print(f"contrastive skipped: {e}")

        # 4) 语义泛化：从记忆内容提取高频概念词（jieba 分词，滤停用词）
        from collections import Counter
        import jieba
        jieba.setLogLevel(60)
        _STOP = {"is", "was", "what", "why", "you", "about", "the", "and", "for",
                 "with", "this", "that", "from", "have", "has", "not", "are",
                 "memory", "important", "please", "tell", "user", "how", "when"}
        kw_counter = Counter()
        for mem in mems[:100]:
            for w in jieba.cut(str(mem.content or "")):
                w = w.strip().lower()
                if len(w) >= 2 and w not in _STOP and not w.isdigit():
                    kw_counter[w] += 1
        top_kws = [k for k, _ in kw_counter.most_common(10)]
        print(f"semantic keywords: {top_kws}")

        # 5) 写回（--write）：泛化语义记忆（episodic→semantic 产物）
        if args.write and top_kws:
            from trinity import Trinity
            m = Trinity(adapter="postgresql")
            content = f"[semantic-generalization] replay-derived top concepts: {json.dumps(top_kws, ensure_ascii=False)} | from {len(mems)} episodic memories"
            try:
                m.ingest(content, category="semantic-generalization",
                         tags=["semantic", "replay", "generalization"], importance=0.65)
                print("generalization memory persisted")
            except Exception as e:
                print(f"ingest fail: {e}")

        # 6) 评估（如有 embedding）
        try:
            ev = trainer.evaluate_embedding()
            if ev is not None:
                _d = vars(ev) if hasattr(ev, "__dict__") else (ev if isinstance(ev, dict) else {})
                print("embedding eval:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in _d.items() if k in ("recall", "ndcg", "hits", "recall@5", "nDCG@5")})
        except Exception as e:
            print("eval skipped:", e)
        return 0
    finally:
        a.disconnect()

if __name__ == "__main__":
    sys.exit(main())
