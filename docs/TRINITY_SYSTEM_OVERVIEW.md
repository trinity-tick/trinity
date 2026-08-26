# Trinity 系统全景说明（组成 · 结构 · 作用 · 功能）

> 2026-08-24 实测版。规模：339 Python 模块 / 88 运维脚本 / 105 测试文件 /
> 147 API 路由 / 5 常驻服务 / 13,447 条记忆。六轮深度优化（R7-R14）后
> "宣称能力"与"运行事实"完全对齐。

---

## 一、Trinity 是什么（作用）

**Trinity = 记忆操作系统（Memory OS）**——面向 AI Agent 的跨会话长期记忆
基础设施。解决 Agent 的"失忆"问题：

- **AI Agent 默认无状态**：每次会话都忘记之前的偏好、决策、踩过的坑；
- **长上下文 ≠ 记忆**：1M token 窗口内信息也会被稀释（context rot），
  且成本随 token 线性增长；
- **Trinity 的作用**：把"值得记住的"持久化、可检索、可审计、可治理——
  Agent 接上后获得跨会话连续性（记住用户偏好、项目事实、历史决策、
  失败教训），并支持多 Agent 共享、合规审计、数据所有权。

**一句话**：给 AI Agent 装一个"大脑的海马体"——写路径记住、检索路径
想起、治理路径忘掉该忘的、审计路径证明没被篡改。

---

## 二、组成成分（六层架构）

```
┌─────────────────────────────────────────────────────┐
│ ① 接入层（怎么被用）                                  │
│   MCP(stdio/SSE/streamable-http) · OpenAI兼容Gateway  │
│   · Mem0兼容 · REST 147端点 · DSH原生插件 · GraphQL    │
├─────────────────────────────────────────────────────┤
│ ② 检索层（怎么想起来）                                │
│   5通道RRF混合(向量/BM25/图谱/聚合池/过程性) · FTS5    │
│   · PPR图谱扩散 · 自适应路由 · 语义缓存 · 证据/置信度   │
├─────────────────────────────────────────────────────┤
│ ③ 存储层（怎么记住）                                  │
│   SQLite权威(FTS5+jieba) · PG镜像 · 聚合池 · FAISS     │
│   · 知识图谱(实体/关系/链接) · CRDT版本链 · AES加密     │
├─────────────────────────────────────────────────────┤
│ ④ 治理层（怎么忘记/维护）                             │
│   decay衰减 · tiers分层 · consolidation整合 · dedup   │
│   · 压缩(78.2%节省) · 每日维护链(17任务) · 噪声/预算治理│
├─────────────────────────────────────────────────────┤
│ ⑤ 安全层（怎么可信）                                  │
│   存储加密 · 投毒写入过滤 · 压缩注入守卫 · RBAC6角色    │
│   · 审计链(14k条可验证) · 记忆回执 · GDPR工具           │
├─────────────────────────────────────────────────────┤
│ ⑥ 智能层（怎么进化）                                  │
│   RouteReasoner(QA 78%) · 命题提取 · Persona画像       │
│   · 自进化(MetaEvolution) · RL反馈 · 技能锻造          │
└─────────────────────────────────────────────────────┘
```

### 关键组件清单

| 组件 | 类型 | 作用 |
|---|---|---|
| `trinity/core/client/` | 引擎客户端（12 mixin） | ingest/search/update/delete/reason 统一入口 |
| `trinity/adapters/sqlite/` | 存储适配器（11 mixin） | 建表/CRUD/FTS/审计/图谱/加密 |
| `trinity/retrieval/` | 检索器 | hybrid/bm25/ann/graph 融合 |
| `trinity/agents/aggregator/` | 聚合池 | 多 Agent 共享记忆 + 向量索引 |
| `trinity/kgraph/` | 知识图谱 | 实体关系 + PPR 增强 |
| `trinity/qa/route_reasoner.py` | QA 推理器 | 78% 分题型策略路由 |
| `trinity/llm/client.py` | LLM 适配层 | Structured Outputs + reasoning effort + 缓存 |
| `trinity/security/` | 安全 | 加密/injection 扫描 |
| `trinity/mcp/` | MCP server | 三形态协议服务 |
| `trinity/memory/` | 记忆模块 | persona/压缩/命题提取/分层 |
| `trinity/api/server/` | REST API | 147 路由（14 个 router） |
| `gateway/server.py` | 兼容层 | OpenAI/Mem0 兼容 |

---

## 三、运行结构（服务拓扑与数据流）

### 3.1 服务拓扑（5 常驻服务）

