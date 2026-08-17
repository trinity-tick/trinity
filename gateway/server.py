# -*- coding: utf-8 -*-
"""Trinity Memory Gateway — OpenAI/Mem0 兼容记忆网关 (v0.1.0)

把 Trinity REST API (129 端点) 包装成 LLM 应用熟悉的 OpenAI/Mem0 形态:

    POST   /v1/memories              写入记忆   (兼容 OpenAI Assistants File 风格简化版)
    GET    /v1/memories?query=&top_k= 检索记忆  (带 query 走 hybrid 检索, 否则按关键词列最新)
    GET    /v1/memories/{id}         取单条
    DELETE /v1/memories/{id}         删除单条
    POST   /v1/memory/search         混合检索 (fusion/rrf/cascade)
    POST   /v1/chat/completions      聊天代理: 自动注入相关记忆后转发上游 LLM (OpenAI/Ollama 兼容)

环境变量:
    TRINITY_API_URL     Trinity REST 地址       (默认 http://127.0.0.1:8001)
    TRINITY_API_KEY     可选 Bearer key
    GATEWAY_PORT        网关端口                (默认 8002)
    UPSTREAM_BASE_URL   上游 LLM base           (默认 https://api.openai.com/v1; Ollama 用 http://host:11434/v1)
    UPSTREAM_API_KEY    上游 LLM key            (默认取 OPENAI_API_KEY)
    DEFAULT_MODEL       默认模型名              (默认 gpt-4o-mini)
    MEMORY_CONTEXT_K    注入记忆条数            (默认 5)

用法:
    uvicorn server:app --host 0.0.0.0 --port 8002
"""
import os
import time as _time
import threading as _threading
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

TRINITY_API = os.environ.get("TRINITY_API_URL", "http://127.0.0.1:8001").rstrip("/")
TRINITY_API_KEY = os.environ.get("TRINITY_API_KEY", "")
GATEWAY_AGENT_ID = os.environ.get("GATEWAY_AGENT_ID", "gateway")
GATEWAY_AGENT_ROLE = os.environ.get("GATEWAY_AGENT_ROLE", "admin")
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-4o-mini")
MEMORY_K = int(os.environ.get("MEMORY_CONTEXT_K", "5"))

# ── 生产化（2026-08-15）─────────────────────────────────────────────
# 上游模型名映射：MODEL_ALIASES="gpt-4o-mini:deepseek-v4-flash,deepseek-chat:deepseek-v4-flash"
# 上游为 DeepSeek 时自动把 OpenAI 默认名映射到 v4 系列。
MODEL_ALIASES: Dict[str, str] = {}
for pair in os.environ.get("MODEL_ALIASES", "").split(","):
    if ":" in pair:
        k, v = pair.split(":", 1)
        MODEL_ALIASES[k.strip()] = v.strip()
if "deepseek" in UPSTREAM_BASE:
    MODEL_ALIASES.setdefault("gpt-4o-mini", "deepseek-v4-flash")
    MODEL_ALIASES.setdefault("gpt-4o", "deepseek-v4-pro")
    MODEL_ALIASES.setdefault("deepseek-chat", "deepseek-v4-flash")

def _resolve_model(model: Optional[str]) -> str:
    m = model or DEFAULT_MODEL
    return MODEL_ALIASES.get(m, m)

# 网关自身鉴权：设置 GATEWAY_API_KEY 后所有请求需 Bearer 匹配（未设置=开放）
GATEWAY_KEY = os.environ.get("GATEWAY_API_KEY", "")
# 简单限流：GATEWAY_RATE_LIMIT 次/分钟/客户端 IP（0=关闭）
RATE_LIMIT = int(os.environ.get("GATEWAY_RATE_LIMIT", "0"))
_RATE: Dict[str, List[float]] = {}
_RATE_LOCK = _threading.Lock()

# 监控计数（/metrics，Prometheus 文本格式）
_METRICS_LOCK = _threading.Lock()
_METRICS = {"requests_total": 0, "errors_total": 0, "start_time": _time.time()}

_HEADERS = {"Authorization": f"Bearer {TRINITY_API_KEY}"} if TRINITY_API_KEY else {}
# RBAC：受保护路由要求 X-Agent-ID（缺失 → 401），显式角色 admin 获得全量权限
_HEADERS.update({"X-Agent-ID": GATEWAY_AGENT_ID, "X-Agent-Role": GATEWAY_AGENT_ROLE})

