# Trinity RAG 检索服务（2026-08-27 方向E）

## 端点

`POST http://127.0.0.1:8002/v1/retrieval`

## 请求

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| query | string | 必填 | 检索查询 |
| top_k | int | 5 | 返回条数 |
| mode | string | hybrid | hybrid / keyword / semantic / graph |
| layer_hint | string | null | auto / episodic / semantic（认知分层） |

## 响应

`{ "object": "retrieval", "query": "...", "count": n, "data": [{content, score, memory_id, category, created_at, layer}] }`

content 已解密（存储加密下安全输出）。

## 一行接入

### curl

```bash
curl -s http://127.0.0.1:8002/v1/retrieval -H "Content-Type: application/json" \
  -d '{"query": "WMS 上架作业规范", "top_k": 5}'
```

### Python

```python
import urllib.request, json
req = urllib.request.Request(
    "http://127.0.0.1:8002/v1/retrieval",
    data=json.dumps({"query": "WMS 上架作业规范", "top_k": 5}).encode(),
    headers={"Content-Type": "application/json"})
resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
for item in resp["data"]:
    print(item["content"][:100], item["score"])
```

### 任意 LLM 应用（RAG 模式）

```
用户问题 → /v1/retrieval → 相关记忆 → 注入 prompt → LLM 回答
（记忆自动增强：Trinity 提供跨会话上下文，LLM 负责生成）
```

## 说明

- 与 gateway 既有 /v1/memory/search（Mem0 兼容）并存，本端点为标准 RAG 形态；
- 检索决策全链可审计（audit_log：query/mode/hits/memory_ids/elapsed_ms/layer）；
- 层感知：layer_hint=auto 时时间词→episodic、知识词→semantic。

*生成 2026-08-27*

## 鉴权（对外部署时启用，2026-08-27 评估通过）

1. 设置 TRINITY_GATEWAY_TOKEN（supervisor 启动环境或进程 env），然后重启 gateway；
2. 调用方必须带 Bearer 头（实测：无 token -> 401，有 token -> 200）：
   curl -s http://127.0.0.1:8002/v1/retrieval -H "Authorization: Bearer your-secret-token" -H "Content-Type: application/json" -d '{"query": "WMS", "top_k": 5}'
3. 未设置 token 时保持无鉴权（本地开发默认）。
