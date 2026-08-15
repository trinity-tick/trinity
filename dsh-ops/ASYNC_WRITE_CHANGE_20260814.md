# memory_write 异步化改动记录（2026-08-14）

## 目标
消除 MCP `memory_write` 冷启动阻塞：DSH 会话内写记忆从「同步等 15.3s 嵌入引擎冷启动」变为「1s 内即时返回 + 后台加工」。

## 改动文件（本轮 3 处 patch）

### 1. `trinity/core/client.py`
- `ingest()` 新增参数 `postprocess: bool = True`（默认 True，REST API 等既有调用方行为不变）。
- 把「语义关联 + 实体提取 + 主动推送」从 ingest 同步路径抽成独立方法 `_postprocess_memory(memory_id, content, result=None)`。
- **审计日志前置**：`write_audit_log(action="create")` 移到加工管线之前，保证核心写入 + SHA-256 审计链即时落账（可信链完整）。
- `postprocess=False` 时：跳过加工，返回 `postprocess="pending"`、`pushed_memories=[]`、`extracted_entities=0`。

### 2. `trinity/mcp/tools/memory_tools.py`
- `memory_write` 改调 `engine.ingest(..., postprocess=False)` 即时返回。
- 用 `threading.Thread(target=engine._postprocess_memory, args=(memory_id, content), kwargs={"result": result}, daemon=True).start()` 后台执行加工。

## 实测验证（temp/verify_async_write.py，Hermes venv python 3.11.15）

| 路径 | 耗时 | 结果 |
|------|------|------|
| `postprocess=False` 快速路径 | 1.035s | `postprocess='pending'`, `pushed=[]`, `entities=0` ✅ |
| `_postprocess_memory` 单独执行 | 15.294s | 嵌入引擎冷启动（懒加载）✅ |
| `postprocess=True` 同步（预热后） | 0.515s | 引擎已加载 ✅ |

结论：冷启动根源 = `_auto_link_semantic` 首次调用的嵌入引擎懒加载（15.3s）。异步化后 memory_write 不再被它阻塞。

## 回归测试（pytest）
- `tests/test_core.py + test_mcp.py + test_sqlite_threadsafe.py` → **27 passed, 37 skipped**（13.07s）。skip 均为环境依赖（PG/Redis 未启动），与本改动无关。
- 覆盖：`TestIngest`（默认 `postprocess=True` 行为不变，仍返回 memory_id/version_id/sha256_hash/timestamp）、`test_search_after_ingest`、`test_sqlite_threadsafe`（后台线程写 SQLite 线程安全）全通过。
- 环境坑：`.venv` 未装 pytest；pytest 9.1.1 来自 Hermes venv（会话 PYTHONPATH 污染可见）。跑测试须 `Hermes venv python + PYTHONPATH=<trinity根>`（覆盖污染的同时注入 trinity）。

## 待办（阻塞中）
- [ ] **重启 Trinity 服务加载新代码**：kill PID 37660（mcp :8000）+ 37820（api :8001），用 `.venv/Scripts/python.exe -m trinity.mcp.server --mode sse --port 8000` + `-m trinity.api.server --port 8001` 拉起。→ `Stop-Process -Force` 被系统拦截，等用户授权。
- [ ] 通过真实 MCP 通道调一次 `memory_write` 验证 `postprocess='pending'` 生效。
- [ ] 全链路闭环验证 + 结构化报告。

## 环境要点
- Trinity 服务 python = `C:/Users/Administrator/trinity/.venv/Scripts/python.exe`（Python 3.11.15，依赖齐全）。
- 服务进程：37660 = mcp SSE :8000；37820 = api :8001。
- supervisor = `dsh-ops/trinity-supervisor.ps1`（每 5 分钟探测拉起，有 restart interval 保护）。
