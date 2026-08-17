# -*- coding: utf-8 -*-
"""Trinity Memory Gateway Python SDK — 一行接入记忆。

用法:
    from trinity_gateway import TrinityGateway
    mem = TrinityGateway()                       # 默认 http://127.0.0.1:8002
    mem.add("用户偏好深色模式", tags=["preference"])
    results = mem.search("用户偏好")
    reply = mem.chat([{"role": "user", "content": "我喜欢的主题色？"}])
"""
from typing import Any, Dict, List, Optional

import requests


class TrinityGateway:
    """OpenAI/Mem0 风格记忆客户端，后端为 Trinity Memory Gateway。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8002", api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _req(self, method: str, path: str, **kw) -> Any:
        r = requests.request(method, f"{self.base_url}/{path.lstrip('/')}",
                             headers=self._headers, timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    # ── 记忆操作 ──────────────────────────────────────────────────

    def add(self, content: str, tags: Optional[List[str]] = None,
            category: Optional[str] = None, importance: Optional[float] = None,
            **extra) -> Dict[str, Any]:
        """写入一条记忆。"""
        payload: Dict[str, Any] = {"content": content}
        if tags:
            payload["tags"] = tags
        if category:
            payload["category"] = category
        if importance is not None:
            payload["importance"] = importance
        payload.update(extra)
        return self._req("POST", "v1/memories", json=payload)

    def search(self, query: str, top_k: int = 5, strategy: str = "rrf") -> List[Dict]:
        """混合检索记忆（向量 + BM25 + 图谱融合）。"""
        return self._req("POST", "v1/memory/search",
                         json={"query": query, "top_k": top_k, "strategy": strategy}).get("results", [])

    def get(self, memory_id: str) -> Dict[str, Any]:
        return self._req("GET", f"v1/memories/{memory_id}")

    def delete(self, memory_id: str) -> Dict[str, Any]:
        return self._req("DELETE", f"v1/memories/{memory_id}")

    def list(self, query: Optional[str] = None, top_k: int = 10) -> List[Dict]:
        params = {"top_k": top_k}
        if query:
            params["query"] = query
        return self._req("GET", "v1/memories", params=params).get("results", [])

    # ── 聊天（记忆自动注入）──────────────────────────────────────

    def chat(self, messages: List[Dict[str, str]], model: Optional[str] = None,
             memory_k: int = 5, **extra) -> Dict[str, Any]:
        """聊天请求，自动把相关记忆注入 system context 后转发上游 LLM。"""
        payload: Dict[str, Any] = {"messages": messages, "memory_k": memory_k}
        if model:
            payload["model"] = model
        payload.update(extra)
        return self._req("POST", "v1/chat/completions", json=payload)

    def health(self) -> Dict[str, Any]:
        return self._req("GET", "health")


if __name__ == "__main__":
    # 冒烟测试
    import sys

    g = TrinityGateway()
    print("health:", g.health())
    r = g.add("Trinity Gateway SDK 冒烟测试记忆", tags=["test", "smoke"])
    mid = r.get("memory_id") or r.get("id")
    print("add:", r)
    print("search:", [m.get("content", "")[:40] for m in g.search("冒烟测试")])
    if mid:
        print("delete:", g.delete(mid))
