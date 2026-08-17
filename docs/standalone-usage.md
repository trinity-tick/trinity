# Trinity 独立使用指南(可移植性,2026-08-16)

Trinity 是可独立部署的 Python 包(trinity-memory),**不依赖 DeepSeek Harness**。
本指南:脱离 DSH,在任何 Python 3.10+ 机器上使用 Trinity 的核心/REST/MCP。

## 一、安装(三种方式)

1. pip 安装(wheel 已验证可构建):
   ```bash
   pip install trinity-memory            # 已发布则直接装
   # 或本地 wheel:
   pip install dist/trinity_memory-8.2.0-py3-none-any.whl
   # 或开发模式:
   pip install -e .                      # 在 trinity 源码目录
   ```
2. Docker:docker compose up -d(官方 docker/)
3. 源码直接使用:sys.path 指向 trinity 根后 import

## 二、核心引擎(纯 Python,无需服务)

```python
from trinity import Trinity
t = Trinity()  # 默认 ~/.trinity/store/trinity_store.db
t.ingest("这是要记住的内容", agent_id="my-agent")   # 写入(CRDT+SHA256 审计)
t.search("关键词", top_k=5)                        # 47 通道检索
t.diagnostics()                                    # 健康/统计
```

## 三、标准服务接口(任一即可)

| 接口 | 启动 | 用途 |
|---|---|---|
| REST API | trinity-api --port 8001 | 90+ 端点:记忆/检索/审计/市场/A2A |
| MCP(stdio/sse/streamable-http) | trinity-mcp --mode sse --port 8000 | 任意 MCP 客户端 |
| Gateway(OpenAI 兼容) | python gateway/server.py | LLM 应用接入,自动记忆注入 |

## 四、配置环境变量(跨机器部署)

| 变量 | 默认 | 说明 |
|---|---|---|
| TRINITY_STORE | ~/.trinity/store | store 目录(或 .db 文件) |
| TRINITY_HOME | ~/.trinity | 配置/日志/持久化文件根 |
| TRINITY_DB_PATH | (由 TRINITY_STORE 推导) | 库文件路径 |
| TRINITY_LLM_API_KEY / DEEPSEEK_API_KEY | - | LLM 摘要/进化用 |
| GATEWAY_PORT | 8002 | Gateway 端口 |
| UPSTREAM_BASE_URL / UPSTREAM_API_KEY | OpenAI | Gateway 上游 LLM |
| TRINITY_API_KEY | - | API 鉴权(可选) |
| PYTHONUTF8 | - | Windows 中文编码(建议 1) |

## 五、数据迁移(导出/导入)

1. 导出全部记忆为 JSONL(可迁移):
   ```bash
   python scripts/export_all.py 输出路径.jsonl
   ```
2. 每行一个记忆:memory_id/content/agent_id/session_id/importance/tags/category/status/created_at
3. 导入:任意系统按此格式读取,或通过 /agents/memory/write 逐条回写

## 六、最小独立部署(30 秒)

```bash
pip install trinity-memory[api,mcp]        # 或本地 wheel
trinity-api --port 8001 &                  # REST
trinity-mcp --mode sse --port 8000 &       # MCP
# 客户端即可经 REST/MCP 使用;记忆存 ~/.trinity/store/trinity_store.db
```

## 七、与 DSH 的关系

- DSH 集成(dsh-trinity 插件/结构层/自动沉淀)是**附加层**,非核心依赖
- 核心引擎 + REST + MCP + Gateway 完全独立,可在任何机器/任何 agent 框架中使用
- 本机 26 轮稳定性加固(锁修复/看门狗/异常兜底)同样适用于独立部署
