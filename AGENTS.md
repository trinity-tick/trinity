# AGENTS.md — Trinity Memory

> 本文件由 Trinity 生成（2026-08-24 15:26:38）。它让接入本仓库/工作区的 AI Agent
> 自动了解 Trinity 记忆层的存在、用法与当前状态。
> 相关规范背景：OpenAI AGENTS.md / Anthropic CLAUDE.md 的"文件即记忆"标准。

## Trinity 记忆层实时快照（生成于 2026-09-02 03:21:12，snapshot 任务自动刷新）

| 指标 | 值 |
|---|---|
| 会话数 | 246 |
| 结构事件数 | 28533 |
| 目标数 | 71 |
| Todos | 108 |
| 计划 | 0 |

### 活跃目标（active goals）

| 状态 | 阶段 | 轮次 | 目标 |
|---|---|---|---|
| active | active | 0 | 继续执行优化（第三轮）：1) GEN-3 生成侧再优化——KU/SS-P 类目专用提示词 + 上下文剪枝实验（每问只喂最相关 3-5 条 vs 10 条，测噪音对 AnswerAcc 的影响），全量 500q 对比 0.678 基线；2) CH-1 KG/混合通道归因——用 search_hybrid(fusion)  |
| active | - | 0 | 按已批准的建议清单全面优化 Trinity：P0-1 用 DSH agent/workflow 修复 Trinity 已知 bug（pytest 5 fail/6 error + keyword 多词 FTS5 检索 bug）；P0-2 用 dsh-credentials 消除 PG 密码/API key 明文并让维护 |
| active | - | 0 | 处理 benchmarks.md 遗留四项：① 跑官方 LongMemEval-S(500题)/LoCoMo(1982题) 或明确不可行时降级（如官方子集+如实标注）并更新对比表；② 统一 SQuAD 评测入口（消除 35.6% vs 98.3% 双口径）；③ 修复 Cluster Stress Raft 三节点全 l |
| active | - | 0 | 执行 smartcos-wms 剩余全部建议：①golangci 债务逐包清理（errcheck 249/unused 47/ineffassign 29 等共 328 项，清理后 lint 显著归零或大幅下降且 build/test 全绿）；②实现 12 个 MOCK-ONLY API 端点（carriers/rou |
| active | - | 0 | 按行业操作页形态（扫码优先+任务队列+动作按钮+状态自动流转）重做 SmartCos WMS 全部作业页面：收货作业(ArrivalCounting)、上架作业(Putaway)、称重作业(PackageWeighing)、发货作业(ShipConfirm)、复核作业(VerificationWorkbench)、打包 |
| active | - | 0 | 按 wms-ui-assessment 建议全方位精细化执行：P1（拣货/收货行级扫码确认、发货运单回传记录、工作台 AI 闭环状态卡）、P2（复核页组件化、上架 AI 建议卡突出、收货差异转质检）、P3（称重 DWS 读秤增强、打包页对接 box_types 主数据、盘点体验微调）；每项后端+前端落地、真实数据/端点 |
| active | - | 0 | 执行 EXECUTION_PLAN_V2.md 全部 14 个项目（A1-A5、B1-B5、C1-C4）：每个项目产出可运行工件并验证，修复评测发现的 2 个 API bug，更新执行计划状态，最终汇总完成情况与遗留项。 |
| active | - | 0 | 按已批准的 Trinity 发展规划执行全部建议：里程碑1（统一 SQuAD 评测口径、修复 Cluster Stress Raft 单 leader 异常、跑官方 LongMemEval-S/LoCoMo/BEAM 1M-10M 基准、同步版本号 v8.2.0）；里程碑2（decay 接入真实 LLM 摘要、决策并落 |
| active | - | 0 | 按 docs_site/optimization-plan.md 的 8 个优化点全方位执行：1) 答案生成评测 harness（mock 500q × DeepSeek → accuracy/latency/cost，逐类目报告）；2) PG FTS GIN 索引 + BEAM 规模复测（对比有/无索引延迟）；3)  |
| active | active | 0 | 完成旺店通WMS操作规则规范迭代收尾：依据已有9域规则文档与知识来源，补齐并验证三个补充域文件（企业版补充域/帮助手册补充域/跨境版补充域）到 C:/Users/Administrator/KnowledgeBase/AI_WMS/smartcos-wms/docs/，并将迭代结果写入Trinity记忆。 |

### 最近会话（recent sessions）

- session-892e6701-a0bb-48f7-82db-19df3bbc2f1d [active] (untitled)
- session-9b103746-ad8b-4a5d-945e-3c2a0dd1251b [active] (untitled)
- session-0b7708ab-4e80-420d-9a7a-af3785817a1a [active] (untitled)
- session-598f3b29-5ca9-42b7-9fa9-f9b20114983a [active] (untitled)
- session-a94c6721-00bf-4ed0-a91d-bea3fcb50e11 [active] (untitled)


## 1. Trinity 是什么

