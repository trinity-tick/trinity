# Trinity MCP 发布清单（2026-08-15, V2 动作 A）

> 目标：把 Trinity 打包为可安装的 MCP server（`trinity-mcp`），让 Claude/IDE 等
> MCP 生态可直接接入——记忆护城河入场券的关键一环。

## 一、现状（已验证）

- `pyproject.toml` 已含 3 个 CLI 入口：`trinity` / `trinity-mcp` / `trinity-api`
- `mcp` extra 依赖已声明（`mcp>=1.0.0`，本机已装）
- MCP server 支持三种 transport：`stdio` / `sse` / `streamable-http`(v2)
  ```
  trinity-mcp --mode stdio
  trinity-mcp --mode sse --port 8000 --host 127.0.0.1
  trinity-mcp --mode streamable-http --port 8003
  ```
- 工具集：8 个 MCP 工具（memory_search/write/update/delete/audit_query/
  chronicle/tag_search/diagnostics）+ 结构层（trajectory/sessions/stats/goals/schedules）

## 二、发布步骤

### 1. PyPI 发布（Python 包）

```bash
# 准备
python -m pip install build twine
python -m build

# 检查
twine check dist/*

# 发布
twine upload dist/*
# 安装验证
pip install trinity-memory[mcp]
trinity-mcp --mode stdio
```

### 2. MCP 生态接入

**Claude Desktop**（claude_desktop_config.json）:
```json
{
  "mcpServers": {
    "trinity": {
      "command": "trinity-mcp",
      "args": ["--mode", "stdio"]
    }
  }
}
```

**通用 MCP 客户端**（SSE 模式）:
```
http://127.0.0.1:8000/sse
```

### 3. README 宣传位（已就绪）

- README 已加"记忆可迁移"章节 + MCP 服务表
- 发布后更新 README 的安装命令为 `pip install trinity-memory[mcp]`

## 三、发布前检查清单

| 项 | 状态 |
|---|---|
| CLI 入口可导入 | ✅ 已验证 |
| mcp 依赖声明 | ✅ |
| stdio/SSE/v2 三模式 | ✅ 运行中 |
| 工具集完整（8+6 结构层） | ✅ |
| 版本号一致（8.2.0） | ✅ pyproject/__init__ |
| README 名实一致 | ✅ 已重写 |
| 官方基准数字 | ⏳ HF 阻塞（发布后可补） |

## 四、注意事项

1. **首次发布前**：确认 `trinity_memory.egg-info` 不打包（已在 .gitignore）
2. **依赖收敛**：`mcp` extra 只带 mcp；api 依赖放 `api` extra（避免安装重）
3. **版本策略**：与 git tag 对齐（`v8.2.0` → PyPI `8.2.0`）
4. **发布后**：MCP server 作为"记忆入口"，配合记忆可迁移标准（memory_portability.py）
   形成"记忆可进可出"的完整闭环
