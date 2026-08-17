"""Trinity client - agent-to-agent (A2A) features mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
class _A2AMixin:
    @property
    def a2a(self):
        """获取 A2A 记忆同步引擎（惰性初始化）"""
        if not hasattr(self, "_a2a_sync"):
            self._a2a_sync = None
        if self._a2a_sync is None:
            from trinity.a2a_memory import A2AMemorySync
            self._a2a_sync = A2AMemorySync(
                local_agent_id=f"trinity-{self.tenant_id}",
                local_store=self._a2a_store_callback,
                local_search=self._a2a_search_callback,
            )
        return self._a2a_sync
    def _a2a_store_callback(self, entry) -> bool:
        """A2A 存储回调：将远端记忆写入本地"""
        try:
            self.ingest(
                content=entry.content,
                persona_id=entry.persona_id,
                tags=entry.tags,
                importance=entry.importance,
            )
            return True
        except Exception as e:
            return False
    def _a2a_search_callback(self, query: str, top_k: int = 10) -> list:
        """A2A 搜索回调：搜索本地记忆供远端查询"""
        try:
            return self.search(query, top_k=top_k)
        except Exception:
            return []
    def share_memory(self, content: str, persona_id: str = "default",
                     tags: list = None, importance: float = 0.5) -> dict:
        """将一条记忆共享给所有在线 Trinity 实例"""
        from trinity.a2a_memory import create_memory_entry
        entry = create_memory_entry(
            content=content,
            persona_id=persona_id,
            source_agent=f"trinity-{self.tenant_id}",
            importance=importance,
            tags=tags or [],
        )
        results = self.a2a.share_to_all(entry)
        return {
            "memory_id": entry.memory_id,
            "shared_to": len(results),
            "results": [{"peer": r.peer, "success": r.success} for r in results],
        }
    def sync_from_peers(self, query: str = "") -> dict:
        """从所有在线实例同步记忆"""
        results = self.a2a.sync_all(query=query)
        return {
            "peers_contacted": len(results),
            "total_entries": sum(r.entries_count for r in results),
            "results": [{"peer": r.peer, "entries": r.entries_count, "success": r.success} for r in results],
        }
    def search_peers(self, query: str, top_k: int = 10) -> dict:
        """搜索所有在线实例的记忆"""
        results = self.a2a.search_peers(query, top_k=top_k)
        return {
            "query": query,
            "peers_found": len(results),
            "results": results,
        }
    @property
    def _a2a_registry(self):
        """惰性初始化 A2A CapabilityRegistry。"""
        if not hasattr(self, "_a2a_registry_inst"):
            from trinity.a2a.capability_registry import CapabilityRegistry
            self._a2a_registry_inst = CapabilityRegistry(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._a2a_registry_inst
    @property
    def _a2a_task_manager(self):
        """惰性初始化 A2A TaskManager。"""
        if not hasattr(self, "_a2a_task_manager_inst"):
            from trinity.a2a.task_manager import TaskManager
            self._a2a_task_manager_inst = TaskManager(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._a2a_task_manager_inst
    def register_agent_card(self, agent_id: str, name: str,
                             description: str = "", version: str = "1.0.0",
                             capabilities: List[str] = None,
                             endpoints: Dict[str, str] = None,
                             skills: List[Dict[str, Any]] = None,
                             input_modes: List[str] = None,
                             output_modes: List[str] = None,
                             security_level: str = "low") -> Dict[str, Any]:
        """注册 Agent 到 A2A 联邦能力目录。"""
        from trinity.a2a.agent_card import AgentCard, SkillDef
        caps = capabilities or []
        eps = endpoints or {}
        skill_objs = [SkillDef(name=s.get("name", ""), description=s.get("description", ""),
                                input_schema=s.get("input_schema", {}), output_schema=s.get("output_schema", {}),
                                examples=s.get("examples", []))
                       for s in (skills or [])]
        card = AgentCard(
            agent_id=agent_id, name=name, description=description,
            version=version, capabilities=caps, endpoints=eps,
            skills=skill_objs,
            input_modes=input_modes or ["text"],
            output_modes=output_modes or ["text"],
            security_level=security_level,
        )
        return self._a2a_registry.register_agent(card)
    def get_agent_card(self, agent_id: str) -> Dict[str, Any]:
        """获取 Agent 能力卡片。"""
        if self._adapter and hasattr(self._adapter, "get_agent_card"):
            return self._adapter.get_agent_card(agent_id) or {}
        return {}
    def unregister_agent(self, agent_id: str) -> Dict[str, Any]:
        """注销 Agent。"""
        return self._a2a_registry.unregister_agent(agent_id)
    def list_a2a_agents(self) -> Dict[str, Any]:
        """列出所有注册的 Agent。"""
        if self._adapter and hasattr(self._adapter, "get_agent_card"):
            return self._a2a_registry.list_all_agents()
        return {"agents": [], "total": 0}
    def create_a2a_task(self, task_id: str, from_agent: str,
                         to_agent: str, payload: str = "{}",
                         status: str = "pending",
                         result: Optional[str] = None) -> Dict[str, Any]:
        """创建跨 Agent 任务。"""
        if self._adapter and hasattr(self._adapter, "create_a2a_task"):
            ok = self._adapter.create_a2a_task(
                task_id, from_agent, to_agent, payload, status, result)
            return {"status": "ok" if ok else "error", "task_id": task_id}
        return {"error": "no adapter"}
    def query_a2a_task(self, task_id: str) -> Dict[str, Any]:
        """查询跨 Agent 任务状态。"""
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            tasks = self._adapter.list_a2a_tasks(task_id=task_id)
            return tasks[0] if tasks else {}
        return {}
    def update_a2a_task(self, task_id: str, status: str,
                         result: Optional[str] = None) -> Dict[str, Any]:
        """更新跨 Agent 任务状态。"""
        if self._adapter and hasattr(self._adapter, "update_a2a_task"):
            ok = self._adapter.update_a2a_task(task_id, status, result)
            return {"status": "ok" if ok else "error", "task_id": task_id}
        return {"error": "no adapter"}
    def list_a2a_tasks(self, agent_id: str = None,
                        status: str = None) -> List[Dict[str, Any]]:
        """列出跨 Agent 任务。"""
        if self._adapter and hasattr(self._adapter, "list_a2a_tasks"):
            return self._adapter.list_a2a_tasks(agent_id=agent_id, status=status)
        return []
    def send_a2a_message(self, from_agent: str, to_agent: str,
                          method: str, params: Dict[str, Any] = None,
                          req_id: str = None) -> Dict[str, Any]:
        """发送 A2A 消息（JSON-RPC 2.0）。"""
        from trinity.a2a.protocol import A2AProtocol
        proto = A2AProtocol()
        return proto.send_message(from_agent, to_agent, method,
                                  params or {}, req_id)
