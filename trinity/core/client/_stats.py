"""Trinity client - stats, conflicts, weights & benchmarks mixin (split from client.py, 2026-08-17).

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
class _StatsMixin:
    def set_agent_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """设置 Agent 检索权重。

        Args:
            agent_id: Agent 标识（如 'file-agent'、'browser'）。
            weight: 权重值（建议 0.1-2.0）。

        Returns:
            操作结果。
        """
        if self._adapter and hasattr(self._adapter, "set_agent_weight"):
            return self._adapter.set_agent_weight(agent_id, weight)
        return {"error": "Adapter does not support agent weights"}
    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 权重配置。

        Returns:
            Dict[agent_id, weight]
        """
        if self._adapter and hasattr(self._adapter, "get_agent_weights"):
            return self._adapter.get_agent_weights()
        return {}
    def delete_agent_weight(self, agent_id: str) -> bool:
        """删除 Agent 权重配置。

        Args:
            agent_id: Agent 标识。

        Returns:
            是否删除成功。
        """
        if self._adapter and hasattr(self._adapter, "delete_agent_weight"):
            return self._adapter.delete_agent_weight(agent_id)
        return False
    def stats(self) -> Dict[str, Any]:
        """返回记忆统计信息（总数、过期数、Agent 分布、平均访问频率等）。

        Returns:
            Stats dict.
        """
        if self._adapter:
            return self._adapter.get_memory_stats()
        return {"error": "no adapter"}
    def modality_stats(self) -> Dict[str, Any]:
        """返回各模态记忆数量、存储占比统计。

        Returns:
            Dict with total_active, modalities, percentages.
        """
        if self._adapter:
            return self._adapter.get_modality_stats()
        return {"error": "no adapter"}
    def get_conflicts(self, memory_id: str) -> Dict[str, Any]:
        """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。

        Args:
            memory_id: 记忆 ID。

        Returns:
            冲突链信息，包含 conflict_group_id 与所有冲突版本列表。
        """
        if self._adapter:
            return self._adapter.get_conflicts(memory_id)
        return {"memory_id": memory_id, "conflicts": [], "error": "no adapter"}
    def resolve_conflict(
        self, conflict_group_id: str, keep_memory_id: str
    ) -> Dict[str, Any]:
        """解决冲突：保留选定版本，软删除同一冲突组的其他版本。

        Args:
            conflict_group_id: 冲突组 ID。
            keep_memory_id: 保留的记忆 ID。

        Returns:
            操作结果，含 resolved_count 与 discarded_ids。
        """
        if self._adapter:
            result = self._adapter.resolve_conflict(conflict_group_id, keep_memory_id)
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=keep_memory_id, action="resolve", agent_id=None,
                        persona_id=None,
                        details={"conflict_group_id": conflict_group_id,
                                 "resolved_count": result.get("resolved_count", 0)},
                    )
                except Exception:
                    pass
            return result
        return {"error": "no adapter", "resolved_count": 0}
    def dedup_stats(self) -> Dict[str, Any]:
        """返回去重统计信息（冲突组数、已解决数等）。

        Returns:
            Dedup stats dict.
        """
        if self._adapter:
            return self._adapter.dedup_stats()
        return {"error": "no adapter"}
    def benchmark(self, name: str = "longmemeval",
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        from trinity.benchmark.runner import run_benchmark
        return run_benchmark(name, config)
