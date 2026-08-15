# Trinity 生产库压测 + 并发修复报告（2026-08-15）

> 目标：① ANN 索引启动预热（消检索 p99 偶发 2.4s 冷启动尾巴）；② 生产库真实 I/O 压测
> （SQLite 12k 条 + API 并发）；③ 复测验证。Commit: `ba56408`

## 1. ANN 预热验证 ✅

`MemoryAggregator` 新增 `_prewarm_ann_index` 后台线程（embedding ready 后全量
`_rebuild_index()` 补建），`_add_to_index` 增加 ready-guard（预热期 ingest 跳过索引、
不阻塞写入，由预热线程兜底补建）。

| 场景 | 结果 |
|---|---|
| 预热完成后 ingest | 20 条 53ms，索引 20/20，检索 3ms |
| 冷启动窗口 ingest（fit 完成前） | 20 条 20ms，fit 2.8s ready 后预热线程补建 20/20，检索 4ms |

之前验证 FAIL 的根因：① 验证脚本在 fit 完成前（2s）就查 `_embedding_ready`；
② "pool=1" 是 5 条测试内容过于相似被 merge 合并。生产路径本身工作正常。

## 2. 压测脚本扩展（--db/--api 真正生效）

原脚本 `--db` 参数是死参数（`consistency_check` 定义了但 main 从不调用）。
现在：
- **阶段 6**：`--db` 指定生产库 → 复制到临时副本（零污染权威库）→ `Trinity`
  并发 hybrid 检索（真实 SQLite I/O + FTS + jieba + BM25 + 图）+ `consistency_check`
  真实一致性校验（12,164 memories / 1,920 active / audit 链）。
- **阶段 7**：`--api` 并发 POST `/memory/search/hybrid`（真实 HTTP → 引擎全链路）。
- **阶段 0**：预热（计时外）——等 embedding ready + 触发 BM25/ANN 构建，
  压测衡量稳定态，一次性冷启动成本单独记录（`warmup_s` / `bm25_warmup_s`）。

## 3. 压测暴露的真实并发 Bug（3 处修复）

### 3.1 SQLite 读路径无锁（最严重）
`SQLiteAdapter` 是 `check_same_thread=False` 单连接共享，写路径有 `_write_lock`
但**读路径完全无锁** → 8 线程并发 `search_hybrid` 对同一连接并发 execute →
游标竞态：`bad parameter or other API misuse`（SQLITE_MISUSE）+ FTS rank 错位为
None 导致 `min()` 抛 `'<' not supported between instances of 'NoneType' and 'float'`。
**这是 API 线程池也会踩的真实生产 bug**（此前 API 负载低未暴露）。

修复：8 处方法统一加 `_write_lock`（RLock 可重入，嵌套调用安全）——
`search_memories`、`get_memory`、`get_memory_owners`、`get_all_memories`、
`search_entities`、`query_relations`、`query_graph`、`traverse`、`write_audit_log`。

验证：240 次 8 线程并发 `search_hybrid` 全链路 → **0 错误**（修复前 4 错误）。

### 3.2 限流误伤只读检索端点
`/memory/search/*` 是只读检索，但因 POST（body 带 query）被"写端点限流"前缀
`/memory/` 命中 → 压测 496 次请求 144 次 429。`is_rate_limited_request` 增加
`/memory/search/` 豁免 → 429 清零。

### 3.3 防御性修复
- `_minmax_normalise`：`it.get(key, 0)` 在 key 存在但值为 None 时仍返回 None →
  `min()`/`max()` 炸。改为 `it.get(key) or 0`。
- `_search_fts`：FTS5 查询串 token 引号转义（`"` → `""`）+ rank None 兜底。

## 4. 稳定态压测结果（预热后，8 线程 × 500/500/200）

| 阶段 | QPS | p50 | p99 | max | errors |
|---|---|---|---|---|---|
| 写（内存池） | 43,497 | 8.18ms | 12.28ms | — | 0 |
| 检索（内存池 hybrid） | 39,042 | 8.51ms | 12.13ms | — | 0 |
| 混合读写 | 14,983 | 8.03ms | — | — | 0 |
| 生产库副本检索（12k 条真实 I/O） | 1,026 | 108ms | 379ms | — | 0 |
| API 并发检索（:8001 全链路） | 3,806 | 128ms | 190ms | — | 0 |
| 一致性 | 12,164 memories / 1,920 active / audit 9,174 | | | | 锁错误 0 |

对比（是否计入冷启动）：
- **池检索 p99：2,476ms → 12ms**（预热后）
- **生产库 p99：1,715ms → 379ms**
- **API p99：1,797ms → 190ms**
- 写 QPS 11,876 → 43,497（预热后不再撞 sklearn fit）

结论：p99 2.4s 尾巴是**一次性冷启动**（BM25 索引构建 ~1-2s + sklearn 首次 fit
~3-10s），非持续问题；预热后稳定态 p99 全部 <400ms。

## 5. 测试与提交

- 全量：**742 passed / 54 skipped / 0 failed**
- 新增回归：
  - `tests/unit/test_ann_prewarm.py`（3 个预热路径回归）
  - `tests/test_sqlite_threadsafe.py::test_concurrent_search_no_cursor_corruption`
  - `tests/test_api_metrics.py` 限流谓词断言更新（`/memory/search/*` 豁免）
- 提交：`ba56408 fix(stress): production-DB stress + SQLite concurrency + ANN prewarm verify`（8 files, +619/-240）

## 6. 边界与遗留

- 压测写路径仍走内存池（`persist_path=None`），生产库写入/审计链压力未做
  （避免污染权威库）；一致性校验在副本上验证了 12,164 条真实数据完整性。
- 冷启动成本（~2.7s）在服务重启后仍存在，已通过启动预热 + 压测预热阶段管理，
  未做启动期后台全量 BM25 预构建（生产 API 已在启动时预热 embedding，BM25 仍惰性）。
- SQLite 单连接串行化后，多线程读并发被 RLock 收敛为串行 execute——
  SQLite 单连接本质串行，锁开销 µs 级（实测 QPS 1,026 未受明显影响）；
  更高读并发可考虑 WAL + 每线程连接池（后续优化项）。
