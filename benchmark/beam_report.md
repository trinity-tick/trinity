---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_7cd197c9927511f1bcfc525400e6dd8f
    ReservedCode1: MmpwW95frjMVU8JY1BJd5D3j4KUIg/ioU8AbvC4m6a+tq0tYNrIT1o36JEJoNYQi7t8vLQQ/Pvuy9Xshtdbxjqrcw8GrLTVimXjREZsKC+L3mwiZhYsXdnXFFbN1iMINz0TJJCJFMipKv+ajykeaiQ7R/8reMXNf5meAX7VNUgsfJMXBNiRFs5GNAjQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_7cd197c9927511f1bcfc525400e6dd8f
    ReservedCode2: MmpwW95frjMVU8JY1BJd5D3j4KUIg/ioU8AbvC4m6a+tq0tYNrIT1o36JEJoNYQi7t8vLQQ/Pvuy9Xshtdbxjqrcw8GrLTVimXjREZsKC+L3mwiZhYsXdnXFFbN1iMINz0TJJCJFMipKv+ajykeaiQ7R/8reMXNf5meAX7VNUgsfJMXBNiRFs5GNAjQ=
---

# Trinity BEAM Scale Benchmark Report

> Generated: 2026-08-07 23:32:46  
> Environment: PostgreSQL 127.0.0.1:5432/trinity  
> Method: PostgreSQL FTS (`to_tsvector` + `plainto_tsquery`) with `ts_rank` scoring

## Summary

| Scale | Memories | Queries | QPS | P50 (ms) | P95 (ms) | P99 (ms) | Mean Lat (ms) | Recall@5 |
|-------|----------|---------|-----|----------|----------|----------|---------------|----------|
| 1K | 1,029 | 50 | 100.0 | 8.7 | 13.7 | 34.3 | 10.0 | 1.000 |

## Latency Distribution

| Scale | Min (ms) | Max (ms) | Mean (ms) | P50 (ms) | P95 (ms) | P99 (ms) |
|-------|----------|----------|-----------|----------|----------|----------|
| 1K | 5.3 | 52.0 | 10.0 | 8.7 | 13.7 | 34.3 |

## 1K Scale — Per-Query Details

