# 服务器 + 多机 Trinity 实时记忆汇聚方案（SERVER_MULTI_NODE_SYNC）

> 日期：2026-08-18 | 状态：**方案设计（待 P0 验证后实施）**
> 目标：服务器安装 Trinity 作权威汇聚端；每台电脑独立 Trinity 实例；记忆**实时增量同步**到服务器，统一处理（检索/治理/跨机共享）。
> 依据：已核实服务器写端点 schema（/agents/memory/bulk_write、/memories）、SDK（Python/Go/TS）、federation_sync.py、多租户与 agent 隔离。

## 一、架构总览

服务器（权威汇聚端）:
  Trinity API :8001 + PostgreSQL（权威库）
    - 鉴权: TRINITY_API_KEY / GATEWAY_API_KEY
    - 汇聚: 各电脑 agent_id 隔离 + 共享池检索
    - 治理: decay/tiers/consolidate/审计/合规

  电脑 A: Trinity 本地(SQLite 权威) + sync-agent（智能体 1..n）
  电脑 B: Trinity 本地(SQLite 权威) + sync-agent（智能体 m..k）
  同步: 本地 sync-agent → HTTPS + Bearer → 服务器 /agents/memory/bulk_write

**核心思路**：本地实例照常工作（智能体读写本地库、零延迟）；一个轻量 **sync-agent 守护进程**
监视本地库的新增/变更记忆，秒级增量推送到服务器（幂等去重）。服务器统一承载跨机检索、治理与备份。

## 二、同步机制设计（实时增量推送）

### 2.1 模式选择：轮询增量推送（推荐）

| 模式 | 实时性 | 侵入性 | 复杂度 | 结论 |
|---|---|---|---|---|
| 写路径钩子（事务内同步推送） | 严格实时 | 高（改 adapters 写路径） | 高 | 备选 |
| **轮询增量推送**（游标 + 批推） | 秒级（2-5s） | 零（独立进程） | 低 | 推荐 |
| 定时全量联邦（federation_sync.py） | 分钟~小时 | 零 | 低 | 兜底/首轮 |

轮询模式复用 **collector DshEventsSource 的成熟模式**（游标 + 增量 + 断线续传），风险最低。

### 2.2 sync-agent 组件设计（新增，约 250-300 行 Python）

trinity-sync-agent（每台电脑一个守护进程）:
  - 读本地库: SELECT memory_id, content, agent_id, persona_id, importance, tags,
    category, metadata, updated_at FROM memories
    WHERE updated_at > 游标 ORDER BY updated_at LIMIT 200   (游标持久化 json)
  - 批推: POST 服务器 /agents/memory/bulk_write
    { entries: [ {agent_id, content, category, importance, tags, metadata} ] }
    (max 100/批, 已核实 schema)
  - 幂等: 服务器 ingest 按 content_hash 自动去重 → 重复推送无害
  - 断线: 指数退避重试 + 游标不前进 → 恢复后续传
  - 配置: sync-agent.yaml (server_url, api_key, interval=3s, agent_filter, direction)

### 2.3 双向同步（可选，二期）

- 默认**单向：本地 → 服务器**（汇聚，满足"统一处理"）；
- 二期可选**服务器 → 本地回写**（如服务器治理后的知识包/共享记忆分发）——用同一增量协议反向；
- 冲突策略：content_hash 去重 + updated_at newer 保留（复用 federation 已有策略）。

## 三、服务器部署设计

| 项 | 方案 |
|---|---|
| 运行形态 | Docker Compose（trinity-api + trinity-mcp + trinity-db PG + dash）或原生 pip install |
| 权威库 | **PostgreSQL**（多机并发写安全；SQLite 只适合单机） |
| 端口 | 修正 docker-compose：api 8001:8001（现文件 8005:8100 是本机并存栈参数，需改） |
| 鉴权 | TRINITY_API_KEY（API）/ GATEWAY_API_KEY（Gateway）Bearer；防火墙只放行 8001/8000 |
| 传输 | Nginx/Caddy 反代 + HTTPS（服务器公网或内网 IP） |
| 维护 | 每日链（mirror/decay/tiers/consolidate/active-health/backup）照常 |

## 四、各电脑部署设计

