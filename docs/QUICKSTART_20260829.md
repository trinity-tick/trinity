# Trinity 快速开始（5 分钟接入指南）

> 面向第三方开发者：让任何 LLM 应用获得跨会话记忆能力。

## 1. 启动服务（1 分钟）

```bash
cd trinity
powershell -File dsh-ops\trinity-supervisor.ps1   # 全部服务自动拉起（守护循环）
```

启动后可用：API :8001 · MCP :8000/:8003 · RAG :8002 · 记忆流 UI :8010

## 2. 验证（30 秒）

```bash
curl http://127.0.0.1:8001/health
# {"status": "ok", ...}
```

## 3. 写入 + 检索（2 分钟）

### REST

```bash
# 写入
curl -s http://127.0.0.1:8001/memories -H "Content-Type: application/json" \
  -d '{"content": "用户偏好暗色模式与中文交流", "category": "general"}'

# 检索（混合）
curl -s http://127.0.0.1:8001/memory/search/hybrid \
  -H "Content-Type: application/json" \
  -d '{"query": "用户偏好", "top_k": 5}'
```

### Python

```python
from trinity import Trinity
mem = Trinity(adapter="sqlite")
mem.ingest("用户偏好暗色模式", agent_id="my-app")
hits = mem.search(query="用户偏好", mode="hybrid", top_k=5)
```

## 4. RAG 一行接入（1 分钟）

```bash
curl -s http://127.0.0.1:8002/v1/retrieval \
  -H "Content-Type: application/json" \
  -d '{"query": "WMS 上架规范", "top_k": 5}'
```

任意 LLM 应用：检索结果注入 prompt → LLM 生成。详见
[RAG_SERVICE_20260827.md](RAG_SERVICE_20260827.md)。

## 5. MCP 接入（Agent 用）

MCP server 暴露 memory_search / memory_write / memory_update / memory_delete /
audit_query / knowledge_search / evolution_status 等工具（stdio 无鉴权；
streamable-http :8003 用 Bearer）。

## 能力总览

记忆（加密+审计+版本链）· 知识层 · 认知分层 · 自动遗忘 · 记忆资产化 ·
自进化引擎 · 自动化编排 · AgentMesh 协作 · 联邦同步 · 合规报告 · RAG 服务

详见 [TRINITY_SUMMARY_20260827.md](TRINITY_SUMMARY_20260827.md)（完整总览）、
[EVOLUTION_DIRECTIONS_20260827.md](EVOLUTION_DIRECTIONS_20260827.md)（进化方向）。

## 常用维护

```bash
powershell -File dsh-ops\trinity-dsh-maintenance.ps1 -Tasks health,eval,tune   # 手动维护
python scripts/fulltest_gate.py                                               # 全量门禁
```

*生成 2026-08-29*
