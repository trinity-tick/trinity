# AGENTS.md — Trinity Memory

> 本文件由 Trinity 生成（2026-08-24 15:26:38）。它让接入本仓库/工作区的 AI Agent
> 自动了解 Trinity 记忆层的存在、用法与当前状态。
> 相关规范背景：OpenAI AGENTS.md / Anthropic CLAUDE.md 的"文件即记忆"标准。

## Trinity 记忆层实时快照（生成于 2026-08-24 15:26:38）

| 指标 | 值 |
|---|---|
| 会话数 | 240 |
| 结构事件数 | 26663 |
| 目标数 | 71 |
| Todos | 108 |
| 计划 | 0 |

### 活跃目标（active goals）

| 状态 | 阶段 | 轮次 | 目标 |
|---|---|---|---|
| active | active | 0 | 完成 DeepSeek Harness 迭代升级：全局 @deepseek-ai/dsh 0.1.0-rc.6 → 0.1.0-rc.7(latest)，核对 dsh-trinity 插件版本配套与 ~/.dsh profiles 补丁，重启 web 宿主(:3080)后验证 GUI 与 trinity_* 工具可用， |
| active | active | 0 | 完成旺店通WMS操作规则规范迭代收尾：依据已有9域规则文档与知识来源，补齐并验证三个补充域文件（企业版补充域/帮助手册补充域/跨境版补充域）到 C:/Users/Administrator/KnowledgeBase/AI_WMS/smartcos-wms/docs/，并将迭代结果写入Trinity记忆。 |
| active | active | 0 | 继续执行优化（第三轮）：1) GEN-3 生成侧再优化——KU/SS-P 类目专用提示词 + 上下文剪枝实验（每问只喂最相关 3-5 条 vs 10 条，测噪音对 AnswerAcc 的影响），全量 500q 对比 0.678 基线；2) CH-1 KG/混合通道归因——用 search_hybrid(fusion)  |
| active | - | 0 | 按已批准的 Trinity 发展规划执行全部建议：里程碑1（统一 SQuAD 评测口径、修复 Cluster Stress Raft 单 leader 异常、跑官方 LongMemEval-S/LoCoMo/BEAM 1M-10M 基准、同步版本号 v8.2.0）；里程碑2（decay 接入真实 LLM 摘要、决策并落 |
| active | - | 0 | 按 docs_site/optimization-plan.md 的 8 个优化点全方位执行：1) 答案生成评测 harness（mock 500q × DeepSeek → accuracy/latency/cost，逐类目报告）；2) PG FTS GIN 索引 + BEAM 规模复测（对比有/无索引延迟）；3)  |
| active | - | 0 | 按优化建议继续执行（第二轮）：1) 生成侧优化——重点提升 TR 时序类目 AnswerAcc（先分析 TR 题结构/失败模式，改进答案提示词与 judge 评分，重跑 500q 对比 0.602 基线）；2) OPT7 后续——修复 Trinity.search 的 mode 参数装饰性问题（semantic/hyb |
| active | - | 0 | 继续执行优化（第三轮）：1) GEN-3 生成侧再优化——KU/SS-P 类目专用提示词 + 上下文剪枝实验（每问只喂最相关 3-5 条 vs 10 条，测噪音对 AnswerAcc 的影响），全量 500q 对比 0.678 基线；2) CH-1 KG/混合通道归因——用 search_hybrid(fusion)  |
| active | - | 0 | 执行 EXECUTION_PLAN_V2.md 全部 14 个项目（A1-A5、B1-B5、C1-C4）：每个项目产出可运行工件并验证，修复评测发现的 2 个 API bug，更新执行计划状态，最终汇总完成情况与遗留项。 |
| active | - | 0 | 执行 smartcos-wms 剩余全部建议：①golangci 债务逐包清理（errcheck 249/unused 47/ineffassign 29 等共 328 项，清理后 lint 显著归零或大幅下降且 build/test 全绿）；②实现 12 个 MOCK-ONLY API 端点（carriers/rou |
| active | - | 0 | 按行业操作页形态（扫码优先+任务队列+动作按钮+状态自动流转）重做 SmartCos WMS 全部作业页面：收货作业(ArrivalCounting)、上架作业(Putaway)、称重作业(PackageWeighing)、发货作业(ShipConfirm)、复核作业(VerificationWorkbench)、打包 |

### 最近会话（recent sessions）

- `session-2a57782f-210b-47c4-9262-19006693eb42` [active] (untitled)
- `session-f66f1c7f-a632-4b35-a894-2a88678d67e3` [compacted] (untitled)
- `session-e99b9491-2e7c-4405-bd52-187b3c498fe2` [compacted] (untitled)
- `session-9c0e55d0-b601-483d-8cb8-e05dbf0328c7` [compacted] (untitled)
- `session-48b90a9b-15cd-4933-a5d8-0846b0a2fc92` [compacted] (untitled)

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
- **REST**：`GET http://127.0.0.1:8001/memory/search?q=...&top_k=5`
  （混合检索；`/memory/search/hybrid` 走 5 通道 RRF 融合）。
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
