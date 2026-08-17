"""
Hybrid Retrieval Benchmark — Pure Vector vs Hybrid (Fusion) Comparison.

Lightweight: 100 memories, 20 queries.
Output: output/bench_hybrid.json
"""

import json, os, sys, statistics, tempfile, time, random

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
logging.disable(logging.CRITICAL)

from trinity import Trinity
from trinity.retrieval import BM25Index

SEED = 42
random.seed(SEED)

TEMPLATES = [
    "User prefers {color} themes with {size} fonts.",
    "Discussed {topic} with {person} on {date}.",
    "Project {name} uses {framework} v{version}.",
    "Agent {agent} completed {task} in {duration}s.",
    "DB query on {table} returned {count} rows.",
    "Deploy to {env} failed: {reason}, rolled back.",
    "Customer {id} reported issue #{issue} about {feature}.",
    "PR #{pr} by {reviewer}: {comments} comments, {decision}.",
    "API {path} → {code} in {latency}ms under {load}.",
    "Training run {run}: {metric}={value} at epoch {epochs}.",
]

FILL = {
    "color": ["dark", "light", "monokai"],
    "size": ["small", "medium", "14px"],
    "date": ["2025-03-15", "2025-06-01", "2025-08-10"],
    "topic": ["auth-flow", "caching", "rate-limiting"],
    "person": ["Alice", "Bob", "Carol"],
    "name": ["Phoenix", "Titan", "Atlas"],
    "framework": ["FastAPI", "Django", "Flask"],
    "version": ["3.1.0", "2.2.5", "4.0.0"],
    "agent": ["file-agent", "browser", "search-agent"],
    "task": ["indexing", "parsing", "filtering"],
    "duration": ["12.3", "0.8", "45.7"],
    "table": ["users", "orders", "events"],
    "count": ["10", "150", "890"],
    "env": ["staging", "production", "dev"],
    "reason": ["OOM", "timeout", "disk full"],
    "id": ["CUST-1", "CUST-2", "CUST-3"],
    "issue": ["100", "101", "102"],
    "feature": ["login", "checkout", "dashboard"],
    "pr": ["4200", "4201", "4202"],
    "reviewer": ["alice", "bob", "carol"],
    "comments": ["3", "12", "0"],
    "decision": ["APPROVED", "CHANGES_REQUESTED"],
    "path": ["/api/v1/users", "/api/v2/auth", "/health"],
    "code": ["200", "404", "500"],
    "latency": ["12", "150", "3"],
    "load": ["100rps", "500rps"],
    "run": ["r42", "r43"],
    "metric": ["accuracy", "f1", "recall"],
    "value": ["0.93", "0.87", "0.72"],
    "epochs": ["10", "25", "50"],
}

def _gen(idx):
    t = TEMPLATES[idx % len(TEMPLATES)]
    for k, vs in FILL.items():
        if "{" + k + "}" in t:
            t = t.replace("{" + k + "}", random.choice(vs), 1)
    return f"[{idx}] {t}"

QUERIES = [
    "code editor theme",
    "auth-flow discussion",
    "FastAPI project",
    "deployment failure",
    "checkout issue",
    "code review approved",
    "API latency health",
    "training accuracy f1",
    "file agent indexing",
    "caching rate-limiting",
    "staging OOM deploy",
    "customer login feature",
    "DB query users",
    "parsing task agent",
    "monokai font editor",
    "PR merge decision",
    "production disk error",
    "dashboard feature issue",
    "Django Phoenix project",
    "Flask Atlas backend",
]

def _pct(data, p):
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    f = int(k); c = k - f
    return s[f] + c * (s[f+1] - s[f]) if f+1 < len(s) else s[-1]

def main():
    out_dir = os.path.join(os.path.dirname(__file__), "..", "output")
    os.makedirs(out_dir, exist_ok=True)

    db_dir = tempfile.mkdtemp(prefix="trinity_bh_")
    db_path = os.path.join(db_dir, "bench.db")
    print(f"DB: {db_path}")

    tri = Trinity(adapter="sqlite", store_path=db_path)

    # populate 100
    print("Populating 100 memories ...")
    t0 = time.perf_counter()
    for i in range(100):
        tri.ingest(content=_gen(i), category="bench", importance=0.5, agent_id="ba")
    pop_t = time.perf_counter() - t0
    print(f"  done in {pop_t:.1f}s")

    # vector baseline
    print("\n=== PURE VECTOR ===")
    v_lats = []
    t0 = time.perf_counter()
    for q in QUERIES:
        tq = time.perf_counter()
        tri.search(q, top_k=10, agent_id="ba")
        v_lats.append((time.perf_counter() - tq) * 1000)
    vt = time.perf_counter() - t0

    vs = {
        "n": len(QUERIES), "total_s": round(vt,3),
        "avg_ms": round(statistics.mean(v_lats),2),
        "p50_ms": round(statistics.median(v_lats),2),
        "p95_ms": round(_pct(v_lats,95),2),
        "qps": round(len(QUERIES)/vt,1),
    }
    print(f"  avg={vs['avg_ms']}ms  p95={vs['p95_ms']}ms  qps={vs['qps']}")

    # hybrid fusion
    print("\n=== HYBRID FUSION ===")
    h_lats = []
    t0 = time.perf_counter()
    for q in QUERIES:
        tq = time.perf_counter()
        tri.search_hybrid(q, top_k=10, strategy="fusion", agent_id="ba")
        h_lats.append((time.perf_counter() - tq) * 1000)
    ht = time.perf_counter() - t0

    hs = {
        "n": len(QUERIES), "total_s": round(ht,3),
        "avg_ms": round(statistics.mean(h_lats),2),
        "p50_ms": round(statistics.median(h_lats),2),
        "p95_ms": round(_pct(h_lats,95),2),
        "qps": round(len(QUERIES)/ht,1),
    }
    print(f"  avg={hs['avg_ms']}ms  p95={hs['p95_ms']}ms  qps={hs['qps']}")

    d_avg = (hs["avg_ms"] - vs["avg_ms"]) / vs["avg_ms"] * 100
    d_qps = (hs["qps"] - vs["qps"]) / vs["qps"] * 100
    d_p95 = hs["p95_ms"] - vs["p95_ms"]
    print(f"\n=== DELTA ===")
    print(f"  latency: {d_avg:+.1f}%  ({vs['avg_ms']} → {hs['avg_ms']} ms)")
    print(f"  QPS:     {d_qps:+.1f}%  ({vs['qps']} → {hs['qps']})")
    print(f"  P95:     {d_p95:+.2f} ms")

    report = {
        "description": "Pure Vector vs Hybrid (Fusion) Retrieval — 100 mem / 20 q",
        "setup": {"memory_count": 100, "query_count": len(QUERIES), "populate_s": round(pop_t,1)},
        "pure_vector": vs,
        "hybrid_fusion": hs,
        "delta": {"avg_latency_pct": round(d_avg,1), "qps_pct": round(d_qps,1), "p95_ms": round(d_p95,2)},
    }
    rp = os.path.join(out_dir, "bench_hybrid.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport: {rp}")

    import shutil; shutil.rmtree(db_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
