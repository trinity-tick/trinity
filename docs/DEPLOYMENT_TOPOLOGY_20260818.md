# Trinity 部署拓扑厘清 (2026-08-18)

> 目的：消除「两套 Trinity 实例并存」的困惑，防止改错库。
> 结论：**docker 化部署（api/mcp/dash）是独立部署、独立数据卷；
> 唯一共享的是 docker trinity-db :5430（维护库 PG）**。

## 现状两套实例

| 栈 | 进程/容器 | 端口 | 数据存放 | 用途 |
|---|---|---|---|---|
| **原生（权威）** | API (python -m trinity.api.server) | :8001 | SQLite 大库 ~/.trinity/store/trinity_store.db | DSH 插件/engine_worker 的运行时权威 |
| 原生 | MCP SSE（native） | :8000 | 同上 | 对外 MCP |
| 原生 | engine_worker（DSH 插件自管） | - | 同上 | 工具调用后端 |
| 原生 | collector 守护进程 | - | 同上 | 主动采集 |
| 原生 | Gateway（OpenAI/Mem0 兼容层） | :8002 | - | 外部模型兼容 |
| **docker（并存）** | trinity-api | :8005->8100 | volume trinity-data（容器内 /app/data，**独立**） | dockerized REST API |
| docker | trinity-mcp | :8006->8000 | volume trinity-data（**独立**） | dockerized MCP |
| docker | trinity-dash | :3000->8100 | volume trinity-data（**独立**） | Web 管理面板 |
| docker | trinity-db（PG16） | 127.0.0.1:5430->5432 | volume trinity-pgdata | **维护库**：native maintenance 的 mirror/decay/tiers 目标 |
| docker | trinity-telemetry-jaeger | 4317/4318/16686 | - | 链路追踪 |

## 数据隔离结论（2026-08-18 实证）

- docker inspect trinity-api/trinity-mcp/trinity-dash 挂载均为 volume `trinity-data` -> 容器内 /app/data，
  与宿主 ~/.trinity/store/trinity_store.db **无任何 bind/volume 共享**。
- 因此 docker 化 Trinity 的 SQLite 数据在 docker volume 里，与原生权威大库完全隔离，互不读写。
- **唯一共享点**：docker `trinity-db` :5430 被原生 maintenance 脚本（sqlite_pg_mirror / decay / tiers 的 PG 目标）使用，
  连接参数由 ~/.dsh/.credentials.yaml 的 TRINITY_PG_* 决定。

## 「改库前先确认目标」检查清单

1. 要改的库是哪个？
   - 记忆内容/检索/DSH 结构层 -> **SQLite 大库** ~/.trinity/store/trinity_store.db（原生）。
   - 维护扫描/衰减/分层 -> PG :5430（docker trinity-db）。
   - docker dash/API 面板数据 -> docker volume trinity-data（docker exec -it trinity-api sh 查看 /app/data）。
2. 端口对不上时先 netstat / docker ps 确认实例归属：:8001=原生 API；:8005=docker API；:3000=docker dash。
3. 不要用 cwd 下的 trinity_store.db：那是另一个小库（已知坑 #9），一律用绝对路径 ~/.trinity/store/trinity_store.db。

## 是否需要停掉 docker 栈？

- 若不需要 Web 面板/独立 docker API：`docker compose --profile full down`（在 C:/Users/Administrator/trinity 下，
  会同时停 mcp/api/dash/jaeger；不会动 trinity-db 的 pgdata 卷，维护库仍可用）。
- 若保留：按本文件理解即可，两者互不干扰。

## 参考

- docker-compose.yml（仓库根，project=trinity，config_files 指向它）
- EXECUTION.md 第 40 轮（2026-08-18 ops 记录）
