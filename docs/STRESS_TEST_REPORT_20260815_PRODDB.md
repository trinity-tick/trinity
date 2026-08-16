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

- 压测写路径走内存池；生产库写入/审计链压力在副本上完成（见第 7 节）。
- 冷启动成本（~2.7s）已大幅收敛：BM25 后台预构建 + 启动预热管理。

## 7. 二轮优化（commit `e520254`）——评价建议四项全部执行

### 7.1 生产库副本写入压测（--db-write）✅
对生产库副本并发 `store_memory` + 审计链 + 锁稳定性（历史 database is locked
验证）。关键发现：`ingest(postprocess=True)` 同步加工管线（语义关联+实体提取+
主动推送）占写入成本 **~97%**（单条 430-665ms vs `postprocess=False` 13ms，
首次 31.8s 为 embedding 冷启动）。压测改用生产 API 一致的异步化路径
（postprocess=False，加工后台完成）+ 少量同步对照采样。

结果（8 线程 × 500 写）：
- QPS **6,534**（同步加工路径为 17.9 → 365x）
- p50 105ms / p99 166ms / **0 错误 / 0 锁冲突**
- 一致性精确：副本 memories 12,164→12,664（+500），audit +501（含对照采样）

### 7.2 启动期 BM25 后台预构建 ✅
`_ensure_bm25_index` 改为立即返回空索引（HybridRetriever 对空索引 search 返回
空 = 优雅降级）+ 后台 daemon 线程 `add_documents` 构建（`_bm25_ready` 标记）。
消除首次检索 1-2s 惰性构建；压测预热等 `_bm25_ready` 后再计时。

### 7.3 WAL + 每线程只读连接池 ✅
`_get_read_conn()`：每线程独立只读 SQLite 连接（`mode=ro` URI），WAL 多读并行、
零锁竞争；写路径保持主连接 + `_write_lock`。基准：8 线程 p50 **115ms→25ms**。
读方法全部切换：search_memories / _search_fts / _search_like / _fts_available /
get_memory / get_memory_owners / get_all_memories / search_entities /
query_relations / query_graph / traverse。

### 7.4 异步 touch（隐藏写放大）✅
`_touch_batch` 改为内存队列（dict + pending event），后台线程批量
`UPDATE…IN` + 单次 commit。检索路径零写阻塞。实测：touch 关闭 p50 120→64ms
（占读延迟 ~40%）；异步化后 QPS +21%（57→75 单连接对照）。access_count
累积精确验证（3 次命中 → +3）。

### 7.5 二轮稳定态压测结果（8 线程 × 500/500/200 + 生产库 + API）

| 阶段 | QPS | p50 | p99 | errors |
|---|---|---|---|---|
| 写（内存池） | 43,059 | 8.45ms | 12.46ms | 0 |
| 检索（内存池） | 44,525 | 8.56ms | 12.85ms | 0 |
| 混合读写 | 17,749 | 8.14ms | — | 0 |
| 生产库副本检索（12k 真实 I/O） | **2,539** | 47.8ms | 195ms | 0 |
| 生产库副本写入（异步化路径） | **6,534** | 105ms | 166ms | 0 |
| API 并发检索（:8001 全链路） | **10,789** | 40ms | 56ms | 0 |
| 一致性 | 12,664 memories / audit 10,176 / 锁错误 0 | | | |

对比一轮（commit ba56408）：生产库检索 QPS 1,026→2,539（2.5x）、API 3,806→
10,789（2.8x）、新增写入路径 6,534。全部 p99 < 200ms。

### 7.6 测试与提交
- 全量：**742 passed / 54 skipped / 0 failed**
- 提交：`e520254 perf(stress): connection pool + async touch + BM25 prewarm + db-write stress`（3 files, +446/-247）

## 8. 指标口径说明（2026-08-15 二轮评价后补充）

> 压测报告中的"提升倍数"需区分两类对比，避免误读：

### 8.1 严格 A/B（同进程、仅一个变量）
| 对比 | 结论 | 可信度 |
|---|---|---|
| touch 同步 vs 异步（同副本、同脚本对照） | p50 120→64ms（touch 占读延迟 ~40%），QPS +21% | **高**：单变量对照 |
| 单连接+RLock vs 8 实例 WAL（独立基准脚本） | p50 115→25ms | **高**：单变量对照 |
| ingest postprocess=True vs False（同副本） | 430-665ms vs 13ms（加工管线占写入 ~97%） | **高**：单变量对照 |
| access_count 累积 3 次命中 → +3 | 精确 | 高：确定性断言 |

