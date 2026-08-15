# DSH × Trinity 融合为单一系统 —— FUSION PLAN（2026-08-15 修订）

> 目标（用户明确修正）：**以 Trinity 为主体，把 DSH 的结构框架融合进 Trinity**——
> 不是两系统对接，也不是系统合并（进程/部署），而是让 Trinity 原生承载
> DSH 的编排结构（会话事件流 / turn-step / 工具轨迹 / goal / todo /
> request-header），DSH 作为结构生产者自动同步；Trinity 由此具备
> DSH 式结构：会话即事件流、轨迹可回放、goal 可追踪。
> 每阶段改动、验证、回滚记录于 `dsh-ops/EXECUTION.md`。

---

## 一、目标形态（单一系统：Trinity 承载 DSH 结构）

```
┌──────────────────────────────────────────────────────────────┐
│  DSH-Trinity 单一系统（Trinity 为主体，承载 DSH 结构）          │
│                                                              │
│  结构层（DSH 结构原生承载，新增 dsh_* 表，可查/可回放/可审计）  │
│    dsh_sessions · dsh_events（turn/step/消息/工具轨迹）        │
│    dsh_goals · dsh_todos · dsh_headers                        │
│  记忆层（既有）：47 通道检索 · CRDT 版本化 · 审计链 · 图谱      │
│  编排面（DSH 作为结构生产者）：                                │
│    @deepseek-ai/dsh-trinity 插件订阅 session/event 流          │
│    → 自动同步结构 → Trinity 原生存储                          │
│  ────────────────────────────────────────────────             │
│  对外面：REST :8001 / MCP SSE :8000 / gateway :8002（保留）    │
└──────────────────────────────────────────────────────────────┘
```

**核心转变**：
- DSH 会话不再是"外部记忆客户端"，而是 Trinity 的**结构生产者**——
  每个 turn/消息/工具调用/goal 变更自动成为 Trinity 内可查询的结构事件
- Trinity 获得 DSH 式编排结构：`trinity_trajectory` 可回放任何 DSH 会话的
  完整工具轨迹与对话流；`trinity_goal` 追踪长期目标状态机
- 记忆（语义）与结构（编排）双闭环共存于同一引擎库

---