app = FastAPI(
    title="Trinity Memory Gateway",
    version="0.2.0",
    description="OpenAI/Mem0-compatible memory API backed by Trinity Memory OS.",
)


@app.middleware("http")
async def _gateway_middleware(request: Request, call_next):
    """鉴权 + 限流 + 监控（生产化，2026-08-15）。"""
    client_ip = request.client.host if request.client else "unknown"
    # 鉴权
    if GATEWAY_KEY:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {GATEWAY_KEY}":
            with _METRICS_LOCK:
                _METRICS["errors_total"] += 1
            return JSONResponse(status_code=401, content={"error": "invalid gateway api key"})
    # 限流（窗口 60s 滑动）
    if RATE_LIMIT > 0:
        now = _time.time()
        with _RATE_LOCK:
            win = [t for t in _RATE.get(client_ip, []) if now - t < 60.0]
            if len(win) >= RATE_LIMIT:
                with _METRICS_LOCK:
                    _METRICS["errors_total"] += 1
                return JSONResponse(status_code=429, content={"error": "rate limit exceeded"})
            win.append(now)
            _RATE[client_ip] = win
    with _METRICS_LOCK:
        _METRICS["requests_total"] += 1
    try:
        return await call_next(request)
    except Exception:
        with _METRICS_LOCK:
            _METRICS["errors_total"] += 1
        raise


@app.get("/metrics")
def metrics() -> Dict[str, Any]:
    """Prometheus 文本格式基础指标。"""
    with _METRICS_LOCK:
        uptime = _time.time() - _METRICS["start_time"]
        return {
            "status": "ok",
            "requests_total": _METRICS["requests_total"],
            "errors_total": _METRICS["errors_total"],
            "uptime_seconds": round(uptime, 1),
        }


# ── 请求模型 ────────────────────────────────────────────────────────────


class MemoryIn(BaseModel):
    content: str = Field(..., description="记忆内容")
    persona_id: Optional[str] = "default"
    session_id: Optional[str] = None
    role: Optional[str] = "user"
    importance: Optional[float] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    agent_id: Optional[str] = None
    tenant_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    source_uri: Optional[str] = None


class SearchIn(BaseModel):
    query: str
    top_k: int = 5
    strategy: str = "rrf"  # fusion / rrf / cascade
    agent_id: Optional[str] = None
    persona_id: Optional[str] = None
    tenant_id: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    memory_k: Optional[int] = None


# ── 内部工具 ────────────────────────────────────────────────────────────


def _t(path: str, method: str = "GET", **kw) -> Dict[str, Any]:
    """调用 Trinity API。"""
    url = f"{TRINITY_API}/{path.lstrip('/')}"
    r = requests.request(method=method, url=url, headers=_HEADERS, timeout=60, **kw)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def _pool_search(query: str, top_k: int = MEMORY_K, mode: str = "hybrid") -> List[Dict]:
    """聚合池检索（返回带 content 的结果，MCP 同源）。"""
    data = _t(
        "agents/memory/search",
        method="GET",
        params={"q": query, "top_k": top_k, "mode": mode},
    )
    if isinstance(data, dict):
        return data.get("results", data.get("memories", []))
    return data if isinstance(data, list) else []


def _hybrid(query: str, top_k: int = MEMORY_K, strategy: str = "rrf") -> List[Dict]:
    """引擎混合检索（结果只含 id+score，按 id 回填内容；池记忆回填失败留空）。"""
    data = _t(
        "memory/search/hybrid",
        method="POST",
        json={"query": query, "top_k": top_k, "strategy": strategy},
    )
    results = data.get("results", data if isinstance(data, list) else [])
    for r in results:
        mid = r.get("memory_id")
        # rrf/fusion 结果可能只有 content_preview——只要 content 缺失就按 id 回填
        if mid and not r.get("content"):
            try:
                detail = _t(f"memories/{mid}", method="GET")
                if isinstance(detail, dict):
                    r["content"] = detail.get("content") or detail.get("content_preview") or ""
            except HTTPException:
                pass
    return results


def _search(query: str, top_k: int = MEMORY_K, strategy: str = "rrf") -> List[Dict]:
    """网关主检索：引擎 47 通道优先（新写入立即可见、含 content_preview），
    池兜底（结果自带完整 content）。"""
    eng = _hybrid(query, top_k=top_k, strategy=strategy)
    if eng:
        return eng
    return _pool_search(query, top_k=top_k)


