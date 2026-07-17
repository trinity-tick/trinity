# Trinity Evolution System — 自我进化 + 跨平台运行

## 架构总览

Trinity v6.37 在原有三位一体架构（second_brain × auto_daemon × chromadb）基础上，新增了**第四个支柱**——自我进化。

```
┌─────────────────────────────────────────────────────────┐
│               Trinity Evolution Engine                    │
├─────────────────────────────────────────────────────────┤
│  Observe → Analyze → Plan → Execute → Certify          │
│  (M112统计证书)  (M113课程编排)  (M114睡眠巩固)         │
├─────────────────────────────────────────────────────────┤
│  self-improving/  skill_system.py                       │
│  ~/.trinity/      serialization.py                      │
├─────────────────────────────────────────────────────────┤
│  cross_platform.py   ↔   其他Agent/窗口                  │
│  mcp_adapter.py      ↔   MCP协议 (Coze/Dify/Goose)      │
└─────────────────────────────────────────────────────────┘
```

## 系统组件

### `evolution/core.py`
核心循环引擎：`MetaEvolution.tick()` 每次执行 1/5 周期，
5 tick = 1 完整进化周期。状态自动持久化到 `~/.trinity/evolution_state.json`。

### `evolution/skill_system.py`
桥接 `self-improving/` 技能目录：
- memory.md → 偏好/模式记录
- corrections.md → 修正日志
- projects/*.md → 项目记忆

### `evolution/serialization.py`
JSON 序列化/反序列化，支持：
- 命名快照 save/load
- 跨平台最小导出 export_for_cross_platform()

### `evolution/cross_platform.py`
跨窗口/跨Agent手递手协议：
- JSON handoff 文件
- 平台无关格式
- 不依赖特定运行时

### `evolution/mcp_adapter.py`
MCP 工具/资源定义生成：
- evolution_tick, evolution_diagnostics
- evolution_save_state, evolution_prepare_handoff
- evolution://state, evolution://diagnostics

## 跨窗口运行机制

### 状态交换协议

```
窗口 A (Goose)                   窗口 B (Claude Code)
    │                                  │
    ├── save_state() ──→ evolution.json
    │                    (持久状态)      ├── load_state()
    │                                  │
    ├── prepare_handoff() ──→ handoff.json
    │                       (最小切换)   ├── read_handoff()
    │                                  │
    └── (MCP server) ←─── MCP ────→  └── (MCP client)
```

### 在其他 Agent 上运行

所需文件（自包含）：
```
trinity_state.json          ← 序列化状态
instructions.md             ← 运行说明
```

## 使用示例

```python
from trinity.evolution import MetaEvolution

# 启动进化引擎（自动恢复状态）
evo = MetaEvolution()

# 执行一次 tick
result = evo.tick({"session": "current_context"})

# 跨窗口交接
from trinity.evolution import CrossPlatformAdapter
cpa = CrossPlatformAdapter()
handoff_path = cpa.prepare_handoff(evo.diagnostics())
# → 另一个窗口读取 handoff_path 即可继续

# MCP 暴露
from trinity.evolution.mcp_adapter import create_mcp_tools
tools = create_mcp_tools(evo)  # 返回 MCP 工具定义
```
