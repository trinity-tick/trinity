# Trinity 服务器 + 多机实时记忆同步详细方案（v2，基于网络最优方案）

> 日期：2026-08-18 | 状态：**详细设计（待 P0 验证）**
> 参考网络最优方案：
> - Mem0 "Remote Memory for AI Agents at the Edge"（中心化记忆服务 + 边缘无状态 agent）
> - Mem0 Self-Hosted Docker 部署指南（一键容器化）
> - Multi-Agent Memory Architecture Patterns 2026（隔离记忆 / 共享池模式）
> - SAMEP（Secure Agent Memory Exchange Protocol, arXiv 2507.10562）（跨实例安全交换：加密/审计/幂等/冲突）
> - Agent Communication Matrix（REST=轻量集成 / MCP=agent↔工具 / A2A=agent↔agent）

---

## 一、总体架构（Mem0 Edge 模式的 Trinity 变体）

**网络共识 → 我们的形态**：记忆系统采用"中心化权威 + 本地写缓存"混合——
本地 Trinity 是**离线优先的写缓存**（agent 照常工作、断网可用），服务器是**记忆权威汇聚端**
（统一检索、治理、备份、跨机共享）。本地到服务器用**REST 批量增量推送**（轻量、幂等），
可选 MCP/A2A 做工具与 agent 间集成。

服务器（记忆权威汇聚端）:
  trinity-api :8001（REST 135+ 端点）
  trinity-mcp :8000（可选工具接入）
  PostgreSQL（权威库, 多机并发写安全）
  Redis（语义缓存）
  每日维护链（decay/tiers/consolidate/backup）

  电脑 A（本地实例）: trinity(SQLite 权威) + trinity-sync-agent + 本地智能体 1..n
  电脑 B（本地实例）: trinity(SQLite 权威) + trinity-sync-agent + 本地智能体 m..k
  同步: HTTPS+Bearer → 批量推送 /agents/memory/bulk_write

**角色分工**（对照网络方案）:
| 层 | 职责 | 对应网络方案 |
|---|---|---|
| 本地 Trinity | agent 交互的写缓存/离线优先/本地检索 | Mem0 "edge agents local" |
| sync-agent | 增量推送 + 断线续传 + 幂等 | Mem0 edge→cloud sync |
| 服务器 Trinity | 权威汇聚、跨机检索、治理、备份、审计 | Mem0/Zep 中心服务 |
| PG 权威库 | 多机并发写唯一入口 | Mem0 self-host PG/Qdrant |

---

## 二、服务器部署详细设计

### 2.1 Docker 部署（推荐）

**修正 docker-compose.yml（服务器版）**——三个关键差异（相对本机并存栈）:
1. api 端口：8001 直出（原 8005:8100 + command 不一致，是并存栈遗留）;
2. 仅保留生产服务：trinity-api、trinity-mcp、trinity-db、trinity-dash（可选）；移除 jaeger/telemetry（省资源）;
3. api/mcp 挂载 trinity-data 卷（容器内数据），PG 用 trinity-pgdata 卷。

**关键 compose 片段（服务器版，环境变量用占位符）**:
services:
  trinity-api:
    build: { context: ., dockerfile: Dockerfile }
    container_name: trinity-api
    ports: ["8001:8100"]          # 对外直出 REST
    volumes: [trinity-data:/app/data]
    environment:
      TRINITY_MCP_HOST: trinity-mcp
      TRINITY_MCP_PORT: "8000"
      TRINITY_API_KEY: "REPLACE_WITH_API_KEY"        # 必填鉴权
      GATEWAY_API_KEY: "REPLACE_WITH_GATEWAY_KEY"
      TRINITY_STORE: /app/data
    depends_on: [trinity-mcp]
    restart: unless-stopped
  trinity-db:
    image: postgres:16-alpine
    ports: ["127.0.0.1:5430:5432"]   # 只本机
    environment:
      POSTGRES_DB: trinity
      POSTGRES_USER: trinity
      POSTGRES_PASSWORD: "REPLACE_WITH_PG_PASS"
    volumes: [trinity-pgdata:/var/lib/postgresql/data]

> 注意：部署时用 .env 文件（compose 变量）替换占位符，不要硬编码在 yml。

> ⚠️ 服务器权威库建议 **PG**（SQLite 单机文件不适合多机并发写）;
> 服务器 PG 里跑一次 scripts/sqlite_pg_mirror.py 初始化 schema（已存在）。

### 2.2 HTTPS 反代 + 鉴权

