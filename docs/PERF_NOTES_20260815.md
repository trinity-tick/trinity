# Trinity 性能优化记录（2026-08-15，round37）

> 依据：真实路径 profiling（非 stub）+ 网络方案对照
> （[Mem0 向量延迟 70x](https://mem0.ai/blog/how-we-cut-vector-search-latency-by-70x)、
> [pgvector 大规模向量检索](https://clickhouse.com:2087/resources/engineering/scale-vector-search-postgres)、
> [Query-Aware Budget-Tier Routing](https://huggingface.co/papers/2602.06025)）。

## 一、实测热点（真实数据，非 profiler stub）

| 热点 | 实测 | 根因 | 修复 |
|---|---|---|---|
| 首次 FTS 搜索 | **1,434ms** | jieba 词典冷启动（热查询仅 1.26ms） | `SQLiteAdapter.connect` 后台线程预热 → **首查 ~3ms** |
| use_ann 向量搜索 | **~30s/查询** | 每查询全量编码 11.7k + 重建 ANN | 持久缓存 + 后台预热 → **首次 483ms（降级 FTS）、热查 9ms** |
| 重复查询嵌入 | ~380ms/次 | 同 query 反复 embed | 进程内查询向量缓存（hash key）→ 同 query 9ms |
| 向量召回盲区 | 覆盖 42% | `get_all_memories(limit=5000)` | → limit=20000（100% 覆盖） |
| 向量搜索静默空 | 返回 [] | `hashlib` 未导入被 except 吞 | 补 import，恢复真实行为 |

## 二、结构问题（本轮确认）

1. **向量索引无持久化**：`_vector_search_ann` 每查询实时构建（use_ann 因此默认关）。
   本轮加进程内持久缓存（版本键+TTL）；**落盘持久化（写入时增量维护）为下一轮方向**，
   对齐 pgvector HNSW/磁盘索引方案。
2. **profiler 是 stub**：`trinity_profiler.py` 的 CB36/CB38 300ms 为模拟值——本轮用真实
   profiling 补齐了真实热点表（见上），后续基准应以真实路径为准。
3. **多进程内存重复**：api/mcp/worker 各自持有 embedding/ANN 缓存（进程隔离）——
   可接受；如需共享可外置（Redis/独立索引服务），成本高收益小，暂不做。

## 三、性能基线（优化后，2026-08-15 实测）

| 场景 | 延迟 |
|---|---|
| FTS 搜索（热） | ~3ms |
| hybrid E2E（Redis 缓存命中） | ~5ms |
| hybrid E2E（miss，热） | ~40ms |
| use_ann 首查（降级 FTS + 后台构建） | 483ms（此后热查 9ms） |
| ANN 热查（缓存命中） | 9ms |

## 四、下一轮候选（按收益）

1. **向量索引落盘持久化**：启动加载、写入增量更新（对齐 pgvector HNSW；11.7k 规模
   收益中等，100K+ 规模收益大）。
2. **embed 批量化/缓存外置**：查询嵌入可入 Redis（跨进程复用）。
3. **自适应预算路由**（P2）：query-aware 分层路由（高频简单查询走 FTS、复杂走
   全通道）——对应论文方案与 EXECUTION_PLAN A2。
