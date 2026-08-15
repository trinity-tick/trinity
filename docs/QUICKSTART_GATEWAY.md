# Quickstart — 5 分钟接入 Trinity 记忆

> 配合 B1 Memory Gateway：任何 LLM 应用用 OpenAI SDK 即可接入长期记忆。

## 方式 A：本机已有 Trinity API

```bash
# 1) 启动网关（默认对接 http://127.0.0.1:8001）
python -m pip install -r gateway/requirements.txt
python gateway/server.py          # :8002

# 2) 用 OpenAI SDK 直连
python - <<'EOF'
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8002", api_key="trinity")
# 写入记忆
c.chat.completions.create(model="x", messages=[{"role":"user","content":"__mem__ 用户喜欢深色模式"}])
# 记忆注入聊天（需配置 UPSTREAM_BASE_URL/UPSTREAM_API_KEY）
r = c.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":"我喜欢的主题色？"}])
print(r.choices[0].message.content)
EOF
```

## 方式 B：Docker 一键全栈

```bash
docker compose -f gateway/docker-compose.yml up -d
# trinity-gateway :8002  |  trinity-api :8001  |  trinity-db :5430
```

## 方式 C：自带 SDK（gateway/client.py）

```python
from trinity_gateway import TrinityGateway
mem = TrinityGateway()
mem.add("Trinity 图谱有 28k 关系", tags=["graph"])
print(mem.search("图谱关系"))
```

## 常用端点速查

| 操作 | 端点 |
|---|---|
| 写入 | `POST /v1/memories` |
| 检索（混合） | `POST /v1/memory/search` |
| 聊天（记忆注入） | `POST /v1/chat/completions` |
| 取/删 | `GET/DELETE /v1/memories/{id}` |
| 健康 | `GET /health` |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRINITY_API_URL` | `http://127.0.0.1:8001` | 后端地址 |
| `UPSTREAM_BASE_URL` | `https://api.openai.com/v1` | 上游 LLM（Ollama: `http://host:11434/v1`） |
| `UPSTREAM_API_KEY` | `OPENAI_API_KEY` | 上游 key |
| `MEMORY_CONTEXT_K` | `5` | 注入记忆条数 |
