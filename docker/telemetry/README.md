# Trinity Telemetry — OTLP/Jaeger 可视化

本目录用 Docker Compose 起一个独立的 Jaeger all-in-one，接收 Trinity
`trinity.telemetry.tracer` 导出的 OpenTelemetry span，并在 Web UI 中可视化。

- 部署形态：**Jaeger all-in-one 直连**（没有单独的 otel-collector）。
  Trinity 的 tracer 直接把 OTLP/HTTP JSON POST 到 Jaeger 的 `:4318/v1/traces`，
  Jaeger 原生支持该协议，中间再放一个 collector 对本场景没有收益。
  若以后需要缓冲/过滤/多后端，再加 `otel/opentelemetry-collector-contrib`。
- Compose 项目名固定为 `trinity-telemetry`，与运行中的 `trinity-api` /
  `trinity-mcp` / `trinity-db` 等容器**完全隔离**，`down` 不会误伤它们。

## 快速开始

```powershell
# 启动（首次会拉镜像，可能耗时数分钟）
docker compose -p trinity-telemetry -f docker/telemetry/docker-compose.yml up -d

# 状态
docker compose -p trinity-telemetry -f docker/telemetry/docker-compose.yml ps

# 日志
docker compose -p trinity-telemetry -f docker/telemetry/docker-compose.yml logs -f jaeger

# 关闭（容器停止并移除；注意 all-in-one 用内存存储，关闭后 trace 丢失）
docker compose -p trinity-telemetry -f docker/telemetry/docker-compose.yml down
```

启动后打开 **http://127.0.0.1:16686**（Jaeger UI），左侧 Service 下拉选择
`trinity`（或自定义的 `OTEL_SERVICE_NAME`），Find Traces 即可看到 span。

## 端口表

| 端口 | 用途 |
|---|---|
| 16686 | Jaeger Web UI |
| 4317 | OTLP gRPC（标准 otel SDK 走这里） |
| 4318 | OTLP HTTP —— **Trinity tracer 使用的端口**（POST `/v1/traces`） |
| 5778 | Jaeger agent 配置 HTTP |

## 如何验证

```powershell
# 1) UI 可达
Invoke-WebRequest http://127.0.0.1:16686 -UseBasicParsing   # 期望 200

# 2) 产生一个测试 span 并确认到达
& 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe' C:\Users\Administrator\.trinity\otel_verify.py

# 脚本会打印 flush 结果、/api/services 服务列表，并确认 verify.otel span 是否存在。
# 也可以用 Jaeger API 直接查：
Invoke-WebRequest 'http://127.0.0.1:16686/api/services' -UseBasicParsing
Invoke-WebRequest 'http://127.0.0.1:16686/api/traces?service=trinity&limit=20' -UseBasicParsing
```

## 如何让 Trinity 各服务上报遥测

`trinity.telemetry.tracer` 的配置全部来自环境变量（在进程启动时读取）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TRINITY_TELEMETRY_ENABLED` | `1` | `1` 开启 span 采集与导出 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP HTTP 端点（即本 compose 的 4318） |
| `OTEL_SERVICE_NAME` | `trinity` | Jaeger 中的服务名 |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | `512` | 缓冲上限 |
| `OTEL_BSP_SCHEDULE_DELAY_MILLIS` | `5000` | 后台导出线程刷盘间隔(ms) |

让 api / mcp 进程带上遥测：

1. 给进程设置上述环境变量（通常只需确认 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向
   `http://127.0.0.1:4318`，其余用默认值即可）。
2. 重启服务：
   - `trinity-mcp`（SSE :8000）：`python -m trinity.mcp.server --mode sse --port 8000`
   - `trinity-api`（:8001）：`python -m trinity.api.server --port 8001`
   - **supervisor 会自动管理**：`dsh-ops/trinity-supervisor.ps1` 每 5 分钟
     检查一次 api/mcp/collector 并拉起，直接手动重启后 supervisor 也会保持其存活。
3. 在 Jaeger UI 刷新即可看到对应服务名下的 span（写入路径 `memory.write`、
   检索路径 `memory.search` 等）。

## 已知限制

- all-in-one 使用**内存存储**，`docker compose down` 或容器重启会清空历史 trace；
  需要持久化时换成 Elasticsearch / Cassandra 后端。
- 导出线程是 daemon 线程，进程退出时由 `atexit` 注册的 `shutdown()` 兜底刷一次；
  端点不可达时 span 保留在缓冲中并按指数退避重试（上限 60s），不会丢。
- 端口若被占用，改 `docker-compose.yml` 中 `ports` 的宿主机侧端口即可，
  但注意 tracer 的 `OTEL_EXPORTER_OTLP_ENDPOINT` 要指向新的宿主端口。