| 服务 | 端口 | 形态 | 作用 |
|---|---|---|---|
| trinity-api | :8001 | FastAPI | 主服务：147 路由（记忆/图谱/身份/A2A/市场/审计/进化） |
| trinity-mcp | :8000 | SSE | MCP 协议服务（memory_search/write 等 9 工具） |
| mcp-http | :8003 | streamable-http | MCP v2（Bearer 鉴权 + well-known 元数据） |
| gateway | :8002 | OpenAI/Mem0 兼容 | 让任意 OpenAI SDK 5 分钟接入（记忆注入 + __memory_write__） |
| collector | 进程 | 守护 | 主动采集（DSH 事件源 + 6 connectors） |
| PG (docker) | :5430 | 维护库 | decay/tiers/mirror 治理目标 |

### 3.2 数据流（写→存→检→证）

```
Agent 写入
  → ingest（CRDT 版本化 + SHA-256 审计 + 加密落盘 + 投毒扫描）
  → 后台加工（实体提取 + 语义关联 + 画像增量 + ANN 增量）
  → SQLite 权威库（FTS5 索引 + 图谱边）

Agent 检索
  → search（短查询 FTS 轻通道 0.6ms / 复杂查询 5 通道 RRF 融合）
  → 语义缓存（memory 后端，TTL 300s）
  → 结果附证据（category/version_count/审计可查）+ 置信度

治理（每日 03:00 维护链）
  → decay（真实 LLM 摘要）→ tiers → consolidate → dedup
  → sync（聚合池）→ compact → db-health（WAL checkpoint）
  → agentsmd（AGENTS.md 刷新）→ noise-gov（噪声/预算治理）→ backup

证明（合规场景）
  → GET /audit/receipt/{id}（当前哈希 + 版本链 + 审计完整性）
  → 验证者可独立重算 SHA-256 对账
```

### 3.3 三库拓扑（2026-08-16 收敛后）

| 库 | 位置 | 角色 |
|---|---|---|
| SQLite 权威库 | `~/.trinity/store/trinity_store.db` | 运行时权威（13,447 条） |
| PG 维护库 | docker :5430 | 治理镜像（decay/tiers/mirror 目标） |
| 聚合池 | `data/aggregator_pool.json` | 多 Agent 共享检索面（1,584 条） |

---

## 四、功能全景（能做什么）

### 4.1 核心记忆功能
- **写入**：ingest（CRDT 版本化 + 审计 + 加密）、update、delete（软删）、
  chronicle（事件序列）、批量写入；
- **检索**：混合检索（hybrid）、语义、图谱、精确、跨模态、时点查询、
  标签搜索、按租户/会话/Agent 隔离、doc 分层（知识/记忆分离）；
- **QA**：开放域推理（RouteReasoner 78%）、分题型策略（multi/temporal/
  preference 各自最优路由）；
- **生命周期**：衰减、分层、整合、去重、压缩、归档、恢复、遗忘（GDPR）。

### 4.2 图谱与时序
- 实体/关系/链接管理（12k 实体 / 30k 关系）；
- edge bi-temporal（valid_from/valid_to + 时点查询）；
- PPR 图谱扩散（HippoRAG 式多跳）。

### 4.3 多 Agent 与身份
- A2A v0.3（AgentCard/任务/ACL）、共享聚合池（命名空间隔离）；
- 身份注册/漂移检测/重建、persona 画像（偏好/事实）、会话隔离。

### 4.4 安全与合规
- AES-256-GCM 存储加密（默认 on）、投毒写入过滤（OWASP AG 类）、
  压缩注入守卫、RBAC 6 角色、审计链（14,019 条可验证）、
  记忆回执（可证明）、GDPR 导出/删除权。

### 4.5 集成与生态
- MCP 三形态（9 工具）、OpenAI 兼容 Gateway、Mem0 兼容、REST 147 路由、
  GraphQL、DSH 原生插件（19 工具）、联邦/多机同步、AGENTS.md 导出、
  记忆 markdown/git 全集导出（反锁定）。

### 4.6 可观测与运维
- /health（engine 真实上报）、/metrics（命中率/写放大/缓存）、
  SLO 报告、诊断、进化周期、备份（14 天保留）、supervisor 自愈。

---

## 五、实测能力基线（2026-08-24）

| 项 | 值 |
|---|---|
| 检索召回 | LongMemEval-S R@5 **0.992**（官方 500 题） |
| QA | **78%**（judge3 三票，同口径超 PlugMem/Zep） |
| 检索延迟 | FTS 0.6ms / API 16-20ms |
| 压缩节省 | 78.2% token |
| prompt cache | 84.58% 命中（长前缀实测≈2 折） |
| 测试 | 1,151 passed / 0 failed |
| 数据 | 13,447 记忆 / 30,504 关系 / 14,019 审计 |
| 服务 | 5 常驻全绿 / tier=full |

---

## 六、一句话总结

**Trinity = 六层架构（接入/检索/存储/治理/安全/智能）× 五常驻服务 ×
147 API × 全生命周期记忆能力**：给 Agent 装上可持久、可想起、可治理、
可证明、可共享、可进化的"记忆操作系统"——从单 Agent 个人助手到多
Agent 企业级合规记忆层全覆盖。
