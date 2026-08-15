# Trinity × DSH 融合现状（2026-08-15 实测）

## 一、融合架构（双通道）

```
DSH 会话 (web profile)
 ├─ 原生通道：@deepseek-ai/dsh-trinity 插件
 │    spawn trinity/engine_worker.py (stdio NDJSON) → 注册 trinity_* 原生工具
 │    + 会话结构订阅：session/created|event|flush → structure_sync 批量写引擎库
 │    + 身份融合 F4：agent_id = dsh-<sessionId>
 └─ MCP 桥：dsh-mcp-client → trinity-mcp (stdio) → mcp__trinity__* 工具
      （与原生并存，F5 阶段移除）
```

## 二、实测状态（2026-08-15 14:11）

| 层 | 状态 | 证据 |
|---|---|---|
| 引擎连通 | ✅ | trinity_ping pong / v8.2.0 |
| 原生工具 | ✅ | trinity_structure_stats/sessions/trajectory 全部返回（round34 schema 修复后） |
| 身份融合 F4 | ✅ | dsh_sessions 2 行；本会话 agent_id=dsh-session-704f1dde |
| 结构同步 | ✅ 实时 | dsh_events **2,414 条**（assistant 723 / tool/call 798 / tool/result 804 / user 28 / turn 20 / todo 11 / header 4）；trinity_trajectory 可见**当前 turn 的事件**（含 usage：input/output/cacheRead 624k tokens） |
| 结构层查询 | ✅ | trinity_structure_stats（2 sessions/2408 events/10 todos/4 headers）、trinity_sessions、trinity_trajectory（按会话回放） |
| Todo/Header | ✅ | dsh_todos 10 / dsh_headers 4（todo/write、request/header 事件已同步） |

## 三、缺口与观察

1. **goal/schedule 未融入结构流**：dsh_goals=0、dsh_schedules=0、trinity_goals 空。
   插件 `toStructureEvent` 只映射 user/assistant/tool/turn/todo/header 事件；
   dsh_events 的 event_types 中**无 goal/schedule 类型**——DSH 侧当前不 emit 这两类
   结构事件（原生 create_goal/schedule_create 不进流）。→ 需 DSH 侧事件源补发
   goal/write、schedule/create 事件，插件再加映射；trinity_goal/trinity_schedule
   工具是显式写入通道（可用但非自动）。
2. **双通道并存**：原生 trinity_* 与 mcp__trinity__* 功能重叠（F5 计划移除 MCP）。
   两者检索语义不同（原生默认按会话身份隔离，MCP 全局）——round35 已文档化。
3. **两套 session 概念**：引擎 `sessions` 表（persona 会话，0 行）与 `dsh_sessions`
   （DSH 结构会话，2 行）独立；结构层查询走 dsh_*。
4. **事件量**：2,414 条/2 会话（约 1.2k/会话，含 tool 细节）——增长可接受；
   超长会话可考虑聚合（结构层 compaction）。

## 四、建议

- 短期：goal/schedule 自动同步（待 DSH 事件源支持）；结构层工具已满足审计/回放。
- 中期：F5 移除 MCP 双通道冗余（原生已覆盖）；trajectory 可加 goal 时间线视图。
- 长期：结构层 compaction（会话事件聚合为摘要，控制 dsh_events 增长）。
