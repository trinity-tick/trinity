# Changelog

## v6.36.0 (2026-07-14)

**🎉 First structured release — package reorganization + benchmark readiness**

### New
- Standardized package structure (`trinity/`) with `pip install` support
- Unified CLI (`trinity search/ingest/diagnostics/mcp/bench`)
- `Trinity` class — 5-line quickstart API
- Benchmark runner (`trinity bench --name longmemeval`)
- MCP Server with stdio/SSE support
- LangChain adapter for agent integration

### Modules (122 total)
- CB54 ExabaseRetrieval — Tri-Signal scoring (S_sem + S_lex + T_temporal)
- CB55 HindsightFourNetwork — Four-network separation (P127 aligned)
- CB56 ZikkaronHopfield — Hopfield energy + spreading activation (P128 aligned)
- CB57 SelfOptimizingMemory — Agent-controlled memory strategy (P129 aligned)

### Guardian Chain (50-tier)
- L46 BEAMLIGHTGuard (P125)
- L47 ExabaseRetrievalGuard (P126)
- L48 HindsightFourNetworkValidation (P127)
- L49 ZikkaronHopfieldEnergyGate (P128)
- L50 SelfOptimizingMemoryGuard (P129)

### Infrastructure
- `pyproject.toml` with setuptools build system
- `.gitignore` for Python/IDE patterns
- MIT License
- Example scripts (quickstart, MCP server, LangChain agent)
- deprecation: old version files (v6.14–v6.34) consolidated into clean structure

## v6.34 (2026-07-13)

- CB55 HindsightFourNetwork (P127)
- CB56 ZikkaronHopfield (P128)
- +L48, +L49 guards
- +ch45, +ch46 retrieval channels

## v6.32 (2026-07-13)

- CB54 ExabaseRetrieval (P126)
- +L46 BEAMLIGHTGuard, +L47 ExabaseRetrievalGuard
- +ch43, +ch44 retrieval channels
- P0: BEAM-LIGHT ICLR2026 alignment

## v6.28 (2026-07-12)

- CB49 RelationalVersioning (P121 — Supermemory aligned)
- CB50 ContextualChunkIngestion (P122)
- +L42, +L43 guards
- LongMemEval self-test: ~97.8%

## v6.26 (2026-07-12)

- CB47 TokenEfficientMemory (P119 — Mem0)
- CB48 AgentNativeCuration (P120 — ByteRover)
- +L40, +L41 guards

## v6.24 (2026-07-12)

- CB45 ProgressiveCascade (P117 — ByteRover)
- CB46 TemporalValidity (P118 — Zep/Graphiti)
- CB42-CB44 ChromaDB edge layer merge
- +L38, +L39 guards
