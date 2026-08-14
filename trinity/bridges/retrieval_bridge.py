"""
retrieval_bridge.py — Trinity → Agent 回传通道
===============================================

从 Trinity MemoryAggregator 拉取记忆、洞察和上下文，提供 Agent
可直接注入的 context_injection_prompt。

用途：
  - Agent 对话前注入最近相关记忆（跨 Agent 共享上下文）
  - 查询特定 Agent 的记忆池
  - 获取聚合洞察和统计

API 端点：
  - GET  /agents/memory/search?q=...&top_k=...
  - GET  /agents/memory/insights
  - GET  /agents/memory/pool
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 可选依赖 ──────────────────────────────────────────────────────────
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import urllib.request
    import urllib.error
    _HAS_URLLIB = True
except ImportError:
    _HAS_URLLIB = True


# ── 常量 ──────────────────────────────────────────────────────────────
DEFAULT_API_BASE = "http://localhost:8005"
REQUEST_TIMEOUT = 10


class TrinityRetrievalBridge:
    """从 Trinity 拉取记忆、洞察、上下文的只读桥接器。"""

    def __init__(self, api_base: str = DEFAULT_API_BASE):
        self.api_base = api_base.rstrip("/")

        if _HAS_REQUESTS:
            self._session = requests.Session()
            self._session.headers.update({"Content-Type": "application/json"})
            self._get = self._get_requests
        else:
            self._get = self._get_urllib

    # ── HTTP 层 ───────────────────────────────────────────────────

    def _get_requests(self, path: str, params: dict = None) -> dict:
        resp = self._session.get(
            f"{self.api_base}{path}",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _get_urllib(self, path: str, params: dict = None) -> dict:
        import urllib.parse

        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── 公开 API ──────────────────────────────────────────────────

    def search_memories(
        self,
        query: str,
        agent_id: Optional[str] = None,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """语义搜索记忆池。

        Args:
            query: 搜索查询字符串
            agent_id: 限定某个 Agent 的记忆（None = 跨 Agent）
            top_k: 返回条数

        Returns:
            {"results": [...], "total": N, "query": "..."}
        """
        params = {"q": query, "top_k": top_k}
        if agent_id:
            params["agent_id"] = agent_id
        return self._get("/agents/memory/search", params)

    def get_insights(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """获取跨 Agent 洞察。

        Returns:
            {"insights": [...], "patterns": [...], ...}
        """
        params = {}
        if agent_id:
            params["agent_id"] = agent_id
        return self._get("/agents/memory/insights", params)

    def get_pool_stats(self) -> Dict[str, Any]:
        """获取聚合池全局统计。"""
        return self._get("/agents/memory/pool")

    def get_recent_context(
        self,
        agent_id: str,
        minutes: int = 30,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """获取最近 n 分钟内指定 Agent 的记忆上下文。

        Args:
            agent_id: 目标 Agent ID
            minutes: 回溯时间
            top_k: 最大返回条数

        Returns:
            记忆列表，每条含 content/source_agents/category/created_at
        """
        # 构建时间范围查询：搜索最近活动
        query = f"recent activity in last {minutes} minutes"
        result = self.search_memories(query=query, top_k=top_k)

        memories = result.get("results", [])

        # 过滤：只保留来自目标 agent 或在时间窗口内的
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = now_ts - (minutes * 60)

        filtered = []
        for m in memories:
            created = m.get("created_at", 0)
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created).timestamp()
                except Exception:
                    created = 0

            sources = m.get("source_agents", [])
            # 保留：目标 agent 发出的，或时间在窗口内的
            if agent_id in sources or created >= cutoff:
                filtered.append(m)

        return filtered[:top_k]

    def context_injection_prompt(
        self,
        agent_id: str,
        query: Optional[str] = None,
        scope: Optional[Dict[str, Any]] = None,
    ) -> str:
        """生成可注入到 Agent system prompt 的共享上下文片段。

        按三层记忆分类分层注入：
          - Episodic  (近期事件)  → 最近 60 分钟对话
          - Semantic  (长期事实)  → 定向搜索 + 全局洞察
          - Procedural (可用技能) → 流程/技能记忆

        Args:
            agent_id: 调用方 Agent ID
            query: 可选的当前任务关键词，用于定向搜索
            scope: 可选的复合范围过滤 (agent_id / session_id / category / …)

        Returns:
            Markdown 格式的上下文注入文本
        """
        # ── Layer 1: Episodic — recent events ────────────────────────
        recent = self.get_recent_context(agent_id, minutes=60, top_k=6)

        # ── Layer 2: Semantic — long-term facts ──────────────────────
        search_results = []
        if query:
            sr = self.search_memories(query=query, top_k=5)
            search_results = sr.get("results", [])

        try:
            insights = self.get_insights(agent_id)
        except Exception:
            insights = {}

        # ── Layer 3: Procedural — available skills ───────────────────
        procedural = []
        try:
            pr = self.search_memories(query="procedure skill action", top_k=3)
            # Filter to procedural category entries if available
            procedural = [
                m for m in pr.get("results", [])
                if m.get("category") == "procedural"
            ]
        except Exception:
            procedural = []

        # ── Scope filtering ─────────────────────────────────────────
        if scope:
            recent = self._apply_scope(recent, scope)
            search_results = self._apply_scope(search_results, scope)
            procedural = self._apply_scope(procedural, scope)

        # ── Assemble injection prompt ───────────────────────────────
        parts = [
            "## Trinity 共享记忆池",
            "",
            "以下是你和其他 Agent 的共享记忆，按三层分层组织：",
            "",
        ]

        # Layer 1: Episodic
        if recent:
            parts.append("### 第一层 · 近期事件 (Episodic)")
            for m in recent:
                content = (m.get("content") or "")[:200]
                sources = ", ".join(m.get("source_agents", []))
                parts.append(f"- [{sources}] {content}")
            parts.append("")

        # Layer 2: Semantic
        if search_results:
            parts.append(f"### 第二层 · 长期事实 (Semantic)")
            if query:
                parts.append(f"*定向搜索：{query}*")
            for m in search_results:
                content = (m.get("content") or "")[:200]
                sources = ", ".join(m.get("source_agents", []))
                parts.append(f"- [{sources}] {content}")
            parts.append("")

        insight_items = insights.get("insights", [])
        if insight_items:
            parts.append("### 跨 Agent 洞察 (Semantic)")
            for ins in insight_items[:5]:
                if isinstance(ins, dict):
                    parts.append(f"- {ins.get('summary', ins.get('content', str(ins)))[:150]}")
                else:
                    parts.append(f"- {str(ins)[:150]}")
            parts.append("")

        # Layer 3: Procedural
        if procedural:
            parts.append("### 第三层 · 可用技能 (Procedural)")
            for m in procedural:
                content = (m.get("content") or "")[:200]
                parts.append(f"- {content}")
            parts.append("")

        # Footer
        parts.extend([
            "---",
            f"*以上记忆来自 Trinity 共享池，由 {agent_id} 在对话启动时注入。*",
            "*你可以通过 Trinity API 查询更多相关记忆（/agents/memory/search）。*",
        ])

        return "\n".join(parts)

    @staticmethod
    def _apply_scope(
        items: List[Dict[str, Any]],
        scope: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Filter items by composite scope (AND semantic)."""
        filtered = []
        for item in items:
            match = True
            for key, val in scope.items():
                if key == "agent_id":
                    sources = item.get("source_agents", [])
                    if val not in sources:
                        match = False
                        break
                elif key in ("category", "session_id", "app_id"):
                    if item.get(key) != val:
                        match = False
                        break
            if match:
                filtered.append(item)
        return filtered


