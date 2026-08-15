# Trinity Memory Gateway

把 **Trinity Memory OS（129 个 REST 端点、47 通道检索、11k 实体图谱）** 包装成 LLM 应用熟悉的
**OpenAI / Mem0 兼容 API**。任何会用 OpenAI SDK 的应用，5 分钟接入长期记忆。

## 快速开始（本地，对接已运行的 Trinity API）

```bash
# 1. 启动网关（默认对接 http://127.0.0.1:8001）
python -m pip install -r gateway/requirements.txt
python gateway/server.py            # 监听 127.0.0.1:8002

# 2. 冒烟测试
python gateway/client.py
```

## 快速开始（Docker 一键全栈）

```bash
docker compose -f gateway/docker-compose.yml up -d
# trinity-gateway  :8002    trinity-api :8001    trinity-db :5430
```

## 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/memories` | 写入记忆（content/tags/category/importance/metadata…） |
| `GET /v1/memories?query=&top_k=` | 检索（带 query 走 hybrid 融合检索；否则列最新） |
| `GET /v1/memories/{id}` / `DELETE /v1/memories/{id}` | 取/删单条 |
| `POST /v1/memory/search` | 混合检索（strategy: fusion/rrf/cascade） |
| `POST /v1/chat/completions` | **记忆注入聊天**：自动检索相关记忆→注入 system→转发上游 LLM |
| `GET /health` | 健康检查（含 Trinity 状态） |

## 用 OpenAI SDK 直连示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8002/v1", api_key="trinity")  # 注意 /v1 前缀

# 写入记忆
client.chat.completions.create(
    model="anything",
    messages=[{"role": "user", "content": "__memory_write__ 用户偏好深色模式"}],
)
# 记忆注入聊天（走 /v1/chat/completions，需配置上游 LLM）
reply = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "我喜欢的主题色是什么？"}],
)
print(reply.choices[0].message.content)
```

## 用自带 SDK

```python
from trinity_gateway import TrinityGateway   # gateway/client.py

mem = TrinityGateway()
mem.add("Trinity 图谱已有 28k 关系", tags=["graph", "fact"])
print(mem.search("图谱关系"))
reply = mem.chat([{"role": "user", "content": "图谱里有多少关系？"}])
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRINITY_API_URL` | `http://127.0.0.1:8001` | 后端 Trinity REST 地址 |
| `TRINITY_API_KEY` | 空 | 后端鉴权 key（可选） |
| `GATEWAY_PORT` | `8002` | 网关端口 |
| `UPSTREAM_BASE_URL` | `https://api.openai.com/v1` | 上游 LLM；Ollama 用 `http://host:11434/v1` |
| `UPSTREAM_API_KEY` / `OPENAI_API_KEY` | 空 | 上游 LLM key |
| `DEFAULT_MODEL` | `gpt-4o-mini` | 默认模型 |
| `MEMORY_CONTEXT_K` | `5` | 每次注入的记忆条数 |

## v0 边界（已知限制）

- `POST /v1/chat/completions` 的 `stream=true` 目前仅透传返回，未做流式封装
- 删除记忆直接映射 `DELETE /memories/{id}`（Trinity 支持）
- 记忆注入为简单模板（system 前缀），未做重排/去重，后续可接 Reranker
- 文件结构：`server.py` 网关、`client.py` SDK、`Dockerfile`、`docker-compose.yml` 全栈编排

## 路线（对应 EXECUTION_PLAN_V2.md B1）

- [x] B1.1 schema 确认
- [x] B1.2 兼容层 server.py
- [x] B1.3 SDK client.py
- [x] B1.4 Docker 编排
- [ ] B1.5 OpenAI SDK 冒烟测试（需要 OpenAI SDK 环境）
- [ ] 流式转发 / Reranker / 多租户隔离
