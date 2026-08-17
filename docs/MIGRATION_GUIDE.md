# 迁移指南 — 从 Mem0 / OpenAI Assistants 迁到 Trinity

## 1. 从 Mem0 迁移

| Mem0 概念 | Trinity 等价 | 说明 |
|---|---|---|
| `add(content, metadata)` | `POST /v1/memories` (gateway) | 字段一一对应（content/tags/category/importance） |
| `search(query)` | `POST /v1/memory/search` | 默认 rrf 融合，比 Mem0 多图谱通道 |
| `get_all()` | `GET /v1/memories?top_k=` | 列表/最新 |
| `delete(memory_id)` | `DELETE /v1/memories/{id}` | 软删 |
| history/timeline | `GET /memories/{id}/versions` | 版本链（CRDT） |

迁移步骤：
1. 用 Mem0 导出全部记忆 → 转成 `{content, tags, category, importance}` 条目
2. `POST /agents/memory/bulk_write` 批量写入（100 条/批）
3. 切换应用调用点到 gateway（`base_url` 指向 :8002 即可，SDK 兼容）

## 2. 从 OpenAI Assistants（Threads）迁移

| Assistants 概念 | Trinity 等价 |
|---|---|
| Thread | `session_id` |
| Message | `POST /memories/session`（整段会话聚合为一条记忆） |
| File search | `POST /memory/search/hybrid`（47 通道） |
| Vector store | `GET /vector/search` + 聚合池向量索引 |

注意：Assistants 的"会话碎片"正是 Trinity 实测要避免的（LoCoMo 结论：
逐 turn 写入 Recall@5=0.14，按会话聚合后 0.88）→ 推荐用 `/memories/session` 聚合写入。

## 3. 数据导出（双向）

```bash
# Trinity → 外部
python federation/sync_protocol.py export --api http://127.0.0.1:8001 --agent default --out snap.json
# 外部 → Trinity
python federation/sync_protocol.py import --api http://127.0.0.1:8002 --file snap.json
```

## 4. 常见坑

- **鉴权**：写路径需 `X-Agent-ID` 头（RBAC）；gateway 已内置
- **检索结果**：engine hybrid 不带 content（A1 已修复 content_preview）；要全文走聚合池通道
- **id 差异**：迁移后 memory_id 重新生成，用 content hash 去重（`POST /agents/memory/bulk_write` 幂等）
