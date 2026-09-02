"""Trinity client - memory lifecycle CRUD mixin (split from client.py, 2026-08-17).

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
class _CrudMixin:
    def age(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count.
        """
        if self._adapter:
            result = self._adapter.age_memories()
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=None, action="age", agent_id="system",
                        persona_id=None,
                        details={"aged_count": result.get("aged_count", 0)},
                    )
                except Exception:
                    pass
            return result
        return {"aged_count": 0, "error": "no adapter"}
    def touch(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。

        Args:
            memory_id: 记忆 ID。

        Returns:
            是否更新成功。
        """
        if self._adapter:
            return self._adapter.touch_memory(memory_id)
        return False
    def get_persona_memories(
        self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if self._adapter:
            return self._adapter.get_persona_memories(persona_id, agent_id=agent_id, limit=limit)
        return self.bridge("diagnostics").get("storage", {})
    def delete_memory(self, memory_id: str) -> bool:
        if self._adapter:
            result = self._adapter.delete_memory(memory_id)
            # ANN 增量维护（①落盘持久化）：后台移除索引条目
            if result and self.use_ann:
                import threading as _th
                _th.Thread(
                    target=self._ann_incremental_remove, args=(memory_id,),
                    daemon=True,
                ).start()
            # 自动审计日志
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=memory_id, action="delete", agent_id=None,
                        persona_id=None,
                        details={"success": result},
                    )
                except Exception:
                    pass
            return result
        return True
    def purge_memory(
        self, memory_id: str, confirm: bool = False, reason: str = "",
        agent_id: str = "requested", persona_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GDPR 硬擦除（2026-09-02, Fable 对照审计 P2-⑤⑦）。不可逆操作门禁。"""
        if not confirm:
            return {
                "memory_id": memory_id, "purged": False,
                "error": "confirm_required",
                "hint": "irreversible: pass confirm=True to permanently erase content",
            }
        if not self._adapter or not hasattr(self._adapter, "purge_memory"):
            return {"memory_id": memory_id, "purged": False, "error": "unsupported"}
        result = self._adapter.purge_memory(memory_id, reason=reason)
        if result.get("purged"):
            if self.use_ann:
                import threading as _th
                _th.Thread(target=self._ann_incremental_remove,
                           args=(memory_id,), daemon=True).start()
            try:
                from trinity.core.cache import get_cache
                get_cache().invalidate(pattern="*")
            except Exception:
                pass
            if hasattr(self._adapter, "write_audit_log"):
                try:
                    self._adapter.write_audit_log(
                        memory_id=memory_id, action="HARD_PURGE",
                        agent_id=agent_id, persona_id=persona_id,
                        details={"reason": (reason or "")[:200],
                                 "prior_sha256": result.get("prior_sha256"),
                                 "status": result.get("status")},
                    )
                except Exception:
                    pass
        return result
    def update_memory(
        self,
        memory_id: str,
        new_content: str,
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update memory (conflict-preserving versioning).

        Old version rows are retained in the audit chain; the memories row is
        bumped to version + 1 with a recomputed SHA-256 hash.

        Returns:
            Dict with memory_id, old_version, new_version, sha256_hash,
            timestamp and status.

        Raises:
            ValueError: If memory_id not found or adapter lacks update support.
        """
        if not self._adapter or not hasattr(self._adapter, "update_memory"):
            raise ValueError(
                f"update_memory not supported by adapter: {type(self._adapter).__name__}"
            )
        current = self._adapter.get_memory(memory_id)
        old_version = current.get("version", 0) if current else 0
        result = self._adapter.update_memory(
            memory_id=memory_id,
            content=new_content,
            importance=importance,
            tags=tags,
            category=category,
        )
        if result is None:
            raise ValueError(f"Memory not found: {memory_id}")
        # ANN 增量维护（①落盘持久化）：内容变更 → 后台更新索引条目
        if self.use_ann:
            import threading as _th
            _th.Thread(
                target=self._ann_incremental_add,
                args=(memory_id, new_content), daemon=True,
            ).start()
        return {
            "memory_id": memory_id,
            "old_version": old_version,
            "new_version": result.get("version"),
            "sha256_hash": result.get("sha256_hash"),
            "timestamp": result.get("updated_at"),
            "status": result.get("status"),
        }
    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        if self._adapter:
            return self._adapter.get_version_chain(memory_id)
        return []
    def switch_tenant(self, tenant_id: str) -> "Trinity":
        self.tenant_id = tenant_id
        return self
