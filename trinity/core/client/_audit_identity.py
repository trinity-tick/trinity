"""Trinity client - audit trail, identity & governance mixin (split from client.py, 2026-08-17).

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
# Preserve the old single-file __file__ semantics: export_replay_report uses
# os.path.dirname(os.path.dirname(__file__)) to reach <repo>/trinity/output.
# (the original module lived at trinity/core/client.py)
__file__ = os.path.join(os.path.dirname(os.path.dirname(__file__)), "client.py")

class _AuditIdentityMixin:
    def get_audit_trail(self, memory_id: str) -> List[Dict[str, Any]]:
        """查看某条记忆的完整变更历史（审计轨迹）。"""
        if self._adapter and hasattr(self._adapter, "get_audit_trail"):
            return self._adapter.get_audit_trail(memory_id)
        return []
    def replay_session(self, agent_id: str,
                        start_time: str = None,
                        end_time: str = None) -> List[Dict[str, Any]]:
        """回放某 Agent 在时间段内的所有操作。"""
        if self._adapter and hasattr(self._adapter, "replay_agent_session"):
            return self._adapter.replay_agent_session(agent_id, start_time, end_time)
        return []
    def verify_integrity(self) -> Dict[str, Any]:
        """验证审计链完整性，检测篡改。"""
        if self._adapter and hasattr(self._adapter, "verify_audit_integrity"):
            return self._adapter.verify_audit_integrity()
        return {"integrity_ok": False, "error": "no adapter"}
    def audit_summary(self, start_time: str = None,
                       end_time: str = None) -> Dict[str, Any]:
        """审计摘要：各操作计数、活跃 Agent、峰值时段。"""
        if self._adapter and hasattr(self._adapter, "get_audit_summary"):
            return self._adapter.get_audit_summary(start_time, end_time)
        return {"error": "no adapter"}
    def audit_timeline(self, agent_id: str = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """最近操作时间线。"""
        if self._adapter and hasattr(self._adapter, "replay_agent_session"):
            session = self._adapter.replay_agent_session(agent_id) if agent_id else []
            return session[-limit:]
        return []
    def export_replay_report(self, agent_id: str,
                               start_time: str = None,
                               end_time: str = None,
                               format: str = "markdown") -> str:
        """导出回放报告为 Markdown 文件，返回报告路径。"""
        import os
        session = self.replay_session(agent_id, start_time, end_time)
        if not session:
            return ""
        lines = [
            f"# Agent 记忆回放报告",
            f"",
            f"**Agent ID**: `{agent_id}`",
            f"**时间范围**: {start_time or '(不限)'} ~ {end_time or '(不限)'}",
            f"**总操作数**: {len(session)}",
            f"**导出时间**: {__import__('datetime').datetime.now().isoformat()}",
            f"",
            f"---",
            f"",
        ]
        for i, entry in enumerate(session, 1):
            lines.append(f"## {i}. {entry.get('action', 'unknown').upper()}")
            lines.append(f"- **时间**: {entry.get('timestamp', '')}")
            lines.append(f"- **记忆 ID**: {entry.get('memory_id', 'N/A')}")
            lines.append(f"- **Agent**: {entry.get('agent_id', 'N/A')}")
            lines.append(f"- **Persona**: {entry.get('persona_id', 'N/A')}")
            details = entry.get("details", {})
            if details:
                import json
                lines.append(f"- **详情**:")
                lines.append(f"```json")
                lines.append(json.dumps(details, ensure_ascii=False, indent=2))
                lines.append(f"```")
            lines.append("")

        report_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "output",
            f"replay_report_{agent_id}_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        )
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return report_path
    def register_identity_anchor(self, agent_id: str, anchor_type: str,
                                  content: str) -> Dict[str, Any]:
        """注册或更新身份锚点。"""
        if self._adapter and hasattr(self._adapter, "upsert_anchor"):
            return self._adapter.upsert_anchor(agent_id, anchor_type, content)
        return {"error": "no adapter"}
    def get_identity_profile(self, agent_id: str) -> Dict[str, Any]:
        """获取完整身份画像（含一致性分数）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.reconstruct_identity(agent_id)
        return {"error": "no adapter"}
    def reconstruct_identity(self, agent_id: str,
                              available_anchors: List[str] = None) -> Dict[str, Any]:
        """从锚点重建 Agent 身份画像。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            if available_anchors:
                return mgr.partial_reconstruct(agent_id, available_anchors)
            return mgr.reconstruct_identity(agent_id)
        return {"error": "no adapter"}
    def detect_drift(self, agent_id: str) -> Dict[str, Any]:
        """检测身份漂移（对比当前行为与基线锚点）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.detect_identity_drift(agent_id)
        return {"error": "no adapter"}
    def export_identity(self, agent_id: str) -> Dict[str, Any]:
        """导出完整身份包（可用于 Agent 迁移）。"""
        if self._adapter and hasattr(self._adapter, "get_all_anchors"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.export_identity_bundle(agent_id)
        return {"error": "no adapter"}
    def import_identity(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        """导入身份包。"""
        if self._adapter and hasattr(self._adapter, "upsert_anchor"):
            from trinity.identity.identity_manager import IdentityManager
            mgr = IdentityManager(self._adapter)
            return mgr.import_identity_bundle(bundle)
        return {"error": "no adapter"}
    @property
    def _dcsa_auditor(self):
        """惰性初始化 DCSA Auditor。"""
        if not hasattr(self, "_dcsa_auditor_inst"):
            from trinity.audit.auditor import Auditor
            self._dcsa_auditor_inst = Auditor(
                adapter=self._adapter if hasattr(self, '_adapter') else None,
            )
        return self._dcsa_auditor_inst
    def audit_action(self, agent_id: str, task: str = "",
                     executor_result: str = "{}") -> Dict[str, Any]:
        """执行一次双循环审计（executor + auditor 独立审查）。"""
        auditor = self._dcsa_auditor
        return auditor.audit_action({
            "agent_id": agent_id, "task": task,
            "executor_result": executor_result,
        })
    def get_audit_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取 DCSA-EJP 审计运行历史。"""
        if self._adapter and hasattr(self._adapter, "get_audit_history"):
            return self._adapter.get_audit_history(agent_id, limit)
        return []
    def get_violations(self, agent_id: str = None,
                        limit: int = 100) -> List[Dict[str, Any]]:
        """获取宪法违规趋势。"""
        if self._adapter and hasattr(self._adapter, "get_violation_trends"):
            return self._adapter.get_violation_trends(agent_id, limit)
        return []
    def get_dcsa_metrics(self) -> Dict[str, Any]:
        """获取 DCSA-EJP 六项指标实时值。"""
        return self._dcsa_auditor.get_metrics()
