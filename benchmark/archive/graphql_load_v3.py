"""P1: GraphQL load test — v3 correct schema fields."""
import json, os, sys, time, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

t0 = time.time()
from trinity.api.graphql_schema import schema
print(f"Schema loaded in {time.time()-t0:.1f}s")

# Correct schema: searchMemories(topK:), memory(memoryId:), agents (no args)
QUERIES = [
    ("health",     "query { health { status version uptimeSeconds componentStatus } }"),
    ("search",     'query { searchMemories(query: "test", topK: 3) { score memory { memoryId content } } }'),
    ("agents",     "query { agents { agentId name status } }"),
    ("diagnostics","query { diagnostics { component health latencyMs } }"),
    ("memory",     'query { memory(memoryId: "mem_test_001") { memoryId content status } }'),
]

def run_one(schema, name, q):
    t0 = time.perf_counter()
    try:
        r = schema.execute_sync(q)
        ms = (time.perf_counter() - t0) * 1000
        return {"query": name, "latency_ms": round(ms, 2), "success": not (r.errors)}
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return {"query": name, "latency_ms": round(ms, 2), "success": False, "error": str(e)[:100]}

def test_level(schema, n, workers, label):
    tasks = [QUERIES[i % len(QUERIES)] for i in range(n)]
    results = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fs = [ex.submit(run_one, schema, t[0], t[1]) for t in tasks]
        for f in as_completed(fs):
            results.append(f.result())
    elapsed = time.perf_counter() - t0
    lats = sorted([r["latency_ms"] for r in results])
    ok = sum(1 for r in results if r["success"])
    return {
        "label": label, "num_queries": n, "concurrency": workers,
        "total_elapsed_s": round(elapsed, 2),
        "throughput_qps": round(n / elapsed, 2),
        "success": ok, "errors": n - ok,
        "error_rate_pct": round((n-ok)/n*100, 2),
        "p50_ms": round(lats[len(lats)//2], 2),
        "p95_ms": round(lats[int(len(lats)*0.95)], 2),
        "p99_ms": round(lats[int(len(lats)*0.99)], 2),
        "min_ms": round(min(lats), 2),
        "max_ms": round(max(lats), 2),
        "mean_ms": round(statistics.mean(lats), 2),
    }

print("Warmup...")
for name, q in QUERIES:
    r = run_one(schema, name, q)
    print(f"  {name}: {r['latency_ms']}ms ok={r['success']}")

levels = [(10, 5, "10 QPS"), (50, 10, "50 QPS"), (100, 20, "100 QPS")]
all_r = []
for n, w, label in levels:
    print(f"  {label} ({n}q/{w}w)...")
    r = test_level(schema, n, w, label)
    all_r.append(r)
    print(f"    p50={r['p50_ms']}ms p95={r['p95_ms']}ms p99={r['p99_ms']}ms "
          f"QPS={r['throughput_qps']} err={r['errors']}")

out = {"test_date": "2026-08-12", "method": "strawberry execute_sync + ThreadPoolExecutor",
       "schema": "trinity.api.graphql_schema",
       "query_types": [q[0] for q in QUERIES], "levels": all_r}

out_dir = str(TRINITY_ROOT / "output")
os.makedirs(out_dir, exist_ok=True)
jp = out_dir + "/graphql_load_results.json"
with open(jp, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

print(f"\nSaved: {jp}")
print(f"\n{'Label':<12} {'P50':<10} {'P95':<10} {'P99':<10} {'QPS':<10} {'Err':<6}")
for r in all_r:
    print(f"{r['label']:<12} {r['p50_ms']:<10} {r['p95_ms']:<10} {r['p99_ms']:<10} {r['throughput_qps']:<10} {r['errors']:<6}")
