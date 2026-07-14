# MCP API 参考

> Trinity 通过 MCP (Model Context Protocol) 对外暴露记忆服务。协议基于 JSON-RPC 2.0，传输层为 gRPC。

---

## 协议概览

```
Endpoint:  grpc://localhost:9091
Protocol:  JSON-RPC 2.0
Version:   MCP 1.0
```

---

## 通用格式

### 请求

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "trinity.<operation>",
  "params": { ... }
}
```

### 成功响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

### 错误响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32000,
    "message": "Memory store unavailable",
    "data": { ... }
  }
}
```

---

## API 方法

### 1. trinity.ingest

注入记忆到系统。

**参数**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 记忆文本内容 |
| `metadata` | object | 否 | 元数据（来源、时间戳、标签等） |
| `persona_id` | string | 否 | 记忆所属的 persona ID |
| `session_id` | string | 否 | 会话 ID（不传则自动生成） |

**请求示例**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "trinity.ingest",
  "params": {
    "content": "用户偏好深色主题，不喜欢自动播放视频",
    "metadata": {
      "source": "chat",
      "timestamp": "2026-07-11T08:00:00Z",
      "tags": ["preference", "ui"]
    },
    "persona_id": "alice_chen",
    "session_id": "sess_001"
  }
}
```

**成功响应**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "memory_id": "mem_abc123",
    "chunk_count": 2,
    "index_status": "indexed",
    "compressed": false
  }
}
```

---

### 2. trinity.retrieve

检索相关记忆。

**参数**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 检索查询 |
| `top_k` | integer | 否 | 返回数量（默认 5，最大 100） |
| `persona_id` | string | 否 | 限制检索范围到指定 persona |
| `time_range` | object | 否 | 时间范围 `{start, end}` |
| `rerank` | boolean | 否 | 是否启用 LLM reranker（默认 false） |

**请求示例**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "trinity.retrieve",
  "params": {
    "query": "用户喜欢什么主题？",
    "top_k": 5,
    "persona_id": "alice_chen",
    "rerank": true
  }
}
```

**成功响应**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "results": [
      {
        "memory_id": "mem_abc123",
        "content": "用户偏好深色主题，不喜欢自动播放视频",
        "score": 0.934,
        "metadata": {
          "source": "chat",
          "timestamp": "2026-07-11T08:00:00Z"
        }
      }
    ],
    "total_hits": 15,
    "latency_ms": 42
  }
}
```

---

### 3. trinity.delete

删除记忆。

**参数**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `memory_id` | string | 是 | 要删除的记忆 ID |

**请求示例**

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "trinity.delete",
  "params": {
    "memory_id": "mem_abc123"
  }
}
```

---

### 4. trinity.benchmark

触发评测（仅开发/调试模式）。

**参数**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `benchmark_name` | string | 是 | 评测名称（如 `longmemeval`） |
| `config` | object | 否 | 评测配置覆盖 |

**请求示例**

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "trinity.benchmark",
  "params": {
    "benchmark_name": "longmemeval",
    "config": {
      "dataset": "mock",
      "top_k": 10,
      "rerank": false
    }
  }
}
```

---

### 5. trinity.health

健康检查。

**请求示例**

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "trinity.health",
  "params": {}
}
```

**成功响应**

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "status": "healthy",
    "components": {
      "second_brain": { "status": "up", "version": "6.16", "modules": 107 },
      "auto_daemon": { "status": "up", "version": "1.8.0", "layers": 8 },
      "chromadb": { "status": "up", "version": "6.15", "modules": 38 }
    },
    "uptime_seconds": 86400,
    "memory_usage_mb": 2048
  }
}
```

---

## 错误码

| 错误码 | 含义 |
|--------|------|
| -32000 | 内部错误 |
| -32001 | 记忆不存在 |
| -32002 | 索引不可用 |
| -32003 | 请求被 auto_daemon 拦截 |
| -32004 | 存储空间不足 |
| -32005 | 评测配置无效 |
| -32600 | 无效请求 (JSON-RPC) |
| -32601 | 方法不存在 |
| -32602 | 参数无效 |
| -32603 | 内部 JSON-RPC 错误 |

---

## Python SDK 示例

```python
from trinity import TrinityClient

# 连接 Trinity 服务
client = TrinityClient(host="localhost", port=9091)

# 注入记忆
client.ingest(
    content="用户偏好深色主题",
    persona_id="alice_chen",
    metadata={"source": "chat", "tags": ["preference"]}
)

# 检索记忆
results = client.retrieve(
    query="用户喜欢什么主题？",
    top_k=5,
    rerank=True
)
for r in results:
    print(f"[{r.score:.3f}] {r.content}")

# 健康检查
health = client.health()
print(health)
```
