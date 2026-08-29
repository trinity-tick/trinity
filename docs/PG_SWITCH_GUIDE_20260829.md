# PG 主存储正式切换指南（2026-08-29）

## 现状

- PG 便携版 18.6（~/.trinity/pgdata，autovacuum=off）在 5432 运行——11,632 条
  全量迁移 + 包装层全模式打通（keyword/hybrid/reason/write+read/审计链）；
- SQLite 仍为默认运行时权威库（加密/FTS/维护链依赖）——**PG 为完整可选主存储**。

## 切换方式（三选一）

### A. 单进程切换（测试用）

```bash
TRINITY_PG_URL=postgresql://trinity:trinity@127.0.0.1:5432/trinity \
  python -c "from trinity import Trinity; m=Trinity(adapter='postgresql'); print(m.search('Trinity', mode='keyword'))"
```

### B. 服务级切换（正式）

1. supervisor/autostart 启动环境加 `TRINITY_PG_URL`（autostart 已含 PG 确保块）；
2. API 进程以 `TRINITY_STORAGE_BACKEND=postgresql` 启动（或配置层 adapter 默认）；
3. 验证：/health + 检索 + 写入回读。

### C. 双写镜像（推荐过渡）

- 维护链新增 PG 镜像任务（已有 federation 同步机制可复用——SQLite→PG 每日同步）；
- 观察 2 周后切默认。

## 回滚方案

- **切换后发现问题**：env 去掉 TRINITY_PG_URL/STORAGE_BACKEND → 重启即回 SQLite
  （SQLite 数据从未被破坏——PG 是增量副本）；
- **PG 数据异常**：SQLite 全量重迁移（migrate_sqlite_to_pg.py——4.3s/11,632 条）；
- **PG 进程崩溃**：autostart 自动拉起（已有 ensure 块）；拉不起则 SQLite 无缝回退。

## 验证清单（切换后）

1. `/health` ok（API 用 PG）；
2. keyword/hybrid/reason 检索命中；
3. 写入→回读一致；
4. 审计链可追溯（audit_log 链式哈希）。

## 边界

- hybrid full（5 通道）对 PG 降级为 light（BM25 未索引 PG——any-word ILIKE 替代）；
- 向量/图谱通道仍 SQLite 侧（后续可 PG vector 扩展）。

*生成 2026-08-29*
