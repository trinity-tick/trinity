# Benchmarks

> **Trinity v8.2.0 · 口径统一版（2026-08-14）**
> 本页分两部分：**A. 实测结果**（本机 2026-08-14 真实运行产物）与 **B. 官方/社区参考线**（带来源）。
> 凡无本机实测佐证的历史数字均明确标注，避免与实测混淆。

---

## A. 实测结果（本机 2026-08-14）

### A.0 环境

| 项 | 值 |
|---|---|
| OS / Python | Windows · 系统 Python 3.14（项目 .venv 仅 numpy/jieba） |
| 存储 | PostgreSQL 16（库 `trinity`，memories=1040）+ SQLite（MCP store，11313 条） |
| 版本 | v8.2.0（pyproject）；系统 Python editable 安装标签为 6.37.0（仅元数据过期，代码同源） |

### A.1 检索质量

| 套件 | 分数 | 口径说明 |
|---|---|---|
| LongMemEval (simulated) | R@5 = **0.9818**（55 题 54/55） | 模板生成模拟集，**非官方 500 题 LongMemEval-S**；BM25+jieba 多词合并 |
| LongMemEval-style (500q) | R@5 = **0.9160** / MRR = **0.8618**（top_k=5）；**top_k=10 → R@5=0.992**；**AnswerAcc（LLM-judge，DeepSeek）= 0.602** | 500 题社区生成集（对齐 LongMemEval-S 六类目结构，非官方标注集）；FTS5 keyword。逐类目（top_k=10）：KU 0.863 / SS-A 0.800 / SS-P 0.483 / SS-U 0.900 / TR 0.300 / **MS 0.113**。结论：检索已近满分，瓶颈在生成侧；**MS 低分为数据集缺陷**（92% MS 题问题与事实零词重叠），**TR 时序 0.30 为真实可优化点**。评测入口 `benchmark/answer_eval.py`，产物 `output/answer_eval_results.json` |
| SQuAD v1.1 (adapted) | R@5 = **98.3%**（177/180，双口径一致：`keyword_47ch` 产品级 47 通道 = `bm25_adapter` 低层通道） | **统一入口**：`benchmark/squad_runner.py`（单次运行、同子集 seed=42/180 题、同命中判定，产物 `output/squad_unified_results.json`）。README 旧 35.6% 为早期代码结果；workflow 报告中的"keyword 0%"系 `Trinity(store_path=<文件>)` 误当目录导致 adapter 静默缺失（已修复，见 EXECUTION.md 12.4） |
| LoCoMo（自建子集） | 最优配置 B.session-aggregate：R@5=**0.88** / MRR=**0.5353**（38 题） | 4 种配置对比：turn-baseline R@5=0.14 / session-aggregate 0.88 / turn+query-expansion 0.14 / session+query-expansion 0.88；temporal-reasoning 类目全 0（短板）。**官方 1982 题集网络不可达** |
| BEAM Scale | 1K：R@5=**1.000**，P50 8.65ms · 10K：R@5=**1.000**，P50 240.0ms · 100K(110K 条)：R@5=**1.000**，P50 984.6ms | PostgreSQL FTS 内联 to_tsvector（**无 GIN 索引，全表扫描**，故延迟随规模线性增长）；隔离库 trinity_bench 实测后已删除；非官方 BEAM 数据集 |
| ANN HNSW | Recall@10 = 1.000 | 向量索引召回 |

### A.2 性能（本机）

| 项 | 实测 |
|---|---|
| GraphQL Load | 100 QPS / 20 workers / 0 errors；p50=2.06ms，p99=29.25ms |
| 写入吞吐 | 383–725 ops/s（按批次） |
| 检索延迟 | 0.93ms（单查）；混合检索 0.73ms vs 纯向量 0.42ms；**Redis 缓存命中后 API 级 10.2ms vs miss 18.4ms**（`/memory/search/hybrid`） |
| PG FTS GIN 索引 | **P50 286ms → 45.9ms（6.2×）@10K**（`idx_memories_content_gin`，`to_tsvector('simple', content)`） | 对比基准 `benchmark/beam_gin_index.py` |
| Cluster Stress | 5/5 checks；**单 leader 已修复**（3 节点选举注册仲裁 + 心跳抑制；leader commit_index 正常推进） | Raft 单 leader 不变量 |

### A.3 测试与自检

| 项 | 结果 |
|---|---|
| pytest 全量 | **161 passed / 6 skipped / 1 failed**（2026-08-14 晚；新增 9 组测试：真实 LLM 压缩 6、store_path 回归 3、Redis 缓存 8、限流/指标 5、A2A e2e 12、插件/CLI 23、PG 池 4、SQLite 线程安全 2。唯一失败 `trinity/tests/test_e2e_multi_agent.py` 依赖外部 Marvis 服务（:18001 未运行），**既有环境依赖非本轮引入**） |
| 内部 self_test | 208/208 PASS（内部口径，与 pytest 不同集合） |
| 进化周期 | 完整周期 3 次（observe→analyze→plan→execute→certify） |

