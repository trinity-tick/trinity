---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_f2490152985111f1a98a525400f8a581
    ReservedCode1: s4h2xzxqTuwcay7DTRuFtu4phFuiSVf/mM8gH5eu89IVbZlmu3d1Y4jh6R2u6FfZYSdJ72CDuE8R7utvYRxFroyuCYBRxzWfB8tYaDrNalxTLeXypaE5wvoBGFBrkNYG+KVqOxgrql3tQKd+uNi4jPOK20KI1nfFciklA97NS1ZZWD/i1M8M9ETYoWU=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_f2490152985111f1a98a525400f8a581
    ReservedCode2: s4h2xzxqTuwcay7DTRuFtu4phFuiSVf/mM8gH5eu89IVbZlmu3d1Y4jh6R2u6FfZYSdJ72CDuE8R7utvYRxFroyuCYBRxzWfB8tYaDrNalxTLeXypaE5wvoBGFBrkNYG+KVqOxgrql3tQKd+uNi4jPOK20KI1nfFciklA97NS1ZZWD/i1M8M9ETYoWU=
---

# Trinity 源码补丁清单（DSH 集成维护）

> 本文件汇总 DSH×Trinity 集成过程中对 Trinity 源码的全部修改，用于升级/回滚时核对，防止被上游覆盖。
> 来源：`dsh-ops/EXECUTION.md`、`dsh-ops/ASYNC_WRITE_CHANGE_20260814.md`。
> 生成时间：2026-08-15

## 补丁总览

| # | 文件 | 改动内容 | 记录来源 |
|---|------|----------|----------|
| 1 | `trinity/telemetry/tracer.py` | 修复遥测死代码：启动后台导出线程（daemon，指数退避 5s→60s）；`flush_to_jaeger` 失败时保留 span（原为 `pass` 静默丢弃）；新增 `shutdown()`；`get_tracer()` 注册 atexit flush | EXECUTION.md §C |
| 2 | `trinity/collector/__main__.py` | `_is_process_alive` Windows 分支改用 ctypes `OpenProcess/GetExitCodeProcess`（不再 spawn tasklist、不依赖输出格式），异常回退原 tasklist 方案 | EXECUTION.md §C |
| 3 | `trinity/collector/daemon.py` | 启动时把项目根注入 `sys.path`（守护进程以脚本方式拉起，cwd 不进 path，此前会解析到 site-packages 旧版 trinity 而崩溃） | EXECUTION.md §C |
| 4 | `trinity/evolution/__init__.py` | 导出 `MetaEvolution / EvolutionCycle / EvolutionPhase / EvolutionState`（此前缺失，导致 `from trinity.evolution import MetaEvolution` 报 ImportError） | EXECUTION.md §C |
| 5 | `trinity/adapters/postgresql.py` | 移除 13 处 `%s::uuid` 强转（`= %s` / `ANY(%s::text[])`）——兼容 varchar 与 uuid 两种列型 | EXECUTION.md §D |
| 6 | `trinity/daemon/memory_compressor.py` | 归档 SQL `memory_id = %s::uuid` → `memory_id::text = %s` | EXECUTION.md §D |
| 7 | `scripts/run_decay_compress.py` | `CompressionStatus.SUCCESS` 引用修复（该名只在 main() 局部绑定，批处理函数内 NameError）→ `result.status.name == "SUCCESS"` | EXECUTION.md §D |
| 8 | `trinity/api/server.py` | ① `request_logging_middleware` 增加 `api.request` trace span（method/path/status/elapsed_ms）；② 接入此前未挂载的 GraphQL（`trinity/api/graphql_schema.py`，strawberry） | EXECUTION.md §E/§F |
| 9 | `trinity/mcp/tools/memory_tools.py` | ① 新增 async 版 `_trace_span` 辅助（同步 `@traced` 不适用于 async 工具），包裹 `memory_search`/`memory_write`；② 后续 6 个工具（`memory_update`/`memory_delete`/`audit_query`/`trinity_diagnostics`/`memory_chronicle`/`memory_tag_search`）用 `_traced_tool` 全量埋点；③ `memory_write` 改调 `engine.ingest(..., postprocess=False)` 即时返回 + 后台线程执行 `_postprocess_memory` | EXECUTION.md §E/§F + ASYNC_WRITE_CHANGE |
| 10 | `trinity/core/client.py` | `ingest()` 新增参数 `postprocess: bool = True`；把「语义关联 + 实体提取 + 主动推送」抽成独立方法 `_postprocess_memory()`；审计日志前置（`write_audit_log(action="create")` 移到加工管线之前） | ASYNC_WRITE_CHANGE_20260814 |

## 回滚命令

```powershell
# 回滚全部补丁（git 还原）
git -C C:\Users\Administrator\trinity checkout -- `
  trinity/telemetry/tracer.py `
  trinity/collector/__main__.py `
  trinity/collector/daemon.py `
  trinity/evolution/__init__.py `
  trinity/adapters/postgresql.py `
  trinity/daemon/memory_compressor.py `
  trinity/api/server.py `
  trinity/mcp/tools/memory_tools.py `
  trinity/core/client.py `
  scripts/run_decay_compress.py
```

## 升级注意事项

- 升级 Trinity 前先 `git stash` 或备份以上文件，升级后重新应用本清单。
- 补丁 9/10（memory_write 异步化）依赖 `trinity/core/client.py` 的 `postprocess` 参数，若上游重构 `ingest()` 需同步适配。
- 补丁 8 的 GraphQL 接入依赖 `strawberry` 依赖，升级后需验证 `trinity/api/server.py` 可正常导入。
*（内容由AI生成，仅供参考）*