| 项 | 方案 |
|---|---|
| 本地实例 | Trinity 原生安装（SQLite 权威，Python 3.10+）；智能体照常本地工作 |
| sync-agent | 脚本分发；systemd（Linux）/计划任务或 VBS（Windows）自启 |
| 网络 | 仅出站 HTTPS 到服务器（无需公网入站，安全） |
| 容量 | 本地库照常增长；服务器按汇聚总量扩容（PG） |

## 五、实时性与一致性

- **实时性**：interval=3s 轮询 → 端到端 ~3-6s 延迟（准实时）；严格实时可二期改写路径钩子；
- **一致性**：content_hash 幂等去重；同内容更新以 updated_at 新者胜；
- **断线**：游标持久化 + 退避重试 + 幂等 → 恢复即续传，不丢不重；
- **顺序**：按 updated_at 增量，批量 <200 条/轮，跨机顺序无关（每机独立游标）。

## 六、安全

- 每台电脑一个 **API Key**（或按 agent 分发子 key）；服务器记录来源（agent_id + metadata.sync_source 机器标识）；
- HTTPS 全程；本地库不暴露公网；
- 服务器审计链自动记录每条同步记忆的写入动作（可追溯哪台机器/哪个 agent）。

## 七、可行性评估（现成 vs 要开发）

| 能力 | 状态 | 说明 |
|---|---|---|
| 服务器部署 | 现成 | docker-compose / 原生；需按服务器修正端口映射 |
| 写端点 | 现成 | /agents/memory/bulk_write（100/批）、/memories——schema 已核实 |
| 去重/隔离/审计 | 现成 | content_hash 去重、agent_id 隔离、多租户、审计链 |
| SDK | 现成 | Python/Go/TS（base_url 指向服务器） |
| 批处理兜底 | 现成 | federation_sync.py（全量/增量快照） |
| **sync-agent** | **要开发** | ~250-300 行 Python，复用 collector 增量模式，风险低 |
| 服务器→本地回写 | 二期 | 可选 |

**结论：可行性高。服务器端零改造（纯用现成端点），唯一新组件是每机的 sync-agent（小、低风险、有成熟模式可循）。**

## 八、实施计划（分阶段，每阶段可验证）

| 阶段 | 内容 | 时长 | 出口标准 |
|---|---|---|---|
| **P0 概念验证** | 本机模拟双实例：临时 SQLite 库（扮演电脑 B）+ sync-agent → 本机 :8001，验证端到端汇聚 + 检索 | 半天 | 推送的记忆能在服务器检索到，agent 隔离正确，重复推送幂等 |
| P1 服务器部署 | 服务器 docker（端口修正 + PG + 鉴权 + HTTPS） | 1-2 天 | 服务器 API 公网/内网可访问，鉴权生效 |
| P2 单机试点 | 一台真实电脑装 sync-agent，跑 1-2 天 | 1 天 | 实时汇聚稳定，日志/监控正常 |
| P3 多机推广 | 其余电脑接入 + 运维（监控/备份/可选回写） | 按需 | 全部机器记忆统一在服务器处理 |

## 九、风险与对策

| 风险 | 对策 |
|---|---|
| 本地库与 sync-agent 并发读锁 | SQLite WAL 多读（已有）；读写分离连接 |
| 推送风暴（大量历史记忆首轮） | 首轮按批次限速（sleep + 分批），或先 federation 全量再增量 |
| 服务器 PG 性能 | 维护链照常（decay/consolidate 控量）；索引 |
| 网络不可达 | 退避重试 + 本地继续工作（同步是旁路，不影响本地智能体） |
| 隐私 | 按 agent/persona 过滤同步范围；敏感记忆不打标不推（metadata 标记） |

## 十、关键决策点（待确认）

1. **同步方向**：仅本地→服务器（汇聚）还是需要双向？——默认单向；
2. **实时粒度**：3s 轮询（推荐）还是写路径钩子（严格实时、侵入）？——推荐轮询；
3. **服务器环境**：公网云（HTTPS+域名）还是内网 NAS/PC？——决定反代与安全配置；
4. **P0 验证时机**：是否现在本机模拟验证（半天）后再上服务器？——建议先 P0。