| # | Topic | Query (truncated) | Latency (ms) | Results | Recall@5 | GT Count |
|---|-------|-------------------|-------------|---------|----------|----------|
| 1 | T0 | paper review SOTA outperforms architecture training | 9.7 | 5 | 1.000 | 100 |
| 2 | T0 | paper review SOTA model comparison neural technique | 11.9 | 5 | 1.000 | 100 |
| 3 | T0 | outperforms SOTA percent dataset architecture layers | 5.7 | 5 | 1.000 | 100 |
| 4 | T0 | quantization paper review outperforms SOTA model technique | 12.0 | 5 | 1.000 | 100 |
| 5 | T0 | paper review architecture layers training parameters outperf... | 10.3 | 5 | 1.000 | 100 |
| 6 | T1 | WMS module warehouse picking order-router throughput | 8.3 | 5 | 1.000 | 100 |
| 7 | T1 | warehouse optimization picking time ROI beam search | 7.6 | 5 | 1.000 | 100 |
| 8 | T1 | WMS dock-scheduler multi-warehouse allocation throughput | 7.6 | 5 | 1.000 | 100 |
| 9 | T1 | inventory SKU warehouse accuracy WMS module throughput | 12.0 | 5 | 1.000 | 100 |
| 10 | T1 | warehouse picking batch genetic algorithm orders throughput | 8.8 | 5 | 1.000 | 100 |
| 11 | T2 | query optimization rewritten execution time index database | 11.7 | 5 | 1.000 | 100 |
| 12 | T2 | PostgreSQL performance tuning query latency index type | 12.0 | 5 | 1.000 | 100 |
| 13 | T2 | database migration data transfer execution time query | 11.3 | 5 | 1.000 | 100 |
| 14 | T2 | query optimization case study execution time method index | 10.4 | 5 | 1.000 | 100 |
| 15 | T2 | full-text search query execution time reduction database | 11.2 | 5 | 1.000 | 100 |
| 16 | T3 | handoff cross-agent task routing capability match score | 8.8 | 5 | 1.000 | 100 |
| 17 | T3 | agent handoff from browser to app context preserved | 52.0 | 5 | 1.000 | 100 |
| 18 | T3 | cross agent routing capability match browser computer agent | 8.2 | 5 | 1.000 | 100 |
| 19 | T3 | handoff agent task transfer context capability score | 9.0 | 5 | 1.000 | 100 |
| 20 | T3 | agent collaboration completed steps pending handoff routing | 11.2 | 5 | 1.000 | 100 |
| 21 | T4 | forgetting curve retention analysis days review interval | 8.2 | 5 | 1.000 | 100 |
| 22 | T4 | memory consolidation cycle processed merged archived duratio... | 8.9 | 5 | 1.000 | 100 |
| 23 | T4 | session boundary summarizing turns consolidated memories the... | 6.7 | 5 | 1.000 | 100 |
| 24 | T4 | memory merge deduplication consolidation forgetting retentio... | 7.1 | 5 | 1.000 | 100 |
| 25 | T4 | consolidation cycle merged archived forgetting curve analysi... | 8.0 | 5 | 1.000 | 100 |
| 26 | T5 | LLM inference optimization throughput tokens per second cost | 9.5 | 5 | 1.000 | 100 |
| 27 | T5 | model serving benchmark tokens per second latency batch | 15.8 | 5 | 1.000 | 100 |
| 28 | T5 | embedding model comparison winner scores task LLM | 12.5 | 5 | 1.000 | 100 |
| 29 | T5 | inference optimization TensorRT SGLang throughput tokens | 8.4 | 5 | 1.000 | 100 |
| 30 | T5 | LLM serving cost per million tokens inference optimization | 8.7 | 5 | 1.000 | 100 |
| 31 | T6 | configuration preference autosave dark mode user confirmed | 7.6 | 5 | 1.000 | 100 |
| 32 | T6 | user preference configuration setting confirmed workflow | 7.6 | 5 | 1.000 | 100 |
| 33 | T6 | prefers dark mode working configuration confirmed preference | 5.3 | 5 | 1.000 | 100 |
| 34 | T6 | configuration setting preference user confirmed performance | 9.1 | 5 | 1.000 | 100 |
| 35 | T6 | workflow preference user confirmed configuration dark mode | 7.4 | 5 | 1.000 | 100 |
| 36 | T7 | self-improvement log strategy before after metric improvemen... | 7.4 | 5 | 1.000 | 100 |
| 37 | T7 | evolution state phase active strategies pattern library | 7.0 | 5 | 1.000 | 100 |
| 38 | T7 | evolution pattern library size phase active strategies | 9.7 | 5 | 1.000 | 100 |
| 39 | T7 | self improvement evolution certified delta strategy boosting | 8.7 | 5 | 1.000 | 100 |
| 40 | T7 | evolution observed analyzed planned executed certified phase | 7.4 | 5 | 1.000 | 100 |
| 41 | T8 | skill registry entry activation cost tokens dependencies ver... | 8.6 | 5 | 1.000 | 100 |
| 42 | T8 | skill definition triggers tools success rate description | 8.6 | 5 | 1.000 | 100 |
| 43 | T8 | skill evaluation test cases accuracy latency status registry | 14.7 | 5 | 1.000 | 100 |
| 44 | T8 | activation cost estimate tokens skill registry dependencies | 8.2 | 5 | 1.000 | 100 |
| 45 | T8 | skill registry version dependencies activation cost estimate | 7.8 | 5 | 1.000 | 100 |
| 46 | T9 | guardian chain check evaluated verdict confidence audit log | 9.6 | 5 | 1.000 | 100 |
| 47 | T9 | security audit incident report type severity detected resolv... | 7.2 | 5 | 1.000 | 100 |
| 48 | T9 | audit log risk action guardian chain check safety | 7.7 | 5 | 1.000 | 100 |
| 49 | T9 | guardian evaluated confidence risk auditor security incident | 7.6 | 5 | 1.000 | 100 |
| 50 | T9 | safety audit log guardian chain level rule risk check | 8.0 | 5 | 1.000 | 100 |

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
- Results CSV: `C:\Users\Administrator\trinity\benchmark\beam_results.csv`
*（内容由AI生成，仅供参考）*
