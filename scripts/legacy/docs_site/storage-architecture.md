# Trinity 存储架构决策（M2-2，2026-08-14）

## 现状盘点（实测）

| 存储 | 位置 | 数据量 | 角色 |
|---|---|---|---|
| SQLite（runtime） | `~/.trinity/store/trinity_store.db`（WAL+FTS5） | 11313 条 / active 1449 | MCP/DSH 实时读写、Hermes 同步源 |
| PostgreSQL（batch） | docker `trinity-db`（postgres:16-alpine）`127.0.0.1:5430`，user/pass `trinity/trinity`，库 `trinity` | **0 条（重建后为空）** | decay/tiers 等批处理目标 |
| ~~原生 PG 5432~~ | ~~Windows 服务 postgresql-16 / postgresql-x64-16~~ | 已 **Stopped**；5432 现被 smartcos-postgres（WMS）占用 | 废弃 |

关键事实：
1. 原"PG 模式对齐（6.2 轮，25 列富 schema、1029 条）"针对的是**已被停用的原生 PG**；
2. docker trinity-db 是全新容器（2 小时前重建），memories 表为最小 14 列结构且为空；
3. 维护脚本/凭证仍指向 `5432/postgres/postgres` —— **已失配**，decay/tiers 现会失败；
4. SQLite 是事实上的数据主存储（唯一有真实数据的持久层）。

## 决策：双存储分工 + 单向镜像（不迁移）

**SQLite = 系统记录源（system of record）**，理由：
- MCP/DSH 工具链全部接 SQLite（FTS5 + jieba 中文分词可用、WAL 并发安全）；
- Hermes 双向同步、collector、memory_tiers 批处理读取均以 SQLite 为准；
- 40MB / 11313 条规模下 SQLite 性能充足，零运维成本。

**PostgreSQL = 批处理/分析镜像层**，理由：
- decay 压缩器、tiers 分层按 PG 适配器（PostgreSQLAdapter + 连接池）实现；
- PG 提供多租户/版本追踪的富 schema 扩展空间；
- docker trinity-db 已是部署形态，作为镜像目标零额外成本。

**实现**：`scripts/sqlite_pg_mirror.py` 单向镜像 SQLite active 记忆 → PG
（按 sha256_hash 幂等 upsert，自动补齐 PG schema 对齐），由维护任务周期执行。
双向回写不启用（避免冲突解决复杂度；Hermes 双向同步已覆盖桌面侧）。

**明确不做**：不把 MCP/DSH 迁移到 PG 单存储（需重写 adapter 检索路径，收益低风险高）；
不删除 SQLite（runtime 依赖）。

## 待办衔接

- [x] `scripts/sqlite_pg_mirror.py`（镜像 + 幂等）
- [x] PG 连接参数修正（凭证 → 127.0.0.1:5430 / trinity / trinity）
- [x] 镜像实跑验证（1449 → PG）＋ decay `--dry-run` 走通
- [ ] 维护任务接入 mirror（`trinity-dsh-maintenance.ps1` 增 task）——随全量集成轮
