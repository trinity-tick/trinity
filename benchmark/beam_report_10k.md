# Trinity BEAM Scale Benchmark Report

> Generated: 2026-08-14 13:34:49  
> Environment: PostgreSQL 127.0.0.1:5430/trinity_bench  
> Method: PostgreSQL FTS (`to_tsvector` + `to_tsquery` OR-logic) with `ts_rank` scoring

## Summary

| Scale | Memories | Queries | QPS | P50 (ms) | P95 (ms) | P99 (ms) | Mean Lat (ms) | Recall@5 |
|-------|----------|---------|-----|----------|----------|----------|---------------|----------|
| 10K | 10,000 | 50 | 4.1 | 240.0 | 273.9 | 291.6 | 242.5 | 1.000 |

## Latency Distribution

| Scale | Min (ms) | Max (ms) | Mean (ms) | P50 (ms) | P95 (ms) | P99 (ms) |
|-------|----------|----------|-----------|----------|----------|----------|
| 10K | 214.2 | 302.3 | 242.5 | 240.0 | 273.9 | 291.6 |

## 10K Scale — Per-Query Details

| # | Topic | Query (truncated) | Latency (ms) | Results | Recall@5 | GT Count |
|---|-------|-------------------|-------------|---------|----------|----------|
| 1 | T0 | paper review SOTA outperforms architecture training | 239.3 | 5 | 1.000 | 1000 |
| 2 | T0 | paper review SOTA model comparison neural technique | 253.5 | 5 | 1.000 | 1000 |
| 3 | T0 | outperforms SOTA percent dataset architecture layers | 220.6 | 5 | 1.000 | 1000 |
| 4 | T0 | quantization paper review outperforms SOTA model technique | 266.5 | 5 | 1.000 | 1000 |
| 5 | T0 | paper review architecture layers training parameters outperf... | 241.6 | 5 | 1.000 | 1000 |
| 6 | T1 | WMS module warehouse picking order-router throughput | 221.8 | 5 | 1.000 | 1000 |
| 7 | T1 | warehouse optimization picking time ROI beam search | 237.9 | 5 | 1.000 | 1000 |
| 8 | T1 | WMS dock-scheduler multi-warehouse allocation throughput | 220.7 | 5 | 1.000 | 1000 |
| 9 | T1 | inventory SKU warehouse accuracy WMS module throughput | 279.7 | 5 | 1.000 | 1000 |
| 10 | T1 | warehouse picking batch genetic algorithm orders throughput | 240.7 | 5 | 1.000 | 1000 |
| 11 | T2 | query optimization rewritten execution time index database | 251.2 | 5 | 1.000 | 1000 |
| 12 | T2 | PostgreSQL performance tuning query latency index type | 249.8 | 5 | 1.000 | 1000 |
| 13 | T2 | database migration data transfer execution time query | 250.4 | 5 | 1.000 | 1000 |
| 14 | T2 | query optimization case study execution time method index | 247.4 | 5 | 1.000 | 1000 |
| 15 | T2 | full-text search query execution time reduction database | 245.0 | 5 | 1.000 | 1000 |
| 16 | T3 | handoff cross-agent task routing capability match score | 234.2 | 5 | 1.000 | 1000 |
| 17 | T3 | agent handoff from browser to app context preserved | 266.8 | 5 | 1.000 | 1000 |
| 18 | T3 | cross agent routing capability match browser computer agent | 280.4 | 5 | 1.000 | 1000 |
| 19 | T3 | handoff agent task transfer context capability score | 243.4 | 5 | 1.000 | 1000 |
| 20 | T3 | agent collaboration completed steps pending handoff routing | 302.3 | 5 | 1.000 | 1000 |
| 21 | T4 | forgetting curve retention analysis days review interval | 247.1 | 5 | 1.000 | 1000 |
| 22 | T4 | memory consolidation cycle processed merged archived duratio... | 262.0 | 5 | 1.000 | 1000 |
| 23 | T4 | session boundary summarizing turns consolidated memories the... | 225.5 | 5 | 1.000 | 1000 |
| 24 | T4 | memory merge deduplication consolidation forgetting retentio... | 223.2 | 5 | 1.000 | 1000 |
| 25 | T4 | consolidation cycle merged archived forgetting curve analysi... | 223.0 | 5 | 1.000 | 1000 |
| 26 | T5 | LLM inference optimization throughput tokens per second cost | 254.0 | 5 | 1.000 | 1000 |
| 27 | T5 | model serving benchmark tokens per second latency batch | 262.6 | 5 | 1.000 | 1000 |
| 28 | T5 | embedding model comparison winner scores task LLM | 258.1 | 5 | 1.000 | 1000 |
| 29 | T5 | inference optimization TensorRT SGLang throughput tokens | 238.8 | 5 | 1.000 | 1000 |
| 30 | T5 | LLM serving cost per million tokens inference optimization | 246.3 | 5 | 1.000 | 1000 |
| 31 | T6 | configuration preference autosave dark mode user confirmed | 231.6 | 5 | 1.000 | 1000 |
| 32 | T6 | user preference configuration setting confirmed workflow | 227.4 | 5 | 1.000 | 1000 |
| 33 | T6 | prefers dark mode working configuration confirmed preference | 214.2 | 5 | 1.000 | 1000 |
| 34 | T6 | configuration setting preference user confirmed performance | 235.0 | 5 | 1.000 | 1000 |
| 35 | T6 | workflow preference user confirmed configuration dark mode | 231.7 | 5 | 1.000 | 1000 |
| 36 | T7 | self-improvement log strategy before after metric improvemen... | 234.6 | 5 | 1.000 | 1000 |
| 37 | T7 | evolution state phase active strategies pattern library | 228.7 | 5 | 1.000 | 1000 |
| 38 | T7 | evolution pattern library size phase active strategies | 249.5 | 5 | 1.000 | 1000 |
| 39 | T7 | self improvement evolution certified delta strategy boosting | 224.2 | 5 | 1.000 | 1000 |
| 40 | T7 | evolution observed analyzed planned executed certified phase | 220.3 | 5 | 1.000 | 1000 |
| 41 | T8 | skill registry entry activation cost tokens dependencies ver... | 229.1 | 5 | 1.000 | 1000 |
| 42 | T8 | skill definition triggers tools success rate description | 234.0 | 5 | 1.000 | 1000 |
| 43 | T8 | skill evaluation test cases accuracy latency status registry | 266.2 | 5 | 1.000 | 1000 |
| 44 | T8 | activation cost estimate tokens skill registry dependencies | 233.0 | 5 | 1.000 | 1000 |
| 45 | T8 | skill registry version dependencies activation cost estimate | 248.8 | 5 | 1.000 | 1000 |
| 46 | T9 | guardian chain check evaluated verdict confidence audit log | 231.6 | 5 | 1.000 | 1000 |
| 47 | T9 | security audit incident report type severity detected resolv... | 224.6 | 5 | 1.000 | 1000 |
| 48 | T9 | audit log risk action guardian chain check safety | 226.1 | 5 | 1.000 | 1000 |
| 49 | T9 | guardian evaluated confidence risk auditor security incident | 243.3 | 5 | 1.000 | 1000 |
| 50 | T9 | safety audit log guardian chain level rule risk check | 256.0 | 5 | 1.000 | 1000 |

## Methodology

- **Backend**: PostgreSQL FTS with `pg_trgm` extension, `simple` text search configuration
- **Query Set**: 50 queries (5 per topic × 10 topics), each query targets a specific topic cluster
- **Ground Truth**: Memories tagged with matching `[topic:T#]` marker in content
- **Recall@5**: |top-5 ∩ ground_truth| / min(5, |ground_truth|)
- **Latency**: Wall-clock time per single query execution (includes network + query + fetch)
- **QPS**: Total queries / total wall-clock time (sequential execution)

## Notes

- Benchmark runs queries sequentially (single-threaded). Parallel QPS would scale with connection pool size.
- PostgreSQL `ts_rank` uses TF-IDF-like scoring; ranking quality depends on term frequency distribution.
- For 100K scale, ensure PostgreSQL has adequate `shared_buffers` and `work_mem` for index scan performance.
- Results CSV: `benchmark\beam_results_10k.csv`