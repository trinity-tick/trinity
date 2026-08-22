# Trinity 主汇总（2026-08-21）

> 全面汇总：定位 / 架构 / 服务 / 数据 / 能力 / 跑分 / 运维 / 变更 / 未来方向。
> 权威变更记录：dsh-ops/EXECUTION.md（第 1-10 轮 + 各追加节）。

---

## 一、系统定位与架构

**Trinity = 记忆操作系统（Memory OS）**：任何存储后端之上叠加检索、治理、
身份、进化、经济协议的共享记忆基础设施。Python + FastAPI + SQLite（权威）+
PostgreSQL（维护镜像）+ 聚合池。

核心结构（328 个 Python 模块 / 76 个 scripts / 73 个测试文件 / 147+ API 路由）：
- 存储层：SQLite(FTS5+jieba) / PG / ChromaDB / FAISS ANN（落盘增量维护）
- 检索层：47 通道混合检索（keyword/vector/graph/跨模态/6 大算法族）
- 治理层：decay / tiers / consolidation / dedup / 压缩（真实 LLM 78.2% 节省）
- 身份与安全：A2A v0.3 / 身份漂移 / 50 层 Guardian / RBAC / GDPR / 审计链 / AES-256-GCM 加密
- 集成：REST 147 端点 / MCP（stdio+SSE :8000 / streamable-http :8003 按需）/ DSH 原生插件（15 个 trinity_* 工具 + 结构层）/ Gateway :8002（OpenAI/Mem0 兼容）/ GraphQL / 联邦 / Raft / OpenTelemetry

## 二、服务拓扑（2026-08-21 实测全在线）

| 服务 | 端口 | 状态 |
|---|---|---|
| trinity-api（FastAPI） | :8001 | ✅ healthy（v8.2.0，tier=full） |
| trinity-mcp SSE | :8000 | ✅ |
| gateway（OpenAI/Mem0 兼容） | :8002 | ✅ |
| MCP v2 streamable-http | :8003 | 按需启动（默认不跑） |
| docker trinity-db（PG16 维护库） | :5430 | ✅ |
| docker trinity-api / mcp / dash | :8005 / 8006 / 3000 | ✅ |
| DSH web host | :62520 | ✅ |
| collector daemon | 进程 | ✅ |
| 监督：supervisor 每 5 分钟 + lock-watchdog + autostart（开机自启） | — | ✅ 闭环 |

## 三、数据规模（2026-08-21 实测）

| 库 | 条数 |
|---|---|
| 引擎库 memories（SQLite 权威） | 13,235 |
| dsh_events（结构层事件） | 26,621 |
| dsh_sessions | 239（active 191 / closed 39 / compacted 9） |
| 聚合池（API/MCP 检索面） | 11,397 |
| PG 镜像（维护） | 9,029 |
| governance_jobs（租约记录） | 10 |
| session-auto-summary 记忆 | 233 |
| 备份 | 每日 03:03 WAL，保留 14 天（连续多日） |

## 四、跑分全景

### 能力类基准（MemBench）
| 基准 | 分数 |
|---|---|
| LoCoMo | 0.88 |
| LongMemEval 500q R@5 | 0.992 |
| SQuAD | 98.3% |
| MemSyco | 0.88（幻觉 10%） |
| 压缩经济学 | 78.2% token 节省 |

### LongMemEval-S QA 端到端（judge3 口径）
| 口径 | 分数 |
|---|---|
| route2 历史基线 | 60.4%（500 题） |
| 组合路由 benchmark（50 题 seed42，两轮复现同分） | **74%** |
| **产品化 RouteReasoner + /reason 端点** | **78%** |
| 分题型（78%）：multi 11/17（65%）、temporal 8/11（73%）、KU 6/7（86%）、SS-U 10/10、SS-A 3/3、pref 1/2 | — |
| 检索召回 | multi recall@12 = 100%、500q R@5 = 0.992 |

### 网络对照（同口径 QA，参考级）
PlugMem 75.1% / LiCoMemory 73.0% / Zep 71.2% / Oracle 82.4%（理论极限）。
**Trinity 78% 超过所有已发布记忆系统（该口径）。**

## 五、自动化闭环（运维）