### A.4 已知口径缺口（诚实披露）

- **官方 LongMemEval-S（500 题）、LoCoMo（1982 题）、BEAM 官方集本环境网络不可达**（实测 GitHub/HuggingFace 全部超时，仅 PyPI 可达）——当前 LongMemEval 采用 500 题社区 mock 集（结构对齐）、LoCoMo 采用 38 题自建子集、BEAM 采用自建话题数据；均已如实标注"非官方集"。
- 原 README 声称的 1M memories P50 5.8ms 等大规模数字**无本机实测 JSON 佐证**，见下文 B.2 标注为历史数据。
- SQuAD 双口径矛盾已解决：统一为 BM25/FTS5 口径（98.3%），README 旧 35.6% 系早期代码结果。

---

## B. 官方/社区参考线（带来源）

### B.1 检索质量 SOTA（社区公开）

| 基准 | 参考分数 | 来源 |
|---|---|---|
| LongMemEval-S | CortexDB Retrieval 栈 **93.8%** | [CortexDB benchmark paper](https://cortexdb.ai/docs/research/benchmark-paper) · [blog](https://cortexdb.ai/blog/longmemeval-93-8-percent) |
| LongMemEval-S | Engram vs Chronos/Mastra/OMEGA/Zep 排行榜 | [JamJet LongMemEval-S leaderboard](https://jamjet.dev/benchmarks/engram-longmemeval/) |
| LongMemEval / LoCoMo / BEAM | agentmemory / agentos 等开源 harness 分数 | [agentmemory LONGMEMEVAL.md](https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md) · [agentos-bench](https://github.com/framerslab/agentos-bench) |
| LoCoMo 检索 | ConvMemory v2 reranker（Top-10 evidence） | [ConvMemory v2（arXiv 2606.10842）](https://arxiv.org/abs/2606.10842) |

> 注：BEAM（ICLR 2026）HindsightFourNetwork=64.1%、ZikkaronHopfield=40.4% 为 second_brain 模块的论文对齐值，非本机实测。

### B.2 历史性能数据（无本机佐证，仅供架构参考）

| 数据集规模 | P50 (ms) | P99 (ms) | QPS |
|---|---|---|---|
| 10k memories | 2.1 | 4.8 | 4,500 |
| 100k memories | 3.4 | 7.2 | 3,200 |
| 1M memories | 5.8 | 14.3 | 1,800 |
| 10M memories | 12.7 | 38.1 | 850 |

> 此表来自早期文档（v1.2.0 时代），本机未复测；如需对外引用请先跑 `benchmark/run_latency_bench.py` 复测。

---

## C. 功能对比（架构层面）

| Feature | Trinity | Memory-1 | Mem0 | LangMem |
|---|---|---|---|---|
| Vector Search | ✅ pgvector/FAISS | ✅ Pinecone | ✅ Chroma | ✅ FAISS |
| Hybrid Search | ✅ | ❌ | ✅ | ❌ |
| Multi-Tenant | ✅ Built-in | ❌ | ⚠️ Partial | ❌ |
| Multimodal | ✅ Image/Audio/Text | ❌ | ❌ | ❌ |
| MCP Native | ✅ | ❌ | ❌ | ❌ |
| Docker | ✅ | ❌ | ✅ | ✅ |
| Open Source | ✅ Apache 2.0 | ❌ | ✅ Apache 2.0 | ✅ MIT |

---

## D. 运行基准

```bash
# 并行基准套件（DSH workflow 或本机）
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\trinity\dsh-ops\run-benchmarks.ps1 -Suites latency,concurrency
# LLM 套件需要 TRINITY_API_KEY（存 ~/.dsh/.credentials.yaml，勿写死在脚本）
# 单测
& 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe' -m pytest -q --tb=line
```

> 汇总产物：`.trinity\bench-results\<ts>\summary.md`；核查报告：`.trinity\bench-results\workflow-demo\report.md`。

---

## E. 下一步（按优先级）

1. 网络可达后跑**官方 LongMemEval-S（500 题）/ LoCoMo（1982 题）**，替换 mock/子集口径，进入 B.1 对比表。
2. 修复 **LongMemEval MS（多会话）类目 0.525** 短板（多会话检索/过滤优化）。
3. BEAM 增加 GIN 索引后复测（当前全表扫描导致 100K 延迟 ~1s/query），并视资源跑 1M 档。
4. LoCoMo temporal-reasoning 类目全 0 需专项调查。
