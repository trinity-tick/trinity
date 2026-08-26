# Trinity × 主流 Agent 框架接入指南（2026-08-24）

> 对齐 2026 生态共识：AGENTS.md 管静态项目知识、MCP 管动态运行时记忆、
> OpenAI/Mem0 兼容 REST 管程序化接入。Trinity 三种形态全部具备——
> 本文给出接 LangGraph / LlamaIndex / OpenAI Agents SDK 的**最小示例**。
> 依据：docs/OPTIMIZATION_ANALYSIS_ROUND7.md（R7 生态连接层调研）。

---

## 一、接入形态总览

| 形态 | 端点/协议 | 适用 |
|---|---|---|
| **MCP**（推荐） | stdio / SSE :8000 / streamable-http :8003 | Claude Code / Cursor / Dify 等 MCP 客户端 |
| **OpenAI 兼容** | Gateway :8002 `/v1/chat/completions` + `/v1/memories` | 任意 OpenAI SDK（记忆自动注入 + __memory_write__ 指令） |
| **Mem0 兼容** | Gateway :8002 `/v1/memories`（id/memory 字段） | Mem0Memory 类适配器（LlamaIndex/OpenAI SDK） |
| **REST 原生** | API :8001（146 端点） | 深度集成（检索/治理/图谱/审计全能力） |

---

## 二、LangGraph 接入（cross-thread store / 工具注入）

LangGraph 的记忆分两条路线：**checkpointer**（线程执行状态，勿重写）与
**cross-thread store**（跨线程长期记忆）。Trinity 走**工具注入**——在
节点里调用 MCP/OpenAI 兼容即可，无需改 checkpointer。

```python
# 最小示例：在 LangGraph 节点中注入 Trinity 记忆工具
from langgraph.graph import StateGraph

def memory_tool_node(state):
    """检索记忆 + 写入新事实（走 MCP streamable-http）。"""
    query = state.get("user_input", "")
    import httpx
    # 检索（streamable-http，Bearer 鉴权）
    r = httpx.post(
        "http://127.0.0.1:8003/mcp",
        headers={"Authorization": "Bearer <KEY>",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "memory_search",
                         "arguments": {"query": query, "top_k": 5}}},
    )
    memories = r.json().get("result", {}).get("content", [])
    return {"memories": memories}

# 若用 cross-thread store 语义：用 store 存 agent_id 与 Thread 的映射，
# 检索时传 agent_id（Trinity 天然支持 agent_id 命名空间隔离）
```

**要点**：LangGraph 官方第三方记忆（Mem0/Zep）也是工具注入——Trinity
MCP 工具（memory_search/write/update）是等价形态。

---

## 三、LlamaIndex 接入（Mem0 兼容 / ChatMemoryBuffer）

LlamaIndex 用 `ChatMemoryBuffer` + vector index；第三方记忆经
`Mem0Memory` 适配器。Trinity 的 Gateway Mem0 兼容端点可直接被
Mem0Memory 类适配器消费（`id`/`memory` 字段已对齐）。

```python
# 方案 A：OpenAI 兼容（LlamaIndex 原生支持）
from llama_index.core.memory import ChatMemoryBuffer
buffer = ChatMemoryBuffer.from_defaults(token_limit=3000)

# 方案 B：Mem0 兼容（走 Gateway）
import requests
def mem0_like_add(content: str, user_id: str = "default"):
    requests.post("http://127.0.0.1:8002/v1/memories",
                  headers={"Authorization": "Bearer <KEY>"},
                  json={"content": content, "agent_id": user_id})

def mem0_like_search(query: str, top_k: int = 5):
    r = requests.get("http://127.0.0.1:8002/v1/memories",
                     headers={"Authorization": "Bearer <KEY>"},
                     params={"query": query, "top_k": top_k})
    return [m["memory"] for m in r.json().get("results", [])]
```

---

## 四、OpenAI Agents SDK 接入（memory tool + 自动注入）

OpenAI Agents SDK 的会话记忆有限，外部长期记忆走 **memory tool** +
`__memory_write__` 指令（Gateway 原生支持）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8002/v1", api_key="<KEY>")

# 写入记忆（__memory_write__ 指令，Gateway 本地落库不转发上游）
client.chat.completions.create(
    model="anything",
    messages=[{"role": "user", "content": "__memory_write__ 用户偏好暗色模式"}],
)

# 记忆自动注入的对话（Gateway 自动检索注入 system 上下文）
reply = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "我喜欢什么主题色？"}],
)
print(reply.choices[0].message.content)
```

**要点**：memory_k 参数控制注入条数（`memory_k=5`）；多 agent 场景传
agent_id 隔离命名空间。

---

## 五、Claude Code / Cursor / Dify（MCP 客户端）

```json
// .cursor/mcp.json 或 claude_desktop_config.json
{
  "mcpServers": {
    "trinity-memory": {
      "url": "http://127.0.0.1:8003/mcp",
      "headers": { "Authorization": "Bearer <KEY>" }
    }
  }
}
```

或 stdio（本机零鉴权）：
```json
{ "command": "trinity-mcp", "args": ["--mode", "stdio"] }
```

---

## 六、最佳实践清单

1. **优先 MCP**：一次接入，多框架复用（Claude/Cursor/Dify/自研都认）；
2. **agent_id 隔离**：多 agent 各用独立命名空间，防回音室污染；
3. **写入有纪律**：只写值得记的（偏好/事实/决策），自包含结构化文本；
4. **证据核对**：关键事实用 `GET /audit/receipt/{memory_id}` 验证；
5. **本地推理可选**：`TRINITY_LLM_BASE_URL=http://127.0.0.1:11434/v1`
   可切 Ollama 本地（批量/隐私场景；实时 QA 仍建议 DeepSeek API）。