- Nginx/Caddy 反代 https://memory.example.com → 127.0.0.1:8001;
- 强制 TRINITY_API_KEY（Bearer）——supervisor 注入已支持;
- 防火墙：公网只放行 443/8001（8000 MCP 仅内网）。

### 2.3 服务器初始化清单

1. docker compose up -d（修正版 compose）;
2. sqlite_pg_mirror.py 建表（若首次）;
3. 设置 TRINITY_API_KEY / GATEWAY_API_KEY / TRINITY_PG_PASSWORD（.env 或 ~/.dsh/.credentials.yaml）;
4. 每日维护链（cron）：mirror, decay, tiers, consolidate, dedup, sync, compact, active-health, backup（复用 dsh-ops 脚本，改 PG 端口为 5430）;
5. 备份：trinity-backup.ps1 逻辑 → 服务器版（PG dump + 卷快照，保留 14 天）。

---

## 三、各电脑部署详细设计

### 3.1 本地实例

| 项 | 说明 |
|---|---|
| 安装 | Python 3.10+；pip install trinity-memory[sdk] 或仓库部署（完整功能用 [api,mcp]） |
| 权威库 | 本地 SQLite（~/.trinity/store/trinity_store.db），WAL 模式 |
| 智能体 | 本地 agent 通过本地 API/SDK 读写（零延迟） |

### 3.2 trinity-sync-agent（新组件，本方案核心）

**部署**：每台电脑一个守护进程；Windows 用计划任务/VBS 自启，Linux 用 systemd。

**配置 sync-agent.yaml**:
server:
  url: https://memory.example.com        # 服务器 API
  api_key: sk-xxx                         # 本机独立 Key
  verify_tls: true
sync:
  interval_seconds: 3                     # 轮询间隔（准实时）
  batch_size: 100                         # 单批 ≤100（API 上限）
  max_per_cycle: 500                      # 每轮最多推送
  direction: local_to_server              # 一期单向；双向二期
  filter:                                 # 可选同步范围
    agents: []                            # 空=全部
    categories: []                        # 空=全部
    min_importance: 0.0
  metadata_tag: { sync_source: "pc-a" }   # 标记来源机器
cursor:
  file: ~/.trinity/sync-agent-cursor.json # 游标持久化
  field: updated_at                       # 增量字段
logging:
  file: ~/.trinity/logs/sync-agent.log
  level: INFO

**游标格式**:
{ "last_updated_at": "2026-08-18T03:00:00.000000+00:00", "last_run": "..." }

**推送请求体（实测 schema）**:
POST /agents/memory/bulk_write
Authorization: Bearer sk-xxx
{ "entries": [
    { "agent_id": "pc-a:agent-1", "content": "...", "category": "context",
      "importance": 0.7, "tags": ["sync"], "metadata": {"sync_source": "pc-a"} }
] }

**运行循环（伪代码）**:
loop:
  rows = SELECT memory_id, content, agent_id, persona_id, importance, tags,
                category, metadata, updated_at
         FROM memories WHERE updated_at > cursor AND status='active'
         ORDER BY updated_at LIMIT batch_size
  if rows:
      POST /agents/memory/bulk_write (Bearer)
      if 200: cursor = max(updated_at); save_cursor()
      else: backoff(2^n, max 60s); retry  # 游标不前进 → 断线续传
  sleep(interval)

**关键语义**:
- **幂等**：服务器 ingest 按 content_hash 去重 → 重复推送/重试无害;
- **agent 标识**：pc-a:agent-1 前缀机器名，服务器按 agent_id 隔离（不冲突）;
- **更新覆盖**：同 agent 同内容更新 → 服务器 updated_at 新者胜;
- **首轮全量**：游标为空 → 从 epoch 开始，按 max_per_cycle 限速分批（或先 federation 全量快照再增量）;
- **断线**：退避重试 + 本地照常工作（同步是旁路）。

---

## 四、同步协议与一致性（对齐 SAMEP 思路）

| 需求 | 实现 |
|---|---|
| 幂等 | content_hash（服务器 ingest 内置） |
| 冲突 | 同 content_hash 不同内容 → updated_at 新者胜（federation 已有策略） |
| 审计 | 服务器审计链自动记录（谁/哪台机器/何时写入） |
| 加密 | HTTPS + Bearer；可选内容列 AES-256-GCM（服务器开启） |
| 顺序 | 每机独立游标按 updated_at 增量，跨机天然并行 |
| 去重 | 服务器 ingest 去重 + sync-agent 游标去重双保险 |
| 限速 | batch_size + max_per_cycle + 退避（防风暴） |