# ── 健康检查 ────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        upstream = _t("health")
        return {"status": "ok", "trinity": upstream.get("status"), "gateway": "ok"}
    except HTTPException as exc:
        return {"status": "degraded", "trinity_error": exc.detail}


# ── 记忆 CRUD（Mem0 风格）─────────────────────────────────────────────


@app.post("/v1/memories", status_code=201)
def create_memory(body: MemoryIn) -> Dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    return _t("memories", method="POST", json=payload)


@app.get("/v1/memories")
def list_memories(
    query: Optional[str] = None,
    top_k: int = 10,
    category: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    if query:
        return {"results": _search(query, top_k=top_k)}
    params = {"query": "", "top_k": top_k}
    if category:
        params["category"] = category
    if agent_id:
        params["agent_id"] = agent_id
    return {"results": _t("memories", method="GET", params=params)}


@app.get("/v1/memories/{memory_id}")
def get_memory(memory_id: str) -> Dict[str, Any]:
    return _t(f"memories/{memory_id}", method="GET")


@app.delete("/v1/memories/{memory_id}")
def delete_memory(memory_id: str) -> Dict[str, Any]:
    return _t(f"memories/{memory_id}", method="DELETE")


@app.post("/v1/memory/search")
def search_memory(body: SearchIn) -> Dict[str, Any]:
    return {"results": _search(body.query, top_k=body.top_k, strategy=body.strategy)}


# ── 聊天代理（记忆自动注入）──────────────────────────────────────────


def _build_context(messages: List[ChatMessage], k: int) -> str:
    last_user = ""
    for m in reversed(messages):
        if m.role == "user":
            last_user = m.content
            break
    if not last_user:
        return ""
    results = _search(last_user, top_k=k)
    if not results:
        return ""
    lines = []
    for i, mem in enumerate(results, 1):
        text = mem.get("content") or mem.get("content_preview") or ""
        if text:
            lines.append(f"[{i}] {text[:500]}")
    return "\n".join(lines)


@app.post("/v1/chat/completions")
def chat_completions(body: ChatIn) -> Dict[str, Any]:
    # ── __memory_write__ 指令：本地写记忆，不转发上游（2026-08-15 补全）──
    for m in body.messages:
        if m.role == "user" and isinstance(m.content, str) and m.content.strip().startswith("__memory_write__"):
            mem_content = m.content.split("__memory_write__", 1)[1].strip()
            if mem_content:
                _t("memories", method="POST", json={"content": mem_content})
                return {
                    "id": "memwrite", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {
                        "role": "assistant",
                        "content": f"记忆已写入：{mem_content[:60]}",
                    }, "finish_reason": "stop"}],
                }

    k = body.memory_k or MEMORY_K
    context = _build_context(body.messages, k)
    upstream_messages: List[Dict[str, str]] = []
    if context:
        upstream_messages.append(
            {
                "role": "system",
                "content": (
                    "以下是与用户问题相关的记忆片段，回答时优先使用其中的事实，"
                    "并标注来源编号：\n" + context
                ),
            }
        )
    upstream_messages.extend(m.model_dump() for m in body.messages)

    payload: Dict[str, Any] = {
        "model": _resolve_model(body.model),
        "messages": upstream_messages,
    }
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.stream:
        payload["stream"] = True

    upstream_headers = {"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {}
    r = requests.post(
        f"{UPSTREAM_BASE}/chat/completions",
        json=payload,
        headers=upstream_headers,
        timeout=120,
        stream=bool(body.stream),
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    if body.stream:
        # 2026-08-16 修复:正确转发上游 SSE 流(此前返回无内容的假响应,导致客户端"无回复")
        try:
            return StreamingResponse(
                (line for line in r.iter_lines(decode_unicode=False) if line),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except GeneratorExit:
            raise
    return r.json()


# ── OpenAI SDK 兼容别名（SDK 把 base_url 视为含 /v1 前缀，实际请求无前缀路径）──

@app.post("/chat/completions")
def chat_completions_alias(body: ChatIn) -> Dict[str, Any]:
    return chat_completions(body)


@app.get("/v1/models")
@app.get("/models")
def list_models() -> Dict[str, Any]:
    return {"object": "list", "data": [
        {"id": DEFAULT_MODEL, "object": "model", "owned_by": "trinity-gateway"}
    ]}


if __name__ == "__main__":
    port = int(os.environ.get("GATEWAY_PORT", "8002"))
    uvicorn.run(app, host="127.0.0.1", port=port)  # 2026-08-17 安全加固：仅本机
