# Trinity 未来项目落地方案（2026-08-14 制定）

> 基于当天实测：Trinity v8.2.0，API :8001 全绿（6 检索通道 full），聚合池 10,632 条
> （WMS 订单/库位数据 10,601 条 + B 站/网页采集 259 条 + insight 306 条），引擎库 11,332 条，
> 15 个 agent 已注册但未实际协作，图谱 1,638 实体但仅 2 条关系。

---

## 0. 共用底座：先补一块短板

**✅ 已执行（2026-08-14）**：实体 1,638 → **11,009**，关系 2 → **28,043**。
- 概念层：6 仓库 / 18 店铺 / 15 物流 实体 + 加权共现关系（服务店铺/合作承运/使用承运，weight=共现次数）
- 订单层：9,318 订单实体 + 发货仓库/下单店铺/承运商 关系（~27.9k）
- 构建脚本：`scripts/build_graph_relations.py`（幂等可重跑，`--dry-run` 预览）；分析脚本 `scripts/analyze_graph_data.py`
- 验证：`/graph/traverse` 从「拼多多彩棠美妆专卖店」2 跳得到 7,600 订单 + 5 物流 + 3 仓库
- 数据局限：记录中无 SKU/库位字段，SKU→库位 关系暂缺（后续 wms_knowledge 记忆补齐）

> 原有设计说明保留如下，供扩展参考：

- 用 `POST /graph/entities/search` + 规则/LLM 抽取 实体间关系（订单→仓库、SKU→库位、品牌→店铺…）
- 关系写入 `/graph/relations`，配合 `/graph/traverse` 做多跳查询
- 收益：检索从"相似度召回"升级为"可推理的知识图"，A2A 协作、市场、身份漂移检测都依赖它

---

## 2026-08-14 图谱关系层执行记录

- 数据源：聚合池 9,332 条 WMS 管道记录（仓库/店铺/物流/订单字段齐全）
- 写入：`scripts/build_graph_relations.py` → 主库 `~/.trinity/store/trinity_store.db`（SqliteAdapter 同路径）
  - Tier A 概念层：warehouse 6（彩棠派样仓 3,555 / 彩棠-拼多多仓 2,173 / 彩棠-拼多多派样 1,872 / 彩棠 1,269 / 彩棠-唯品仓 448 / 印彩巴哈 13）、store 18、logistics_company 15
  - Tier B 订单层：order 9,318（订单编号），properties 含 order_no/outbound_no
  - 关系：发货仓库 9,317 / 下单店铺 9,318 / 承运商 9,318 / 服务店铺 27 / 合作承运 19 / 使用承运 27（概念层带 weight）
- 幂等性：实体按 name upsert、关系 INSERT OR IGNORE（sha256 id），可重复运行
- 注意：原「彩棠」company 实体（name 唯一约束）被按需更新为 warehouse 类型并带 count=1269

---

## 1. WMS 订单/库位智能问答与日报（数据现成，最快出成果）

**背景**：聚合池 9,318 条订单记忆（订单编号/仓储单号/出库单号/发货时间…）、彩棠派样仓 3,552、拼多多仓 2,173。
**目标**：对 10,601 条业务记忆做自然语言问答 + 每日自动摘要 + 异常提示。
**用到的 Trinity 能力**：`/agents/memory/search`（聚合池检索）、`/memory/search/hybrid`（引擎库）、`/agents/memory/bulk_write`（日报回写）、evolution 的 `track-access`（热门记忆）。
**MVP（1 周）**：
1. 写 `wms_qa.py`：输入自然语言问题 → hybrid 检索 Top-K → LLM 组装答案（引用记忆 id）
2. 写 `wms_daily_report.py`：定时聚合当日订单记忆 → 生成日报 → `bulk_write` 回写为 `category=insight`
3. 接 DSH/微信：把问答封装成 MCP 工具或 HTTP 接口
**验收**：10 个典型问题（"彩棠派样仓今天发了多少单""拼多多仓哪个 SKU 出货最多"）准确率 ≥ 90%，日报自动生成并入库。

---

## 2. RAG 飞轮 v4：Trinity 做统一记忆层（接现有 RAG 服务）

**背景**：现有 RAG 服务在迁移 Ollama（rewrite-url 端口超时、RerankerSkip、Weaviate 空库已处理）。
**目标**：把 Trinity 的 47 通道检索 + 进化（衰减/冲突/偏好）作为 RAG 服务的"记忆管理层"。
**用到的 Trinity 能力**：`/memory/search/hybrid`、`/memories/conflicts/resolve`、`/memory/compress`（token 压缩）、`/evolution/cycle/run`（定期自我进化）。
**MVP（2 周）**：
1. 在 RAG 服务加一个 retrieval 适配器：优先 Trinity hybrid，回退 BM25
2. 检索结果带 memory_id → 命中/未命中反馈写回（`track-access`、`/evolution/feedback`）
3. 每 24h 跑一次 `-Tasks decay,tiers`，观察进化状态文件
**验收**：RAG 问答质量对比 v3 提升（自建 50 题评测集），衰减后热记忆命中率不降。

---

## 3. 原创 IP 世界观一致性助手（差异化最强）