### 8.2 跨轮对比（含就绪态变量，非严格 A/B）
| 对比 | 值 | 说明 |
|---|---|---|
| 生产库检索 QPS 1,026→2,539 | 2.5x | 跨两轮提交（ba56408→e520254），变量 = 连接池+异步 touch+BM25 预热叠加 |
| API QPS 3,806→10,789 | 2.8x | **注意**：R6→R7 之间 API 代码未变，QPS 2,687→10,789 的主因是 **BM25 就绪态**（R6 时 API 进程刚重启、压测时 BM25 后台构建抢 GIL；R7 预热等 `_bm25_ready`）。"10,789"有相当部分是稳定态红利，非全部来自连接池 |
| 写 QPS 17.9→6,534 | 365x | **口径限定**：6,534 是 `postprocess=False`（去加工）的 SQLite 写路径吞吐；生产真实写入（带语义关联/实体提取/主动推送）受 embedding/LLM 限制，同步加工路径实测 p50 5.4s、p99 30s。**"6,534"不代表系统全管线写入能力，它量化的是 SQLite 写路径本身** |

### 8.3 建议引用口径
- 说"连接池使读并发提升"：引用 8.1 的 25ms（A/B）或"生产库读 QPS 2,539（含连接池+异步 touch 综合）"。
- 说"写入快"：必须带限定词"异步化路径（postprocess=False）"，且注明全管线成本在加工侧（97%）。
- 说"API 吞吐"：注明是"BM25 就绪后的稳定态"，重启后首个请求窗口仍受构建期 GIL 影响。

### 8.4 遗留债（二轮评价）
- 连接池/异步 touch 行为契约已补专项回归（`tests/test_sqlite_connpool_touch.py`，9 用例），
  连接生命周期管理（注册表 + 上限 64 + overflow 计数 + disconnect 全关）已验证。
- thread-local 连接在超限时走临时连接（GC 兜底），注册连接由 disconnect 全量关闭；
  长驻服务中若线程池频繁重建，注册连接数可能接近上限（有 overflow 计数可观测）。

## 9. 三轮优化（commit `[round-3]`）——评价建议四项全部执行

### 9.1 多进程写并发重压 ✅（scripts/multi_process_stress.py）
API + collector 双进程同时打开同一 WAL 库副本，各自 4 线程并发写：
| 进程 | 写入 | QPS | p50 | p99 | 错误 |
|---|---|---|---|---|---|
| api | 300 | 26.6 | 117ms | 1,642ms | 0 |
| collector | 300 | 26.3 | 120ms | 1,713ms | 0 |
| 一致性 | memories +600 精确 | audit 10,389 | **lock_errors=0** | | |

**新连接池架构多进程写稳定性验证通过**——历史 `database is locked` 隐患
在 API+collector 并存场景下 0 锁冲突。

### 9.2 写入加工管线异步化 ✅
`ingest(postprocess=True)` 默认后台线程执行（写入即时返回），
`_postprocess_lock` 全局串行化（并发加工线程抢 GIL/写锁会拖垮写入：
响应 p95 3.7s → **552ms**，QPS 14 → **51**）。
顺带修复两个潜伏 bug：
- `_auto_link_semantic` 用 `backend=sklearn`（auto 探测 Ollama → embed_batch
  100 条 × 300ms = 30s 后台"卡住"）
- `proactive_push` 的 `float(link.get("strength"))` 遇 None 崩溃
- `TRINITY_LLM_EXTRACT=on` 保持同步（显式功能语义：调用方期望返回时实体已入库）

### 9.3 崩溃恢复测试 ✅（tests/test_crash_recovery.py，4 用例）
子进程 `os._exit(1)` 故障注入：
- WAL 恢复已 commit 记忆（5 基准 + 20 崩溃前 = 25 精确）
- 审计 checksum 链崩溃后完整
- 异步 touch 丢数有界（access_count 无半写损坏值）
- 库不损坏且崩溃后可继续写入

### 9.4 API 启动期预热 ✅
lifespan 触发 `_ensure_bm25_index`（**仅触发后台构建，不跑完整 search_hybrid**
——预热跑全链路会与首请求竞争写锁/GIL，实测首请求 16s/0 结果 → 纯构建预热
**122ms/5 结果**）。`_ensure_bm25_index` 加 `_bm25_lock` 原子化（防预热+请求
并发时双份 12k 构建）。

### 9.5 测试与提交
- 全量：**755 passed / 54 skipped / 0 failed**
- 新增：`scripts/multi_process_stress.py`、`tests/test_crash_recovery.py`（4 用例）；
  修复 overflow 测试确定性（3 线程 barrier + 顺序第 4 线程）

## 10. 四轮验证（2026-08-16）——剩余建议 + 综合回归

### 10.1 三进程写并发（--procs 3）✅
api + collector + worker 三进程同时写同一 WAL 副本（各 300 条 / 4 线程）：
| 进程 | QPS | p50 | p99 | 错误 |
|---|---|---|---|---|
| api | 18.8 | 139ms | 1,661ms | 0 |
| collector | 18.5 | 138ms | 2,069ms | 0 |
| worker | 20.9 | 125ms | 1,655ms | 0 |
一致性：memories +900 精确（12,168→13,068）、audit +900、**lock_errors=0**。
3 进程拓扑下新连接池架构同样无 database is locked。