## 二、阶段划分与状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| F0 | 事实核查（引擎直连 1.5s、日志 stdout 污染点、DSH 插件机制、MCP 工具模式） | ✅ 2026-08-14 |
| F1 | 引擎 worker（stdio NDJSON 直连，stdout 隔离） | ✅ 12/12 全通（EXECUTION 26.2） |
| F2 | 原生插件（spawn worker、15 个 trinity_* 工具） | ✅ 注册 + 调用链全通（EXECUTION 26.3/27.3） |
| F3 | 数据源收敛（聚合池/PG 角色明确、检索统一走引擎） | 🟡 会话内已统一（worker 直连引擎库） |
| F4 | 身份与会话归属（DSH 会话自动注册 Trinity 身份） | ✅ 自动注入 + 多会话隔离实测（EXECUTION 26.5） |
| F5 | **结构融合（DSH 结构 → Trinity 原生承载）** | ✅ dsh_* 表 + 插件事件订阅 + 端到端实测（EXECUTION 27） |
| F6 | 结构层 REST 暴露 + schedule/subagent 映射 + 双闭环回归 | ✅ /structure/* 5 端点 + 17 工具 + 全量回归（EXECUTION 28.1-28.5） |
| F7 | 真实会话验证 + 运维修复 | ✅ headless 真实会话 7 事件自动流入 + supervisor .venv→系统 Python 修复（EXECUTION 28.7-28.8） |
| F8 | 结构层写端点 + GraphQL + 共享模块 + 双闭环审计 | ✅ structure_store.py 单一实现 + POST /structure/* + GQL 5 字段 + 审计 passed 3/0（EXECUTION 29） |
| F9 | web profile 加载新插件代码（重启，需用户操作） | ⏳ 已备 restart-web-profile.ps1；headless 已实证 |

---

## 三、F1 详情：engine worker 协议（NDJSON over stdio）

**进程**：`trinity/engine_worker.py`，常驻，由 DSH 插件 spawn。
**协议**：stdin/stdout 每行一个 JSON 请求/响应（NDJSON）。
**stdout 隔离**（关键）：引擎初始化日志走 stdout，worker 启动时
`os.dup(1)` 保留干净协议 fd → `sys.stdout = sys.stderr`（日志进 stderr），
协议写入保留的 fd。

**方法**（与 MCP 8 工具对齐，去掉协议层）：

| method | params | 返回 |
|---|---|---|
| ping | — | {"pong": true} |
| search | query, top_k, mode, persona_id, agent_id, session_id | {"results": [...]} |
| write | content, metadata, category, tags, importance | {memory_id, version_id, sha256_hash, timestamp} |
| update | memory_id, new_content | {old_version, new_version, sha256_hash} |
| delete | memory_id | {deleted, deleted_version, timestamp} |
| audit | memory_id | {version_chain, total_versions, current_status} |
| diagnostics | — | 全组件诊断 |
| chronicle | events, title, session_id | {session_id, event_count, tags} |
| tag_search | tags, top_k, session_id | 匹配列表 |
| identity_register | agent_id, name | 注册 DSH 会话为 Trinity 身份（F4） |

**错误**：`{"id": n, "error": {"message": "..."}}`；请求含 `id` 用于关联。

---

## 四、数据源收敛策略（F3）

现状三套：SQLite 引擎库（11,425 条，权威）/ 聚合池（10,632 条，文件快照）/ PG 镜像（1040+ 条）。

| 存储 | 融合后角色 | 动作 |
|---|---|---|
| SQLite 引擎库 `~/.trinity/store/trinity_store.db` | **唯一权威源**（读写/检索/审计全走它） | 保留 |
| 聚合池 `~/.trinity/data/aggregator_pool.json` | 对外 API 检索缓存视图 | 保留给外部端点；会话内检索不再依赖它；写入不再双写（或仅在 API 路径双写） |
| PG（Docker :5430 / 原生 :5432） | 批处理镜像层（decay/tiers 的存储后端） | 保留为批处理专用；不作为会话记忆权威 |

**检索统一**：会话内 `trinity_search` 一律走引擎 `engine.search()`（47 通道），
不再依赖聚合池（消除"池/库交集 0"造成的口径分裂）。

---

## 五、身份与生命周期（F4/F5）

- **身份**：worker 启动时注册 `identity_anchor`；DSH 会话创建时插件调
  `identity_register(agent_id="dsh-<session>")`；write/search 自动注入
  agent_id/session_id → 记忆天然归属当前 DSH 会话，多会话隔离。
- **生命周期**：插件内建 worker 监督（ping 探活 + 崩溃自动重启，指数退避）；
  supervisor/autostart 保留给 API/collector（对外面）；MCP stdio 实例移除
  （内部不再需要），MCP SSE :8000 保留对外。

---

## 六、回滚总则

- worker：删除 `trinity/engine_worker.py`
- 插件：从 `cordis.patch.yml` 移除 dsh-trinity insert，恢复 mcp-trinity 实例
- 数据：引擎库无结构性变更（worker 只是新调用面）；聚合池/PG 角色还原按阶段记录
- 对外面（REST/MCP SSE/gateway）始终保留，融合失败不影响外部系统

---

## 七、验收标准（融合完成定义）

1. `dsh trinity` 一条命令拉起整套（web + worker + 可选 API）
2. 会话内工具为原生 `trinity_*`（无 MCP 前缀），直连 worker，无 mcp 中间层
3. 写入→检索→审计→删除闭环在 worker 协议下全通
4. 记忆自动归属 DSH 会话（agent_id/session_id 注入，多会话隔离生效）
5. 检索统一走引擎库（不依赖聚合池），结果与 API 口径一致
6. 全量 pytest + API 回归 + 闭环 9/9 不回归
7. EXECUTION.md 完整记录各阶段与回滚