**背景**：《墨时》（五卷本长篇，玄墨/过去、赤墨/现在、金墨/未来）、《裂渊纪》（千万字、七境顺天体系）、《钢铁共和国》（13 台机甲+5 场战役视觉设计）。
**痛点**：长篇连载最怕设定前后矛盾——角色能力、时间线、力量体系、地名。
**用到的 Trinity 能力**：`/identity/agents/{id}/anchors`（身份锚点）、`/identity/drift-check`（漂移检测）、`/graph/entities`（角色/地点实体）、`/memories/{id}/links`（设定间关联）、`/memory/search/cross-modal`。
**MVP（2 周）**：
1. 每个 IP 注册一个 agent（`/identity/register`），按卷写入设定记忆（角色、时间线、能力、物品）
2. 写作助手：输入新章节草稿 → 检索相关设定 → 输出"一致性检查报告"（引用冲突记忆 id）
3. 可视化：图谱渲染世界观（角色-关系-事件），`/graph/traverse` 查"苏默与赤墨的所有关联"
**验收**：《墨时》EP01 及后续章节跑通检查，能自动标出 3 类以上设定冲突；图谱可视化可演示。

---

## 4. 多智能体内容工厂（A2A，激活已注册的 15 个 agent）

**背景**：a2a 全套端点就绪（register/dispatch/message/tasks/security），15 个 agent 注册但 0 次协作。
**目标**：内容流水线——采集 → 提炼 → 写作 → 发布，各环节独立 agent 共享记忆。
**用到的 Trinity 能力**：`/a2a/agents/register`、`/a2a/marvis/dispatch`、`/a2a/tasks`、`/agents/memory/search`（共享池）、`/audit/agents/{id}/replay`（可审计）。
**MVP（2 周）**：
1. 注册 4 个 agent：harvester（B 站/网页采集）、extractor（知识提炼）、writer（成文）、publisher（多渠道分发）
2. 用 `/a2a/marvis/dispatch` 编排一条"今天采集 3 个视频 → 提炼 → 出 1 篇笔记"的流水线
3. 全链路写入聚合池，`/agents/memory/insights` 里能看到 agent 贡献分布
**验收**：一条完整流水线跑通且产出入库；insights 显示 4 个 agent 各自 memory_count 增长；audit replay 可回放任一步骤。

---

## 5. 记忆市场变现（把数据变成资产）

**背景**：10,632 条行业记忆（WMS 词库、供应链资料、专业选择笔记）可整理成"知识包"。
**用到的 Trinity 能力**：`/market/list`、`/market/price/{modality}`、`/market/estimate`、`/market/reputation/{agent_id}`（信誉体系已内置）。
**MVP（1 周）**：
1. 用 `/agents/memory/export` 导出 WMS/供应链两个知识包，脱敏后上架 `/market/list`
2. 写一个"买断/订阅"流程：下单 → 交付 → `/market/report` 记录交易
3. 定价参考 `/market/estimate` 的估值
**验收**：知识包可检索、可下单、交易记录入库；reputation 随交易累积。

---

## 6. 个人第二大脑产品化（吃自己狗粮）

**背景**：已采集 B 站知识视频 198 条、网页 61 条、marvis 同步 4 条；进化引擎在跑但 preference/pattern 还是 0。
**用到的 Trinity 能力**：`/evolution/hotspots`、`/evolution/suggestions`、`/memories/age`、`/agents/memory/insights`。
**MVP（2 周）**：
1. 每周摘要：从本周采集内容生成知识周报（`category=summary`）
2. 宫殿记忆法复习提醒：按 `access_count` + `importance` 排复习队列，定时推送
3. 家庭保险决策助手：写入方案选型偏好 → `identity/route` 做偏好路由，每次咨询自动带上历史决策
**验收**：周报自动产出；复习提醒可交互；保险咨询 3 轮后能主动引用之前的方案对比记忆。

---

## 7. 运维与基建收尾（随时可做，优先级 P2）

- **聚合池文件守护**：把"pool JSON 缺失/损坏"纳入 supervisor 检查（当天已把 `.corrupt` 备份恢复为现场文件，见下）
- **collector 事件为空**：scanner 280+ 周期但 events_captured=0，需确认采集源配置（B 站/网页源是否在跑）
- **git 8 个未提交文件**：health 检查提示工作区不干净，建议提交或明确忽略
- **图谱关系抽取**：见第 0 节，是所有项目的公共前置

---

## 优先级建议

| 顺序 | 项目 | 理由 |
|---|---|---|
| 1 | 图谱关系层（第 0 节） | 公共底座，1-2 天 |
| 2 | WMS 智能问答（第 1 节） | 数据现成，最快见成果 |
| 3 | IP 一致性助手（第 3 节） | 差异化最强，可做作品集 |
| 4 | A2A 内容工厂（第 4 节） | 激活闲置能力，展示价值 |
| 5 | RAG 飞轮 v4（第 2 节） | 与现有业务线衔接 |
| 6 | 市场/第二大脑（第 5、6 节） | 变现与个人工具，按需 |

## 2026-08-14 当日维护记录

- 聚合池现场文件 `aggregator_pool.json` 缺失（被轮转为 `.corrupt_1786692448`，12.2MB）：
  已验证 `.corrupt` 是完整有效 JSON（10,632 条，与 API 内存池一致），已复制恢复为 `aggregator_pool.json`，
  API 校验 10,632 条正常；维护脚本 decay/tiers 加载时也确认 `pool restored from disk: 10632 memories`。
- 全量维护（health+evolution+decay+tiers+sync+selftest）同日执行：
  - health / evolution（第 5 轮）/ decay（压缩 100 条→7 条摘要，归档 100，0 失败，mock LLM 模式）/ tiers（core=3, recall=419, archival=78, 0 变更）/ sync（Hermes+Marvis 双向，新增 2 条）全部 OK。
  - **selftest 挂死**：`run_all_self_tests.py` 某模块子进程 11+ 分钟 CPU 零推进
    （单模块超时上限 30s 失效，因为 ProcessPoolExecutor shutdown(wait=True) 会等挂死 worker）。
    已终止（PID 64980/41480），其余任务不受影响。建议后续排查是哪个 self_test 阻塞（疑似无超时网络调用），
    或改用 `--target` 收窄范围分批跑。
