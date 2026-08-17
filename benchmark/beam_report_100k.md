# Trinity BEAM Scale Benchmark Report

> Generated: 2026-08-14 13:36:14  
> Environment: PostgreSQL 127.0.0.1:5430/trinity_bench  
> Method: PostgreSQL FTS (`to_tsvector` + `to_tsquery` OR-logic) with `ts_rank` scoring

## Summary

| Scale | Memories | Queries | QPS | P50 (ms) | P95 (ms) | P99 (ms) | Mean Lat (ms) | Recall@5 |
|-------|----------|---------|-----|----------|----------|----------|---------------|----------|
| 100K | 110,000 | 50 | 1.0 | 984.6 | 1224.8 | 1337.3 | 1005.8 | 1.000 |

## Latency Distribution

| Scale | Min (ms) | Max (ms) | Mean (ms) | P50 (ms) | P95 (ms) | P99 (ms) |
|-------|----------|----------|-----------|----------|----------|----------|
| 100K | 830.9 | 1382.2 | 1005.8 | 984.6 | 1224.8 | 1337.3 |

## 100K Scale — Per-Query Details

| # | Topic | Query (truncated) | Latency (ms) | Results | Recall@5 | GT Count |
|---|-------|-------------------|-------------|---------|----------|----------|
| 1 | T0 | paper review SOTA outperforms architecture training | 973.3 | 5 | 1.000 | 11000 |
| 2 | T0 | paper review SOTA model comparison neural technique | 942.8 | 5 | 1.000 | 11000 |
| 3 | T0 | outperforms SOTA percent dataset architecture layers | 857.2 | 5 | 1.000 | 11000 |
| 4 | T0 | quantization paper review outperforms SOTA model technique | 975.4 | 5 | 1.000 | 11000 |
| 5 | T0 | paper review architecture layers training parameters outperf... | 973.3 | 5 | 1.000 | 11000 |
| 6 | T1 | WMS module warehouse picking order-router throughput | 869.5 | 5 | 1.000 | 11000 |
| 7 | T1 | warehouse optimization picking time ROI beam search | 1007.4 | 5 | 1.000 | 11000 |
| 8 | T1 | WMS dock-scheduler multi-warehouse allocation throughput | 896.6 | 5 | 1.000 | 11000 |
| 9 | T1 | inventory SKU warehouse accuracy WMS module throughput | 1144.5 | 5 | 1.000 | 11000 |
| 10 | T1 | warehouse picking batch genetic algorithm orders throughput | 1142.0 | 5 | 1.000 | 11000 |
| 11 | T2 | query optimization rewritten execution time index database | 1090.4 | 5 | 1.000 | 11000 |
| 12 | T2 | PostgreSQL performance tuning query latency index type | 981.3 | 5 | 1.000 | 11000 |
| 13 | T2 | database migration data transfer execution time query | 1043.0 | 5 | 1.000 | 11000 |
| 14 | T2 | query optimization case study execution time method index | 941.3 | 5 | 1.000 | 11000 |
| 15 | T2 | full-text search query execution time reduction database | 1074.3 | 5 | 1.000 | 11000 |
| 16 | T3 | handoff cross-agent task routing capability match score | 919.6 | 5 | 1.000 | 11000 |
| 17 | T3 | agent handoff from browser to app context preserved | 1162.6 | 5 | 1.000 | 11000 |
| 18 | T3 | cross agent routing capability match browser computer agent | 934.3 | 5 | 1.000 | 11000 |
| 19 | T3 | handoff agent task transfer context capability score | 986.0 | 5 | 1.000 | 11000 |
| 20 | T3 | agent collaboration completed steps pending handoff routing | 999.8 | 5 | 1.000 | 11000 |
| 21 | T4 | forgetting curve retention analysis days review interval | 942.6 | 5 | 1.000 | 11000 |
| 22 | T4 | memory consolidation cycle processed merged archived duratio... | 1185.3 | 5 | 1.000 | 11000 |
| 23 | T4 | session boundary summarizing turns consolidated memories the... | 1116.5 | 5 | 1.000 | 11000 |
| 24 | T4 | memory merge deduplication consolidation forgetting retentio... | 1070.7 | 5 | 1.000 | 11000 |
| 25 | T4 | consolidation cycle merged archived forgetting curve analysi... | 874.9 | 5 | 1.000 | 11000 |
| 26 | T5 | LLM inference optimization throughput tokens per second cost | 983.2 | 5 | 1.000 | 11000 |
| 27 | T5 | model serving benchmark tokens per second latency batch | 1060.5 | 5 | 1.000 | 11000 |
| 28 | T5 | embedding model comparison winner scores task LLM | 1166.4 | 5 | 1.000 | 11000 |
| 29 | T5 | inference optimization TensorRT SGLang throughput tokens | 1046.8 | 5 | 1.000 | 11000 |
| 30 | T5 | LLM serving cost per million tokens inference optimization | 927.5 | 5 | 1.000 | 11000 |
| 31 | T6 | configuration preference autosave dark mode user confirmed | 954.8 | 5 | 1.000 | 11000 |
| 32 | T6 | user preference configuration setting confirmed workflow | 845.5 | 5 | 1.000 | 11000 |
| 33 | T6 | prefers dark mode working configuration confirmed preference | 830.9 | 5 | 1.000 | 11000 |
| 34 | T6 | configuration setting preference user confirmed performance | 917.9 | 5 | 1.000 | 11000 |
| 35 | T6 | workflow preference user confirmed configuration dark mode | 1014.7 | 5 | 1.000 | 11000 |
| 36 | T7 | self-improvement log strategy before after metric improvemen... | 1382.2 | 5 | 1.000 | 11000 |
| 37 | T7 | evolution state phase active strategies pattern library | 1024.5 | 5 | 1.000 | 11000 |
| 38 | T7 | evolution pattern library size phase active strategies | 906.1 | 5 | 1.000 | 11000 |
| 39 | T7 | self improvement evolution certified delta strategy boosting | 920.2 | 5 | 1.000 | 11000 |
| 40 | T7 | evolution observed analyzed planned executed certified phase | 886.7 | 5 | 1.000 | 11000 |
| 41 | T8 | skill registry entry activation cost tokens dependencies ver... | 913.2 | 5 | 1.000 | 11000 |
| 42 | T8 | skill definition triggers tools success rate description | 873.1 | 5 | 1.000 | 11000 |
| 43 | T8 | skill evaluation test cases accuracy latency status registry | 1018.1 | 5 | 1.000 | 11000 |
| 44 | T8 | activation cost estimate tokens skill registry dependencies | 1042.0 | 5 | 1.000 | 11000 |
| 45 | T8 | skill registry version dependencies activation cost estimate | 864.9 | 5 | 1.000 | 11000 |
| 46 | T9 | guardian chain check evaluated verdict confidence audit log | 1002.3 | 5 | 1.000 | 11000 |
| 47 | T9 | security audit incident report type severity detected resolv... | 1047.5 | 5 | 1.000 | 11000 |
| 48 | T9 | audit log risk action guardian chain check safety | 1006.8 | 5 | 1.000 | 11000 |
| 49 | T9 | guardian evaluated confidence risk auditor security incident | 1257.1 | 5 | 1.000 | 11000 |
| 50 | T9 | safety audit log guardian chain level rule risk check | 1290.7 | 5 | 1.000 | 11000 |

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
- Results CSV: `benchmark\beam_results_100k.csv`