**协议矩阵（Oracle 建议映射）**:
- REST /agents/memory/bulk_write：跨机批量同步（本方案主通道）
- MCP trinity-mcp：服务器对外工具接入（可选）
- A2A：未来 agent↔agent 协作（二期）

---

## 五、数据流与时序

1. **首轮**：本机 cursor 空 → sync-agent 分批推送全部 active 记忆（限速）→ 服务器入库;
2. **日常**：agent 写本地 → 3s 内 sync-agent 检测到 updated_at 变化 → 批推 → 服务器;
3. **统一处理**：服务器检索/治理（decay/tiers/consolidate）对全量（含各机）记忆运行;
4. **断网恢复**：游标停住 → 恢复后续传; 本地 agent 不受影响;
5. **对账（可选）**：每周 federation_sync.py 全量 diff 一次，纠正漏推。

---

## 六、运维设计

| 项 | 设计 |
|---|---|
| 监控 | sync-agent 心跳日志（每轮 sent/err）；服务器 /metrics（已有）+ API 健康 |
| 告警 | sync-agent 连续 N 轮失败 → 本地日志 + 可选通知；服务器监控 PG 连接/磁盘 |
| 日志 | 每机 ~/.trinity/logs/sync-agent.log（轮转）；服务器 dsh-maintenance.log |
| 备份 | 服务器：PG dump + 卷快照（14 天）；各机：本地库日常备份（已有） |
| 扩展 | 加机器=加 sync-agent + Key；服务器 PG 扩容/索引（已有维护链控量） |

---

## 七、安全设计

- 每机独立 API Key；可吊销单机;
- 服务器仅接受 Bearer + 来源标记（metadata.sync_source）;
- 传输 HTTPS；服务器防火墙只放行 443/8001;
- 敏感记忆：filter.min_importance / categories 控制同步范围; 可加内容关键词黑名单（二期）;
- 审计链全程可追溯。

---

## 八、网络方案对照（为什么这样设计）

| 网络方案 | 做法 | 我们采用/改进 |
|---|---|---|
| Mem0 Edge | 边缘 agent 本地缓存 + 远程记忆服务 | 本地 Trinity 全功能实例（更强缓存）+ sync-agent 推送 |
| Mem0 Self-Host | Docker 一键部署 + PG/Qdrant | 服务器 Docker + PG 权威（Qdrant→FAISS 内嵌，无需外置） |
| SAMEP | 跨实例安全交换：加密/审计/幂等/冲突 | content_hash 幂等 + updated_at 冲突 + 审计链（对齐） |
| Multi-Agent 2026 | 隔离记忆 / 共享池模式 | agent_id 隔离 + MemoryAggregator 共享池（原生） |
| 协议矩阵 | REST/MCP/A2A 分层 | REST 同步主通道；MCP/A2A 二期 |

---

## 九、分阶段实施与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 概念验证**（半天） | 本机模拟电脑 B：临时 SQLite 库 + sync-agent → 本机 :8001 | 推送记忆服务器可检索、agent 隔离正确、重复推送幂等、断线续传 |
| P1 服务器部署（1-2 天） | docker 修正版 + PG + 鉴权 + HTTPS 反代 + 每日维护链 | 服务器 API 公网可用、鉴权生效、备份 OK |
| P2 单机试点（1 天） | 一台真机装 sync-agent，跑 1-2 天 | 实时汇聚稳定、日志监控正常 |
| P3 多机推广 | 其余电脑接入 + 对账 + 运维 | 全部机器记忆统一在服务器处理 |

---

## 十、需要开发的组件（工作量）

| 组件 | 工作量 | 说明 |
|---|---|---|
| trinity-sync-agent.py | ~250-300 行 | 游标+批推+退避+日志；复用 requests/SDK |
| sync-agent.yaml 模板 + 自启脚本 | ~50 行 | systemd/VBS 各一 |
| 服务器 docker-compose.server.yml | ~60 行 | 端口/鉴权/卷修正 |
| 部署文档（本文档落地执行版） | 已有 | |

**服务器端零 Python 改造**——纯用现成端点。总开发量约半天到一天。

---

## 十一、决策点（实施前确认）

1. 同步方向：单向（本地→服务器，推荐）还是双向？
2. 实时粒度：3s 轮询（推荐）还是写路径钩子？
3. 服务器环境：公网云（域名+HTTPS）还是内网机？
4. 是否现在执行 **P0 概念验证**（半天，唯一能实测"能不能实行"的步骤）？
