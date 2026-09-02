# PG 单写主迁移计划（2026-09-01 起草）

> 目标：消除"三套真相"的最后结构性残余——SQLite 仍是若干组件的写路径。
> 前置条件（已完成）：对账工具 reconcile、PG→SQLite 反向回填 pg-backfill（日链）、
> 哈希 100% 一致实证、聚合池/API 重启流程验证（含提权通道）。

## 现状写路径清单

| 写路径 | 组件 | 当前目标 | 迁移动作 |
|---|---|---|---|
| 记忆写入 | api/gateway/MCP（TRINITY_STORAGE_BACKEND=postgresql） | PG | 无改动 |
| 记忆写入 | engine_worker（DSH 插件） | SQLite（TRINITY_STORE） | worker 改走 PG 适配器或经 API 写入 |
| 记忆写入 | 维护链脚本（decay/consolidate/sleep/self-reflect…） | SQLite | 脚本统一走 PG 适配器（TRINITY_STORAGE_BACKEND 已支持） |
| 结构层 | structure_sync（dsh_* 表） | SQLite | 迁移到 PG 结构表或保持 SQLite 只读镜像 + 每日 pg-sync |
| 聚合池 | MemoryAggregator | aggregator_pool.json | 池改从 PG 派生（API 启动时重建） |

## 迁移步骤（建议独立一轮，每步可回滚）

1. 冻结写（维护窗口）：停 api/mcp/gateway/collector + 提权终止（见 RUNBOOKS.md 提权通道）
2. PG 全量对账（reconcile）→ 确认 pg_only≈0
3. 切 engine_worker 存储后端（改插件 config 或 worker 内 TRINITY_STORAGE_BACKEND=postgresql）→ 冒烟（plugin-smoke）
4. 切维护链脚本（ps1 中为各任务统一注入 TRINITY_STORAGE_BACKEND=postgresql）→ 全链 dry-run
5. SQLite 降级为只读派生镜像：日链保留 pg-sync(SQLite→PG) 反转为 PG→SQLite 生成镜像；reconcile 保留为告警
6. 聚合池派生化：API 启动从 PG 重建池（替代快照加载）
7. 观察两周：水位/对账/质量门禁/backup 全部绿后，冻结 SQLite 写入口

## 风险与兜底
- 每步前跑 backup（trinity-backup.ps1，SQLite+PG 双份）
- 回滚 = 环境变量回切（无代码回滚）
- 迁移期间日链可能 FAIL 的任务：全链观察，reconcile 兜底

## 验收
- reconcile: pg_only≈0 持续 7 天
- quality-gate PASS 连续 2 周
- 单写主后写锁/镜像/漂移类 bug 归零（预期）

## 执行进度（2026-09-01 更新）

- ✅ 步骤 1-2（对账+冻结准备）：reconcile/backfill 上线并日链化；pg_only=59 残留（空内容边界行）
- ✅ 步骤 3（worker 切 PG）已落地：dsh-trinity 插件 spawn env 增加 TRINITY_STORAGE_BACKEND=postgresql
  （dsh-plugin/dsh-trinity/lib/index.js:89）；web 重启后实测 headless trinity_write → PG 命中 1 / SQLite 0
  （写入分离生效；SQLite 由每日 pg-backfill 派生为镜像）
- ✅ 步骤 4（维护链脚本后端感知）已落地（2026-09-01 下午）：
  * decay/tiers wrapper 翻转 --store pg + 显式 --host 127.0.0.1/--port/--user/--password
    （localhost→::1 被 pg_hba 拒——已知坑#1 复现；实跑验证：decay Scan 2000 OK、tiers
    Evictions 121 OK，均 Disconnected from PostgreSQL）
  * restore_high_value 加 PG 路径（RESTORE_BACKEND=pg，16/16 恢复至主存储；PG content 为
    密文 bytea 不可读——只操作元数据；audit_log.details 为 jsonb 需 ::text cast；autocommit 下
    execute 返回 None 取 cur.rowcount）
  * active_set_health 加 PG 读路径（TRINITY_STORAGE_BACKEND=postgresql）
  * backfill_sqlite_from_pg 升级为全量镜像（insert + 状态对齐；不做内容覆盖——PG 密文不可回灌；
    日链顺序 pg-backfill → mirror → decay/tiers → consolidate → dedup → pg-sync 回灌）
  * consolidate/sleep_consolidation 保持 SQLite（镜像暂存 + pg-sync 回灌设计）
- ⏳ 步骤 5-6（SQLite 降级/池派生化）：SQLite 已事实降级为派生镜像（仅 consolidate 等暂存写入 +
  结构层）；池派生化待 API 启动从 PG 重建（当前池 14,677 已稳态）
- ✅ 残留清理（2026-09-01 晚）：hash_mismatch=1 已定位为 PG schema 初始化 SQL 的 sha 常量 bug
  （b4b11a0f 系统行），已修数据 + 修复初始化 SQL（现哈希完整内容）→ hash_mismatch=0；
  53 条 pg_only 实为内容重复行（SQLite 下有规范副本，UNIQUE 正确拦截）；2 条双胞胎同上
- 步骤 5-6 状态：SQLite 事实降级为派生镜像；池派生化待 API 启动从 PG 重建（池 14,677 稳态，暂不改）
- 混沌演练（2026-09-01 完成）：worker 杀→MCP ping 自愈 RECOVERED；collector 杀→supervisor 拉起；
  PG 停→supervisor Start-Service 失败（非提权）→ pg_ctl fallback 拉起成功（服务状态 Stopped，
  下次重启服务 Auto 自动恢复；D:\trinity-data\pgdata 为 2026-09 迁移废弃 initdb 残留，未使用）
- 记忆市场试点（2026-09-01 完成）：estimate/list/orderbook/search/delist 全链路 OK（挂单 ast_ 已撤）
- 发现：supervisor 恢复 PG 服务托管需提权（Start-Service 非提权必败）——建议后续给 supervisor
  加提权助手（RunAs 通道，见 RUNBOOKS.md）或接受 pg_ctl fallback 兜底