class InsightsWriter:
    """定期从 Trinity 拉取洞察，写入 Marvis 可访问的共享文件。"""

    def __init__(
        self,
        bridge: Optional[TrinityRetrievalBridge] = None,
        output_path: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ):
        self.bridge = bridge or TrinityRetrievalBridge()

        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent.parent
        self.output_path = Path(
            output_path or (project_root / "data" / "trinity_insights.json")
        )
        self.project_root = project_root

    def refresh(self) -> Dict[str, Any]:
        """拉取最新数据并写入 JSON 文件。

        Returns:
            写入的数据字典。
        """
        data: Dict[str, Any] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pool": {},
            "insights": {},
            "agent_contexts": {},
        }

        # 聚合池统计
        try:
            data["pool"] = self.bridge.get_pool_stats()
        except Exception as e:
            data["pool_error"] = str(e)

        # 全局洞察
        try:
            data["insights"] = self.bridge.get_insights()
        except Exception as e:
            data["insights_error"] = str(e)

        # 每个 Agent 的最近上下文
        from trinity.bridges.marvis_bridge import BUILTIN_AGENTS

        for agent_name in BUILTIN_AGENTS:
            agent_id = f"marvis-{agent_name}"
            try:
                ctx = self.bridge.get_recent_context(agent_id, minutes=120, top_k=5)
                data["agent_contexts"][agent_id] = [
                    {
                        "content": (m.get("content") or "")[:200],
                        "sources": m.get("source_agents", []),
                        "category": m.get("category"),
                        "memory_id": m.get("memory_id"),
                    }
                    for m in ctx
                ]
            except Exception as e:
                data["agent_contexts"][agent_id] = {"error": str(e)}

        # 写入
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), "utf-8"
        )

        return data