### 10.2 WAL 增长 + 16 线程高并发 ✅（scripts/wal_growth_stress.py）
16 线程 × 3,000 写入：
- QPS 56.4 / p50 270ms / p99 540ms / **0 错误 0 锁冲突**
- memories +2,992 精确（12,168→15,160）
- **WAL 自动 checkpoint 生效**：写入后 WAL 10.2MB→4.16MB（写入中已合并），
  checkpoint 后 0.0MB；主库 74.6MB→76.63MB（+2MB 受控，无膨胀）

### 10.3 综合回归（三轮优化叠加后全链路）
| 阶段 | QPS | p50 | p99 | errors |
|---|---|---|---|---|
| 内存池写/读/混合 | 43.3k / 43.8k / 16.7k | ~8ms | <12ms | 0 |
| 生产库副本读 | 2,771 | 47ms | 183ms | 0 |
| 生产库副本写（异步路径） | 2,880 | 106ms | 236ms | 0 |
| API 并发 | 496 请求 | 159ms | 13.8s* | 0 |
*：API 尾延迟为本轮压测期间环境噪声（MCP+collector+压测副本抢 CPU/IO）；
隔离复测连续 5 次 32-77ms（avg 41ms）——稳定态正常。

### 10.4 测试与提交
- 全量：**755 passed / 54 skipped / 0 failed**
- 新增 `scripts/wal_growth_stress.py`；`multi_process_stress.py` 支持 `--procs 3`

## 11. 剩余边界深挖（2026-08-16）

### 11.1 交叉拓扑：3 进程 × 16 线程 ✅
最恶劣写拓扑（api+collector+worker × 16 线程 × 同一 WAL 库，各 500 写）：
- **0 锁错误 / 0 database is locked**，+1,488 memories 精确（12,169→13,657）
- p50 ~720ms（写锁竞争+WAL 单写者导致高于低并发），正确性优先

### 11.2 显式 checkpoint 路径 ✅
2,000 写后 `wal_checkpoint(TRUNCATE)`：WAL 4.09MB→0MB，2000/2000 ingest 返回
memory_id（无合并丢数）。此前"计数差 1"判定为**测试脚本断言 bug**
（硬编码 12168+2000，实际副本 at-copy 时已是 12,167）——真实计数精确。

### 11.3 checkpoint 与并发读互斥 ✅
PASSIVE checkpoint 5 轮 + 并发检索线程：busy=0、reader 0 错误——checkpoint
不阻塞读、读不阻塞 checkpoint。

### 11.4 固化回归
`tests/test_wal_checkpoint.py` 3 用例：TRUNCATE 回收+精确计数、checkpoint/

## 12. 未覆盖边界深挖（2026-08-16，commit 682ea8d）

### 12.1 PostgreSQL 镜像链 ✅（scripts/pg_adapter_stress.py）
原生 PG16 :5432（维护链 :5430 docker 当前不可用，验证同一适配器代码路径）：
- 读 QPS 916 / p50 3.4ms，显式事务回滚写 QPS 829 / p50 0.79ms，0 错误
- **诚实修正**：此前"写回滚零污染"结论错误（store_memory 默认 commit，
  实际泄漏 402 行已清理）——改为显式事务 ROLLBACK 后计数 2553→2553 真零污染

### 12.2 加密存储路径 ✅（TRINITY_STORAGE_ENCRYPTION）
- 读：加密 OFF p50 74.0ms vs ON 71.7ms（**<5% 开销**，解密 ~µs 级被检索成本掩盖）
- 写：QPS 56.6 vs 51.1（**~10% 开销**）
- 密文落库验证 + 检索解密正常
- **关键发现**：`shutil.copy2` 快照跳过 WAL → FTS rowid 错位 → 写触发器撞
  rowid → IntegrityError。所有快照函数改 **VACUUM INTO**（含 WAL、rowid 一致、
  免疫 WinError 33）。此缺陷同时解释了此前加密写"失败"的误判。

### 12.3 长时间运行 soak ✅（scripts/soak_test.py）
10 分钟混合负载（27,863 操作：写 9.3k/读 9.3k/touch 8.6k）：
- **0 错误 / 0 锁冲突**；RSS 306.1→309.6MB（**+1.1% 无泄漏**）
- WAL 峰值 4.2MB（受控）、读连接池稳定 10（无泄漏）
- 首跑 756 错误为脚本 bug（_pool 是聚合器属性非 Trinity）——修复后 0 错误

### 12.4 LLM 抽取异步开关 ✅（TRINITY_LLM_EXTRACT_ASYNC）
- 同步 LLM 抽取 ingest 3,656ms vs 异步开关返回 **20ms（183x）**，后台完成
- 默认保持同步语义（兼容既有测试/管线）；异步开关供吞吐优先场景

### 12.5 测试与提交
- 全量 **762 passed / 54 skipped / 0 failed**
- 新增 `scripts/pg_adapter_stress.py`、`scripts/soak_test.py`、
  `tests/test_pg_llm_extract.py`（4 用例）；test_wal_checkpoint fixture 改
  VACUUM INTO
读不阻塞、ingest id 完整性。全量 **758 passed / 54 skipped / 0 failed**。
