"""
Federated Memory Query — P1-4 联邦查询模块

跨 Agent 并行发起记忆查询，RRF 融合多源结果并去重排序。
支持按能力标签路由查询，与 A2A Registry 集成实现 Agent 自动发现。

设计要点:
    - 并行查询：ThreadPool 并发向所有 Agent 发起 memory_search
    - RRF 融合：Reciprocal Rank Fusion 跨源合并 + 语义去重
    - 智能路由：基于关键词匹配 Agent 能力标签自动选择查询目标
    - 优雅降级：单 Agent 超时/失败不影响整体结果
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FederatedMemoryQuery:
    """联邦记忆查询引擎 — 跨 Agent 并行检索 + RRF 融合。

    使用方式::

        from trinity.a2a_registry import AgentRegistry
        from trinity.modules.federated_query import FederatedMemoryQuery

        registry = AgentRegistry()
        federated = FederatedMemoryQuery(registry)
        results = federated.query_across_agents(
            "Q3 华东区毛利率分析",
            agent_filter=["marvis", "file"],
            top_k=10,
        )
    """

    # ── 内置 Agent 能力标签（独立注册接口的 fallback）───────────────

    DEFAULT_AGENTS: Dict[str, Dict[str, Any]] = {
        "marvis": {
            "name": "Marvis Core",
            "capabilities": ["memory.search", "memory.store", "planning",
                             "reasoning", "task.orchestration"],
            "description": "任务编排与推理中枢",
        },
        "file": {
            "name": "File Agent",
            "capabilities": ["file.search", "file.read", "file.edit",
                             "file.organize", "document.analysis"],
            "description": "本地文件系统智能助手",
        },
        "search": {
            "name": "Search Agent",
            "capabilities": ["web.search", "research.deep", "paper.retrieval",
                             "comparison.analysis"],
            "description": "深度搜索与调研专家",
        },
        "browser": {
            "name": "Browser Agent",
            "capabilities": ["web.browse", "form.fill", "page.extract",
                             "authentication"],
            "description": "网页自动化操作专家",
        },
        "computer": {
            "name": "Computer Agent",
            "capabilities": ["system.diagnose", "process.manage",
                             "window.layout", "desktop.ops"],
            "description": "Windows 系统专家",
        },
        "app": {
            "name": "App Agent",
            "capabilities": ["app.install", "app.uninstall", "app.launch",
                             "ui.interact", "app.recommend"],
            "description": "应用与游戏操作专家",
        },
    }

    # ── 查询路由关键词映射 ──────────────────────────────────────────

    ROUTE_KEYWORDS: Dict[str, List[str]] = {
        "marvis": ["任务", "计划", "编排", "推理", "分析报告", "方案",
                   "plan", "orchestrate", "reason"],
        "file": ["文件", "文档", "发票", "合同", "PDF", "Excel", "Word",
                 "整理", "删除", "复制", "file", "document", "invoice"],
        "search": ["搜索", "调研", "论文", "最新", "对比", "research",
                   "paper", "compare", "latest"],
        "browser": ["网页", "登录", "表单", "提取", "browser", "webpage",
                    "login", "form"],
        "computer": ["系统", "设置", "进程", "桌面", "窗口", "system",
                     "process", "desktop", "window"],
        "app": ["安装", "卸载", "启动", "APP", "微信", "小程序",
                "install", "launch", "uninstall"],
    }

    # ── 构造函数 ──────────────────────────────────────────────────────

    def __init__(self, a2a_registry=None):
        """初始化联邦查询引擎。

        参数:
            a2a_registry: A2A 注册表实例（AgentRegistry 或兼容接口）。
                          为 None 时使用内置独立注册接口（DEFAULT_AGENTS）。
        """
        self._registry = a2a_registry
        self._use_a2a = a2a_registry is not None

        # 独立注册接口（无 A2A 注册表时使用）
        self._standalone_agents: Dict[str, Dict[str, Any]] = dict(
            self.DEFAULT_AGENTS
        )

        # 统计
        self._stats: Dict[str, int] = {
            "total_queries": 0,
            "total_agents_queried": 0,
            "total_results": 0,
            "errors": 0,
            "timeouts": 0,
        }
        self._lock = threading.Lock()

        if self._use_a2a:
            logger.info("FederatedMemoryQuery initialized with A2A registry")
        else:
            logger.info(
                "FederatedMemoryQuery initialized with standalone registry "
                "(%d agents)", len(self._standalone_agents),
            )

    # ── 主查询接口 ────────────────────────────────────────────────────

    def query_across_agents(
        self,
        query: str,
        agent_filter: Optional[List[str]] = None,
        top_k: int = 10,
        timeout: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """向所有（或指定）Agent 并行发起记忆查询，融合结果。

        参数:
            query: 查询文本。
            agent_filter: 可选，限定查询的 Agent 标签列表。
                          例如 ['marvis', 'file'] 仅向这两个 Agent 查询。
            top_k: 返回结果数。
            timeout: 单个 Agent 查询超时（秒）。

        返回:
            RRF 融合后的结果列表，每项包含:
            {"id": ..., "content": ..., "score": ..., "source_agent": ...,
             "relevance": ...}
        """
        with self._lock:
            self._stats["total_queries"] += 1

        # 1) 确定目标 Agent 列表
        if agent_filter:
            target_agents = self._resolve_agents(agent_filter)
        else:
            target_agents = self._get_all_agent_ids()

        if not target_agents:
            logger.warning("No agents available for query: %s", query[:60])
            return []

        # 2) 并行查询
        all_results: List[Dict[str, Any]] = []
        max_workers = min(len(target_agents), 8)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._query_single_agent, aid, query, top_k, timeout
                ): aid
                for aid in target_agents
            }
            for future in as_completed(futures):
                aid = futures[future]
                try:
                    results = future.result(timeout=timeout + 5)
                    if results:
                        all_results.extend(results)
                    with self._lock:
                        self._stats["total_agents_queried"] += 1
                except Exception as e:
                    logger.warning(
                        "Agent '%s' query failed: %s", aid, str(e)[:100],
                    )
                    with self._lock:
                        self._stats["errors"] += 1

        # 3) RRF 融合 + 去重 + 排序
        fused = self._fuse_results(all_results, top_k)
        with self._lock:
            self._stats["total_results"] += len(fused)

        return fused

    # ── 单 Agent 查询 ─────────────────────────────────────────────────

    def _query_single_agent(
        self,
        agent_id: str,
        query: str,
        top_k: int,
        timeout: float,
    ) -> List[Dict[str, Any]]:
        """调用单个 Agent 的记忆查询接口。

        优先通过 A2A registry endpoint 调用 MCP memory_search；
        无可用注册表时生成模拟结果作为框架占位。

        参数:
            agent_id: Agent 标识符。
            query: 查询文本。
            top_k: 返回结果数。
            timeout: 超时秒数。

        返回:
            该 Agent 的查询结果列表。
        """
        if self._use_a2a:
            return self._query_via_a2a(agent_id, query, top_k, timeout)

        # Standalone fallback: 模拟查询结果
        return self._query_standalone(agent_id, query, top_k)

    def _query_via_a2a(
        self, agent_id: str, query: str, top_k: int, timeout: float,
    ) -> List[Dict[str, Any]]:
        """通过 A2A 注册表的 endpoint 调用 MCP memory_search。"""
        try:
            agents = self._registry.discover()
            agent_info = next(
                (a for a in agents if a.agent_id == agent_id), None
            )
            if agent_info is None:
                logger.debug("Agent '%s' not found in A2A registry", agent_id)
                return []

            # 构建 MCP 调用
            import urllib.request
            import json as _json

            payload = _json.dumps({
                "method": "memory_search",
                "params": {"query": query, "top_k": top_k},
            }).encode("utf-8")

            req = urllib.request.Request(
                agent_info.endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                results = data.get("results", data.get("memories", []))
                for r in results:
                    r["source_agent"] = agent_id
                return results

        except Exception as e:
            logger.debug(
                "A2A query failed for '%s': %s", agent_id, str(e)[:80],
            )
            return []

    def _query_standalone(
        self, agent_id: str, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        """独立模式：基于关键词匹配生成模拟查询结果（框架占位）。

        生产环境中应替换为真实的 MCP 调用。
        """
        agent = self._standalone_agents.get(agent_id)
        if agent is None:
            return []

        caps = agent.get("capabilities", [])
        query_lower = query.lower()

        # 简单相关度模拟：关键词命中 capability 标签
        results: List[Dict[str, Any]] = []
        import re

        keywords = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", query_lower)

        for i, cap in enumerate(caps):
            match_score = 0.0
            for kw in keywords:
                if kw in cap.lower():
                    match_score += 0.3
            if match_score > 0:
                results.append({
                    "id": f"{agent_id}_{i}",
                    "content": f"[{agent.get('name', agent_id)}] "
                               f"能力 '{cap}' 匹配查询 '{query[:40]}'",
                    "score": round(min(match_score, 1.0), 4),
                    "source_agent": agent_id,
                    "capability": cap,
                    "timestamp": time.time(),
                })

        results.sort(key=lambda r: -r["score"])
        return results[:top_k]

    # ── RRF 融合 ──────────────────────────────────────────────────────

    def _fuse_results(
        self,
        all_results: List[Dict[str, Any]],
        top_k: int,
        rrf_k: int = 60,
    ) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion：融合多 Agent 结果，去重并排序。

        算法:
            RRF_score(doc) = Σ 1 / (k + rank_i(doc))
            其中 rank_i 是文档在第 i 个 Agent 结果中的排名（1-indexed）。

        参数:
            all_results: 所有 Agent 的原始结果列表。
            top_k: 最终返回数。
            rrf_k: RRF 平滑参数（默认 60）。

        返回:
            融合后的结果列表。
        """
        if not all_results:
            return []

        # 1) 按 source_agent 分组，组内按 score 降序排名
        agent_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for r in all_results:
            aid = r.get("source_agent", "unknown")
            agent_buckets.setdefault(aid, []).append(r)

        for aid in agent_buckets:
            agent_buckets[aid].sort(key=lambda x: -x.get("score", 0))

        # 2) 计算 RRF 分数 + 内容去重（基于 content 前 80 字符模糊匹配）
        rrf_scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}
        seen_contents: set[str] = set()

        for aid, ranked in agent_buckets.items():
            for rank_idx, doc in enumerate(ranked):
                # 去重：content 前 80 字符归一化
                content = doc.get("content", "")
                content_key = content[:80].strip().lower()
                if content_key in seen_contents:
                    continue
                seen_contents.add(content_key)

                rrf = 1.0 / (rrf_k + rank_idx + 1)
                doc_id = doc.get("id", f"{aid}_{rank_idx}")

                if doc_id in rrf_scores:
                    rrf_scores[doc_id] += rrf
                else:
                    rrf_scores[doc_id] = rrf
                    doc_map[doc_id] = doc

        # 3) 按 RRF 分数降序排列
        sorted_ids = sorted(rrf_scores.keys(),
                            key=lambda did: -rrf_scores[did])

        # 4) 组装结果
        fused = []
        for did in sorted_ids[:top_k]:
            doc = doc_map[did]
            doc["rrf_score"] = round(rrf_scores[did], 6)
            fused.append(doc)

        return fused

    # ── 能力查询 ──────────────────────────────────────────────────────

    def get_agent_capabilities(self) -> Dict[str, Dict[str, Any]]:
        """返回各 Agent 的能力摘要，供查询路由与 UI 展示使用。

        返回:
            {agent_id: {"name": ..., "capabilities": [...],
                        "status": ..., "description": ...}, ...}
        """
        result: Dict[str, Dict[str, Any]] = {}

        if self._use_a2a:
            agents = self._registry.discover()
            for a in agents:
                result[a.agent_id] = {
                    "name": a.name,
                    "capabilities": a.capabilities,
                    "status": a.status,
                    "version": a.version,
                    "endpoint": a.endpoint,
                }
        else:
            for aid, info in self._standalone_agents.items():
                result[aid] = {
                    "name": info["name"],
                    "capabilities": info["capabilities"],
                    "status": "standalone",
                    "description": info.get("description", ""),
                }

        return result

    # ── 智能路由 ──────────────────────────────────────────────────────

    def route_query(self, query: str) -> List[str]:
        """根据查询内容自动判断应查询哪些 Agent。

        策略:
            对查询文本分词，与 ROUTE_KEYWORDS 中每个 Agent 的关键词表
            进行匹配。命中任一关键词即将该 Agent 加入目标列表。

        参数:
            query: 查询文本。

        返回:
            建议查询的 Agent ID 列表。
        """
        query_lower = query.lower()
        targets: set[str] = set()

        for aid, keywords in self.ROUTE_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in query_lower:
                    targets.add(aid)
                    break  # 命中一个关键词即可

        # 如果无命中，默认查询所有 Agent
        if not targets:
            targets = set(self._get_all_agent_ids())

        # 过滤掉不可用的 Agent
        available = set(self._get_all_agent_ids())
        result = [aid for aid in targets if aid in available]

        logger.debug(
            "route_query '%s' → %s", query[:60], result,
        )
        return result

    # ── 统计接口 ──────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, int]:
        """返回联邦查询统计信息。"""
        with self._lock:
            return dict(self._stats)

    # ── 独立注册接口 ──────────────────────────────────────────────────

    def register_standalone_agent(
        self,
        agent_id: str,
        name: str,
        capabilities: List[str],
        description: str = "",
    ) -> None:
        """向独立注册表添加 Agent（仅无 A2A 注册表时有效）。

        参数:
            agent_id: Agent 唯一标识符。
            name: 显示名称。
            capabilities: 能力标签列表。
            description: 描述文本。
        """
        if self._use_a2a:
            logger.warning(
                "register_standalone_agent ignored: A2A registry is active"
            )
            return
        self._standalone_agents[agent_id] = {
            "name": name,
            "capabilities": capabilities,
            "description": description,
        }

    def unregister_standalone_agent(self, agent_id: str) -> bool:
        """从独立注册表移除 Agent。"""
        if self._use_a2a:
            return False
        return self._standalone_agents.pop(agent_id, None) is not None

    # ── 内部辅助 ──────────────────────────────────────────────────────

    def _get_all_agent_ids(self) -> List[str]:
        """获取所有可用 Agent 的 ID 列表。"""
        if self._use_a2a:
            agents = self._registry.discover()
            return [a.agent_id for a in agents if a.status == "active"]
        return list(self._standalone_agents.keys())

    def _resolve_agents(self, agent_filter: List[str]) -> List[str]:
        """解析 agent_filter 为目标 Agent ID 列表。"""
        available = set(self._get_all_agent_ids())
        return [aid for aid in agent_filter if aid in available]
