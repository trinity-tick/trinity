"""Trinity client - advanced cognition (SAGE/DCPM/personalization/compression) & proactive push mixin (split from client.py, 2026-08-17).

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
class _AdvancedMixin:
    def proactive_push(
        self,
        memory_ids: List[str],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """基于当前上下文记忆，主动推送关联记忆。

        策略：
        1. 收集本次涉及的所有记忆 ID。
        2. 查询这些记忆的 memory_links，按 strength 降序取 top-k。
        3. 排除已在请求记忆列表中的条目，去重。
        4. 应用时间衰减（半衰期 30 天）。

        Args:
            memory_ids: 当前上下文中的记忆 ID 列表。
            top_k: 最大推送条数（默认 5）。

        Returns:
            推送记忆列表，每条含 push_reason (link_type + strength) 和 push_score。
        """
        import math
        from datetime import datetime, timezone

        if not self._adapter or not hasattr(self._adapter, "get_linked_memories"):
            return []

        seen: set = set(memory_ids)
        pushed: Dict[str, Dict[str, Any]] = {}
        now = datetime.now(timezone.utc)

        for mid in memory_ids:
            if not mid:
                continue
            try:
                links = self._adapter.get_linked_memories(mid, min_strength=0.3)
            except Exception:
                continue
            for link in links:
                target_id = link.get("target_id", "")
                if not target_id or target_id in seen:
                    continue
                # 2026-08-15（二轮压测修复）：strength 可能为 None
                # （旧数据/部分链接未填）→ float(None) 崩溃；兜底 0.5。
                strength = float(link.get("strength") or 0.5)
                link_type = link.get("link_type", "semantic")

                # 时间衰减
                push_score = float(strength)
                created_at = link.get("created_at", "")
                if created_at:
                    try:
                        if isinstance(created_at, str):
                            for fmt in [
                                "%Y-%m-%dT%H:%M:%S.%f%z",
                                "%Y-%m-%dT%H:%M:%S%z",
                                "%Y-%m-%dT%H:%M:%S.%f",
                                "%Y-%m-%dT%H:%M:%S",
                                "%Y-%m-%d %H:%M:%S.%f",
                                "%Y-%m-%d %H:%M:%S",
                            ]:
                                try:
                                    dt = datetime.strptime(created_at, fmt)
                                    break
                                except ValueError:
                                    continue
                            else:
                                dt = None
                        else:
                            dt = created_at
                        if dt:
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            days_since = (now - dt).total_seconds() / 86400.0
                            if self.push_half_life_days > 0:
                                push_score *= math.pow(
                                    2, -days_since / self.push_half_life_days
                                )
                    except Exception:
                        pass

                # 优先保留强度更高的
                if target_id not in pushed or push_score > pushed[target_id].get("push_score", 0):
                    pushed[target_id] = {
                        "memory_id": target_id,
                        "target_content": link.get("target_content", ""),
                        "push_reason": {
                            "link_type": link_type,
                            "strength": strength,
                            "source_id": mid,
                        },
                        "push_score": round(push_score, 6),
                    }

        # 按 push_score 降序，取 top_k
        sorted_pushed = sorted(
            pushed.values(), key=lambda x: x["push_score"], reverse=True
        )
        return sorted_pushed[:top_k]
    @property
    def personalization(self):
        """惰性获取 PAHFEngine（不可用时返回 None）。"""
        if self._personalization is None:
            try:
                from trinity.modules.second_brain.personalization_engine import PAHFEngine
                self._personalization = PAHFEngine()
            except Exception:
                self._personalization = None
        return self._personalization
    def get_preference_context(
        self, user_id: str, domain: str = "search",
    ) -> Dict[str, Any]:
        """获取用户偏好上下文（供检索/行动注入）。

        Args:
            user_id: 用户标识（persona_id）。
            domain: 偏好领域（search/style/format...）。

        Returns:
            {"user_id": ..., "preferences": [...], "enabled": bool}
        """
        eng = self.personalization
        if eng is None:
            return {"user_id": user_id, "preferences": [], "enabled": False}
        try:
            from trinity.modules.second_brain.personalization_engine import PreferenceDomain
            dom = PreferenceDomain(domain) if domain in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            prefs = eng.retriever.retrieve(user_id, dom)
            entries = [
                {"content": e.value, "confidence": e.confidence,
                 "key": e.key,
                 "domain": e.domain.value if hasattr(e.domain, "value") else str(e.domain)}
                for e in (prefs or {}).values()
            ]
            return {"user_id": user_id, "preferences": entries, "enabled": True}
        except Exception:
            return {"user_id": user_id, "preferences": [], "enabled": False}
    def integrate_feedback(
        self, user_id: str, feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        """整合用户反馈到偏好记忆（行动后）。

        Args:
            user_id: 用户标识。
            feedback: {"content": 偏好内容, "domain": 领域,
                       "feedback_type": "explicit_confirm|explicit_correction", ...}

        Returns:
            {"integrated": bool, "preference_count": int}
        """
        eng = self.personalization
        if eng is None:
            return {"integrated": False, "preference_count": 0}
        try:
            content = feedback.get("content", "")
            if not content:
                return {"integrated": False, "preference_count": 0}
            from trinity.modules.second_brain.personalization_engine import (
                FeedbackRecord, FeedbackType, PreferenceDomain,
            )
            domain_str = feedback.get("domain", "search")
            dom = PreferenceDomain(domain_str) if domain_str in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            fb_type_str = feedback.get("feedback_type", "explicit_confirm")
            fb_type = FeedbackType(fb_type_str) if fb_type_str in {f.value for f in FeedbackType} \
                else FeedbackType.EXPLICIT_CONFIRM
            record = FeedbackRecord(
                user_id=user_id,
                feedback_type=fb_type,
                raw_response=content,
                preference_changes=[{"domain": dom.value, "key": content[:60],
                                     "value": content, "confidence": 0.6}],
            )
            result = eng.integrator.integrate_feedback(
                user_id=user_id, feedback=record, retriever=eng.retriever,
            )
            return {
                "integrated": True,
                "changes": result.get("changes_made", 0),
                "drift_detected": result.get("drift_detected", False),
            }
        except Exception:
            return {"integrated": False, "preference_count": 0}
    def should_clarify(
        self, user_id: str, action_context: str, domain: str = "search",
    ) -> bool:
        """判断行动前是否需要澄清偏好。"""
        eng = self.personalization
        if eng is None or not action_context.strip():
            return False
        try:
            from trinity.modules.second_brain.personalization_engine import PreferenceDomain
            dom = PreferenceDomain(domain) if domain in {d.value for d in PreferenceDomain} \
                else PreferenceDomain.SEARCH
            return eng.should_clarify(user_id, action_context, dom)
        except Exception:
            return False
    @property
    def sage(self):
        if not hasattr(self, "_sage") or self._sage is None:
            try:
                from trinity.modules.second_brain.sage_graph_memory_engine import (
                    SAGEGraphMemoryEngine,
                )
                eng = SAGEGraphMemoryEngine()
                # 2026-09 (EXECUTION 125): 从 PG 快照恢复（跨进程图记忆）
                try:
                    eng.restore_snapshot()
                except Exception:
                    pass
                self._sage = eng
            except Exception:
                self._sage = None
        return self._sage
    def sage_ingest(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """摄入一轮交互到 SAGE 图记忆（MindMemOS 自进化）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False}
        try:
            return {**eng.ingest_turn(content, metadata), "sage": True}
        except Exception:
            return {"sage": False}
    def sage_query(self, query_text: str) -> Dict[str, Any]:
        """SAGE 图检索（实体/关系/证据路径）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False, "entities": [], "relations": []}
        try:
            return {**eng.query(query_text), "sage": True}
        except Exception:
            return {"sage": False, "entities": [], "relations": []}
    def brain_capabilities(self) -> Dict[str, Any]:
        """2026-09 (EXECUTION 172): 大脑方向能力注册表——列出全部已激活
        的认知/记忆模块及其可用性（DSH/脚本可按需调用）。"""
        caps = {}
        for name, mod in [
            ("causal_memory", "trinity.modules.second_brain.causal_memory"),
            ("causal_semantic_graph", "trinity.modules.second_brain.causal_semantic_graph_memory"),
            ("consensus_voting", "trinity.modules.second_brain.consensus_voting"),
            ("contextual_embedding", "trinity.modules.second_brain.contextual_embedding"),
            ("engine_memory_core", "trinity.modules.second_brain.engine_memory_core"),
            ("engine_memory_tiers", "trinity.modules.second_brain.engine_memory_tiers"),
            ("federated_memory", "trinity.modules.second_brain.federated_memory"),
            ("memory_page_manager", "trinity.modules.second_brain.memory_page_manager"),
            ("proactive_prefetcher", "trinity.modules.second_brain.proactive_prefetcher"),
            ("prompt_ingestion", "trinity.modules.second_brain.prompt_ingestion"),
            ("reflective_repair_memory", "trinity.modules.second_brain.reflective_repair_memory"),
            ("selective_recall", "trinity.modules.second_brain.selective_recall"),
            ("structured_distillation", "trinity.modules.second_brain.structured_distillation_compressor"),
            ("workflow_memory", "trinity.modules.second_brain.workflow_memory"),
        ]:
            try:
                __import__(mod)
                caps[name] = {"available": True}
            except Exception as e:
                caps[name] = {"available": False, "error": str(e)[:60]}
        return {"capabilities": caps, "count": sum(1 for v in caps.values() if v.get("available"))}

    def sage_evolve(self) -> Dict[str, Any]:
        """触发 SAGE 自进化轮（图结构调整）。"""
        eng = self.sage
        if eng is None:
            return {"sage": False}
        try:
            return {**eng.evolve(), "sage": True}
        except Exception:
            return {"sage": False}
    @property
    def dcpm(self):
        if self._dcpm is None:
            try:
                from trinity.modules.second_brain.dcpm_dual_process_memory import (
                    System1DaytimeWriter, System2NighttimeEngine,
                )
                self._dcpm = {
                    "system1": System1DaytimeWriter(),
                    "system2": System2NighttimeEngine(),
                }
            except Exception:
                self._dcpm = None
        return self._dcpm
    def dcpm_record_belief(self, subject: str, predicate: str, obj: str,
                           superseded_by: Optional[str] = None) -> Dict[str, Any]:
        """System1 记录信念（含修订链，快路径）。"""
        eng = self.dcpm
        if eng is None:
            return {"dcpm": False}
        try:
            import uuid as _uuid
            from trinity.modules.second_brain.dcpm_dual_process_memory import BeliefRevisionNode
            node = BeliefRevisionNode(
                belief_id=_uuid.uuid4().hex[:12],
                subject=subject, predicate=predicate,
                object=obj, superseded_by=superseded_by,
            )
            stored = eng["system1"].record_belief(node)
            return {"dcpm": True, "belief_id": stored.belief_id,
                    "chain_len": len(eng["system1"].get_chain(stored.belief_id))}
        except Exception:
            return {"dcpm": False}
    def dcpm_consolidate(self) -> Dict[str, Any]:
        """System2 夜间整合：schema 归纳 + 冲突检测（慢路径）。"""
        eng = self.dcpm
        if eng is None:
            return {"dcpm": False, "schemas": 0, "collisions": 0}
        try:
            beliefs = list(eng["system1"]._beliefs.values())
            schemas = eng["system2"].induce_schemas(beliefs)
            collisions = 0
            for i in range(len(schemas)):
                for j in range(i + 1, len(schemas)):
                    if eng["system2"].detect_collisions(schemas[i], schemas[j]):
                        collisions += 1
            return {"dcpm": True, "schemas": len(schemas), "collisions": collisions}
        except Exception:
            return {"dcpm": False, "schemas": 0, "collisions": 0}
    @property
    def compressor(self):
        """MemoryCompressor 实例（延迟初始化）。"""
        return self._ensure_compressor()
    def _ensure_compressor(self):
        """Lazy-initialize the MemoryCompressor."""
        if self._compressor is None:
            from trinity.memory.compression import MemoryCompressor
            self._compressor = MemoryCompressor(
                trinity_instance=self,
                max_tokens=4096,
                compression_threshold=0.8,
            )
        return self._compressor
    def compress_context(
        self,
        agent_id: str,
        max_tokens: int = 4096,
        no_compress: bool = False,
    ) -> Dict[str, Any]:
        """Compress agent context using the compression pipeline.

        Parameters
        ----------
        agent_id : str
            Agent whose context is being compressed.
        max_tokens : int
            Target token budget ceiling.
        no_compress : bool
            Set True to skip compression entirely (passthrough).

        Returns
        -------
        dict with the compressed context result.
        """
        if no_compress:
            return {
                "status": "skipped",
                "reason": "no_compress flag",
                "agent_id": agent_id,
            }

        # Gather all memories for this agent
        memories = []
        if self._adapter and hasattr(self._adapter, "get_all_memories"):
            try:
                memories = self._adapter.get_all_memories(
                    agent_id=agent_id,
                    limit=10000,
                ) or []
            except Exception:
                pass

        if hasattr(self._adapter, "search_memories"):
            try:
                extra = self._adapter.search_memories(
                    query="",
                    agent_id=agent_id,
                    top_k=1000,
                ) or []
                seen = {m.get("memory_id") for m in memories}
                for m in extra:
                    if m.get("memory_id") not in seen:
                        memories.append(m)
            except Exception:
                pass

        compressor = self._ensure_compressor()
        compressor.max_tokens = max_tokens
        result = compressor.compress(agent_id, memories)
        return result.to_dict()