Trinity 是长程记忆系统（Memory OS）：跨会话保存并检索事实、偏好、决策与
会话轨迹。它不是普通 RAG 知识库——记忆带 CRDT 版本链、SHA-256 审计、
时间感知与多租户隔离（persona/session/agent/tenant）。

## 2. 如何检索记忆

Agent 在回答"是否记得… / 之前做过… / 用户偏好…"类问题时，应当先检索
Trinity，而不是仅凭当前上下文猜测。

- **MCP（推荐）**：本机 MCP server 暴露 `memory_search` / `memory_write` /
  `memory_update` / `memory_delete` / `audit_query` / `memory_tag_search`
  （stdio 模式无鉴权；streamable-http :8003 用 Bearer token）。
- **REST**：`POST http://127.0.0.1:8001/memory/search/hybrid`（body: {"query":"...", "top_k":5}；
  混合检索，5 通道 RRF 融合）。注：GET /memory/search 不存在（404）——2026-09-01 文档修正。
- **CLI**：`python -m trinity search --query "..." --top-k 5`。

检索建议：
- 默认用混合模式（hybrid），短查询走 FTS 轻通道（毫秒级）。
- 检索不到时放宽关键词（Trinity 用 jieba 中文分词 + BM25 + 向量 + 图谱
  多通道融合；同义改写后再试一次）。
- 关键事实请用 `audit_query` 核对版本链与来源。

## 3. 如何写入记忆

- 值得记住的才写：用户偏好、事实、决策、完成的工作、踩过的坑。
- 内容自包含：让未来的 agent 不看本对话也能读懂（含路径、工具名、数字）。
- 建议结构（与 Trinity 记忆契约一致）：

```
[类型] 日期 一句话标题
- 目标/任务: ...
- 关键决策与理由: ...
- 结果/产出: ...
- 坑与经验: ...
- 下一步: ...
```

- 标签保持一致（项目名/领域/类型），importance 0.4-0.6 常规、0.7+ 决策/事故。
- 用 `memory_update` 更新已有记忆而不是重复写入新条目。

## 4. 会话身份与隔离

- 每个 DSH 会话自动注册为独立 agent 身份（agent_id=dsh-<sessionId>）。
- 未显式指定时检索默认按当前会话隔离；空结果自动回退全局检索。
- 多租户：persona_id / session_id / agent_id / tenant_id 四级过滤。

## 5. 常用命令

```bash
# 搜索记忆（混合检索，top-5）
python -m trinity search --query "用户偏好" --top-k 5

# 引擎诊断（版本/存储/通道/规模）
python -m trinity diagnostics

# 服务健康
curl -s http://127.0.0.1:8001/health

# 维护（decay/tiers/sync）
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks all
```

## 6. 注意事项（known pitfalls）

- SQLite 大库多进程共享有写锁风险：批量写入用维护链（每日 03:00 自动），
  不要并发大量 ingest。
- 引擎默认检索路径是 FTS5（R@5 0.975 > hybrid-rrf 0.942）；显式
  `search_hybrid` 才走 5 通道融合。
- 语义缓存默认 memory 后端（TTL 300s）：刚写入的记忆可能短暂命中旧缓存，
  敏感操作可用 `TRINITY_CACHE_BACKEND=off` 临时关闭。
- PG 连接必须用 127.0.0.1（localhost 解析 IPv6 会被 pg_hba 拒绝）。

## 7. 安全与可证明性（R8-R9 起出厂默认）

- **存储加密默认开启**（AES-256-GCM）：content 列密文落盘，FTS 不受影响；
  `TRINITY_STORAGE_ENCRYPTION=off` 显式关闭。
- **记忆投毒写入过滤**（OWASP AG 类）：写路径扫描注入模式，高危命中自动
  归档 + `INJECTION_ISOLATED` 审计；`TRINITY_INJECTION_SCAN=off` 关闭。
- **可证明记忆回执**：`GET http://127.0.0.1:8001/audit/receipt/{memory_id}`
  返回当前哈希/版本链/审计链完整性（验证者可独立重算 SHA-256 对账）；
  `GET /audit/integrity` 全链校验。
- **健康真实上报**：`/health` 含 engine 组件——引擎故障报 degraded + 错误
  详情（不再有"健康假象"）；写锁竞争时引擎只读降级（检索可用、写报错）。

## 8. 图谱与时序能力（R7-R8 增强）

- **edge bi-temporal**：`GET /graph/relations/at?at_time=...` 时点查询；
  创建关系可带 valid_from/valid_to（对齐 Zep/Graphiti）。
- **PPR 图谱通道**（HippoRAG 式）：混合检索的图谱通道含 PPR 多跳扩散
  （`TRINITY_GRAPH_PPR` 默认 on）。

## 9. 可观测指标（/metrics）

- 记忆命中率/写放大：`trinity_write_amplification` /
  `trinity_queries_by_source_total` / `trinity_semantic_cache_hit_rate_pct` /
  `trinity_last_query_ts`——Prometheus 可直接抓取。
