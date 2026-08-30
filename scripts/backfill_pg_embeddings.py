# -*- coding: utf-8 -*-
"""PG 融合回填 v2：Ollama bge-m3 GPU 向量回填（~11/s，28k 约 40min）。

用法: python scripts/backfill_pg_embeddings.py --ollama
幂等/断点续传: 只处理 embedding IS NULL；可随时中断重跑。
"""
import sys, os, time, argparse, requests, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity")
os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")

OLLAMA = "http://127.0.0.1:11434"
MODEL = "bge-m3"

def ollama_embed_batch(texts, base=OLLAMA, model=MODEL, retries=3):
    last = None
    for _ in range(retries):
        try:
            r = requests.post(f"{base}/api/embed",
                              json={"model": model, "input": texts}, timeout=120)
            r.raise_for_status()
            return [list(map(float, v)) for v in r.json()["embeddings"]]
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from trinity.adapters.postgresql import PostgreSQLAdapter
    adapter = PostgreSQLAdapter(auto_connect=True)
    adapter.connect()
    try:
        # sanity: ollama up + dim matches vector(1024)
        v = ollama_embed_batch(["sanity check"])
        assert len(v[0]) == 1024, f"dim {len(v[0])} != 1024"
        print(f"ollama {MODEL} OK, dim=1024")
        total_done = 0
        t0 = time.time()
        while True:
            batch = adapter.get_memories_missing_embedding(limit=args.batch)
            if not batch:
                break
            if args.limit and total_done + len(batch) > args.limit:
                batch = batch[: args.limit - total_done]
            try:
                vecs = ollama_embed_batch([m["content"][:512] for m in batch])  # 截断 512：测速 2.1/s vs 800 字符 1.7/s（语义主体在开头）
            except Exception as exc:
                print(f"embed batch failed: {exc}; retry next round")
                time.sleep(2)
                continue
            ok = 0
            for m, vec in zip(batch, vecs):
                try:
                    if adapter.set_embedding(m["memory_id"], vec):
                        ok += 1
                except Exception as exc:
                    print(f"  set_embedding fail {m['memory_id']}: {exc}")
            total_done += ok
            done = adapter.count_embeddings()
            rate = total_done / max(time.time() - t0, 0.001)
            print(f"batch {len(batch)} | {ok} ok | done={done}/28018 | {rate:.1f}/s | eta {max((28018-done)/max(rate,0.01),0)/60:.0f}min")
        print(f"BACKFILL DONE: {adapter.count_embeddings()} embeddings in {time.time()-t0:.1f}s")
        return 0
    finally:
        adapter.disconnect()

if __name__ == "__main__":
    sys.exit(main())