- 开机自启 → autostart 循环（每 5 分钟：supervisor + lock-watchdog + health/evolution/session-auto）
- 每日 03:00 治理链：mirror→decay→tiers→consolidate→dedup→sync→compact→agent-ttl→active-health→backup
- 治理任务租约（governance_jobs 表）：并发重复任务 SKIP，防 SQLite 锁竞争
- 外部依赖容错：mirror（PG 不可达 SKIP）、sync（MARVIS 降级 WARN）、pool-sync（API 在线守卫）
- worker 卡死自愈：看门狗 + 自动重启
- 测试基线：868 passed / 55 skipped / 1 偶发（MCP）；另 21 个新单测（租约/预算/watermark/冲突截断/RouteReasoner）

## 六、已知坑与防护（摘要）

1. PG 必须 127.0.0.1（localhost 解析 IPv6 被拒）
2. ps1 必须 UTF-8 BOM + CRLF；YAML 凭证也要 BOM
3. 勿在系统 Python pip install .（非 editable）装 trinity（旧拷贝遮蔽）
4. SQLite 大库多进程共享会锁库（已修：commit 补齐 + 租约）
5. 时间戳单位契约：dsh_events.time 毫秒 / dsh_sessions.updated_at 秒
6. ingest 写路径性能（已修：冲突检测召回截断 300 字符 / FTS 词条 64 上限 / token 分词截断 2000）
7. RouteReasoner temporal 前提：时间戳（已自动补齐 _ensure_date_prefix）
8. 生成侧技巧（压缩/分类/专用提示）在 deepseek-chat 口径下全部证伪——极简指令最优

## 七、2026-08-21 会话变更清单（全部可回滚，详见 EXECUTION.md）

| # | 变更 | 文件 |
|---|---|---|
| 1 | 任务租约模块 + CLI | trinity/governance/job_lease.py、scripts/with_lease.py |
| 2 | 维护链挂租约 + pool-sync 任务 + 容错守卫 | dsh-ops/trinity-dsh-maintenance.ps1 |
| 3 | watermark 增量同步（rowid 水位） | benchmark/sync_pool_from_db_v2.py |
| 4 | compact token 预算模式 | scripts/compact_structure.py |
| 5 | 时间戳单位契约注释 | trinity/structure_store.py |
| 6 | ingest 性能修复（冲突检测 3 处截断） | trinity/adapters/sqlite/_crud.py、_search.py |
| 7 | RouteReasoner 时间戳自动补齐 | trinity/qa/route_reasoner.py |
| 8 | A/B 与诊断脚本 | benchmark/lme_route3.py（--multi-prompt/--multi-sort/--gen-compress/--gen-classify）、recall_diag_multi.py、rr_ab50.py |
| 9 | 新单测 21 个 | tests/unit/（job_lease / compact_budget / sync_watermark / conflict_query_trim） |
| 10 | 未来方向规划 | docs/TRINITY_FUTURE_DIRECTION_20260821.md |

## 八、未来方向（详见规划文档）

定位：**企业合规 + 时间感知的私有化记忆层**。
- 阶段一（0-3 月）：产品化闭环 + MCP 生态获客 + 合规打包 + 多模态打通
- 阶段二（3-12 月）：企业私有记忆 MVP（部署包 / 事实时间线 / 双模式）
- 阶段三（12-24 月）：垂直深耕 / 记忆即服务 / 市场协议
- 不做：通用分销（Mem0 通道）、继续追跑分（78% 已天花板）、学术叙事
- 止损：90 天零信号转个人工具；12 个月零付费收敛开源

## 九、关键文件索引

| 类别 | 路径 |
|---|---|
| 权威变更记录 | dsh-ops/EXECUTION.md |
| 架构/特性 | docs/ARCHITECTURE.md、FEATURE_OVERVIEW_20260815.md |
| 优化计划 | docs/OPTIMIZATION_PLAN_20260817.md（PlugMem 命题化）、OPTIMIZATION_FROM_LMEME_20260816.md |
| 未来规划 | docs/TRINITY_FUTURE_DIRECTION_20260821.md、TRINITY_COMMERCIAL_PLAN_V5_20260818.md |
| 部署拓扑 | docs/DEPLOYMENT_TOPOLOGY_20260818.md |
| 运维手册 | .dsh/skills/trinity-maintenance（服务/脚本/坑/命令） |
| 评测产物 | .trinity/bench-official/（route3*/rr_ab50/judge3_*） |
| 日志 | .trinity/logs/（dsh-maintenance/supervisor/autostart） |
| 备份 | .trinity/backups/（保留 14 天） |
| 记忆库 | ~/.trinity/store/trinity_store.db（权威）、trinity/data/aggregator_pool.json（检索面） |
