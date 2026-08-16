# Trinity 代码审计报告(定向工程审计)

- 日期:2026-08-16
- 范围:trinity/ 全部 526 个 Python 文件 / 244,902 行(按问题模式定向审计,非全面梳理)
- 方法:1.内存态状态 2.路径语义 3.写路径回滚 4.冗余/死代码
- 原则:本报告只标记不修改;修复按优先级另议

---

## 一、规模全景

| 模块 | 行数 | 真实使用? | 说明 |
|---|---|---|---|
| modules/(第二大脑) | 162,499(66%) | 少量 | 50 级 guardian/122 引擎,多数为展示模块,仅 CB47-CB57 部分被引擎初始化 |
| agents/ | 9,299 | 部分 | aggregator(MemoryAggregator 池在用)+ 大量演示/自测 |
| adapters/ | 6,779 | 核心 | sqlite(权威)/postgresql/基础接口 |
| core/ | 5,630 | 核心 | client.py(引擎入口) |
| api/ | 4,955 | 核心 | server.py 90+ 端点 |
| a2a/ | 4,765 | 部分 | capability_registry/task_manager 在用 |
| market/ | 1,664 | 部分 | orderbook 在用(已持久化) |
| evolution/ | 3,159 | 部分 | 双机制(见冗余) |
| 其余(identity/retrieval/vector/audit/mcp/daemon 等) | ~30,000 | 混合 | 按需 |

## 二、发现的问题(按严重度)

### 中风险(与已修同类,建议修复)

1. ReputationEngine / TrustExchange 内存态且被 API 使用
   - 位置:market/reputation.py、market/trust_exchange.py;api/server.py L2628-2658
   - 问题:惰性单例(内存 dict),API 重启后信誉分/信任交换状态丢失
   - 影响:market/endorse、market/report、market/transactions 等端点状态无法跨重启累积
   - 建议:仿 orderbook.py 加 JSON/DB 持久化(注册数小,JSON 即可)

2. agent_registry 压测残留治理
   - 已清 10 条 loop-*,但无 TTL 清理机制(卡片 ttl_seconds=86400,过期未清理)
   - 建议:维护链加过期卡片清理任务(简单 SQL)

### 低风险(当前一致,标记观察)

3. engine_worker.py L351 硬编码库路径
   - 硬编码 ~/.trinity/store/trinity_store.db,不走 TRINITY_STORE env
   - 当前凭证 TRINITY_STORE 恰好等于该路径,一致;若未来改 TRINITY_STORE 会失配
   - 建议:改为复用 core/client.py 的 _find_trinity_store 解析

4. session_recorder.py 写 6 commit 4 rollback 0
   - 异常时可能悬挂写事务(与 sqlite.py 修复前同类,但 recorder 只写会话数据,风险低)
   - 建议:视使用频率决定是否加 _safe_write 装饰器

5. postgresql.py 写 24 commit 25 rollback 0 / postgres_backend.py 写 3 commit 0
   - PG 连接断开会自动回滚未提交事务(与 SQLite 不同),风险低于 sqlite;但仍建议写路径 try/except rollback 保持一致
   - 当前 PG 仅作维护镜像(非权威),风险可控

### 冗余/死代码(标记,不删除)

6. OrderBook 双份:market/orderbook.py(L34,API 在用,已持久化)+ market/trade_protocol.py(L154,疑似备用,仅 self_test 导出)
7. 进化双机制:MetaEvolution(evolution/core.py,maintenance 脚本用,状态存文件)+ EvolutionScheduler(evolution_scheduler.py,API 用,分析器内存+维护链 feed 补偿)—— 两套独立、数据不互通
8. agent 注册三处实现:a2a/capability_registry(调用)+ adapters/sqlite.py + adapters/postgresql.py + adapters/base.py(接口)—— 正常适配器模式,非冗余
9. modules/ 16 万行展示代码:多数从未被真实调用路径引用(仅引擎初始化打印),构成认知负担而非运行负担

## 三、本会话已修复基线(9 项,均已验证)

| # | 修复 | 类型 |
|---|---|---|
| 1 | engine_worker stdin UTF-8(Windows cp936) | 编码 |
| 2 | dsh-trinity 插件 PYTHONUTF8 env | 编码 |
| 3 | sqlite.py _safe_write 写路径回滚 | 锁根因 |
| 4 | 锁看门狗 | 自愈 |
| 5 | structure_store TRINITY_STORE 路径语义 | 路径 |
| 6 | evolution 统计持久化 | 持久化 |
| 7 | A2A CapabilityRegistry 启动加载 | 持久化 |
| 8 | OrderBook JSON 持久化 | 持久化 |
| 9 | auto_session_summary 自动沉淀 + 维护链 | 功能 |

## 四、结构地图(运维参考)

[真实使用核心] api/server.py + core/client.py + adapters/sqlite.py + structure_store.py
  - 检索:adapter search_memories(FTS5+LIKE)+ retrieval/ann_index(落盘)
  - 沉淀:engine_worker(worker 侧)+ auto_session_summary.py(维护链)
  - 进化:EvolutionScheduler(API)+ MetaEvolution(维护链,双轨)
  - 市场:orderbook(已持久化)+ reputation/trust_exchange(待持久化)
  - A2A:capability_registry(已持久化)+ task_manager(DB 持久)
  - 身份:identity_manager(identity_anchor 表持久)

[展示/备用层] modules/second_brain(16 万行)、market/trade_protocol、agents/ 演示

## 五、建议优先级(若执行修复)

1. P1:ReputationEngine/TrustExchange 持久化(与 orderbook 同类,API 在用)
2. P2:agent_registry 过期卡片 TTL 清理任务(维护链)
3. P3:engine_worker 路径改权威解析(防未来失配)
4. P4:冗余清理(仅当确认 trade_protocol/展示模块无引用时,谨慎删除)

## 六、结论

- 核心路径工程质量已通过本会话 9 项修复达到稳定;审计发现 1 项中风险(内存态 reputation/trust_exchange)与 orderbook 修复前同类
- 其余为低风险/冗余标记,不影响当前运行
- 建议:优先做 P1(P1 约 30 分钟,复用 orderbook 的 JSON 持久化模式),P2-P4 按需
