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
            # 2026-09 (EXECUTION 191): 大脑化新机制（181-190 轮）
            ("action_loop", "trinity.brain.action_loop"),
            ("curiosity", "trinity.brain.curiosity"),
            ("predictive_loop", "trinity.brain.predictive_loop"),
            ("sensory_integration", "trinity.brain.sensory_integration"),
            ("emotional_consolidation", "trinity.brain.emotional_consolidation"),
            ("autobiographical", "trinity.brain.autobiographical"),
            ("self_assessment", "trinity.brain.self_assessment"),
            ("cognition_pipeline", "trinity.brain.cognition_pipeline"),
            ("social_memory", "trinity.brain.social_memory"),
            # 2026-09 (EXECUTION 219): 网络方案机制（197-218 轮）
            ("self_axioms", "trinity.brain.self_axioms"),
            ("emotion_axioms", "trinity.brain.emotion_axioms"),
            ("self_prediction", "trinity.brain.self_prediction"),
            ("consciousness_blueprint", "trinity.brain.consciousness_blueprint"),
            ("sensory_integration", "trinity.brain.sensory_integration"),
            ("associative_memory", "trinity.brain.associative_memory"),
            ("reconstructive_memory", "trinity.brain.reconstructive_memory"),
            ("memory_manager", "trinity.brain.memory_manager"),
            ("unknown_awareness", "trinity.brain.unknown_awareness"),
            ("attention_control", "trinity.brain.attention_control"),
            ("theory_of_mind", "trinity.brain.theory_of_mind"),
            ("mental_simulation", "trinity.brain.mental_simulation"),
            ("resource_adaptation", "trinity.brain.resource_adaptation"),
            ("emotion_regulation", "trinity.brain.emotion_regulation"),
            ("proactive_initiative", "trinity.brain.proactive_initiative"),
            ("dopamine_reward", "trinity.brain.dopamine_reward"),
            ("observational_learning", "trinity.brain.observational_learning"),
            ("metamemory", "trinity.brain.metamemory"),
            ("cognitive_flexibility", "trinity.brain.cognitive_flexibility"),
            ("habit_formation", "trinity.brain.habit_formation"),
            # 2026-09 (EXECUTION 240): 219-239 轮机制
            ("self_talk", "trinity.brain.self_talk"),
            ("spatiotemporal_memory", "trinity.brain.spatiotemporal_memory"),
            ("executive_function", "trinity.brain.executive_function"),
            ("emotion_space", "trinity.brain.emotion_space"),
            ("episodic_semantic", "trinity.brain.episodic_semantic"),
            ("sleep_stages", "trinity.brain.sleep_stages"),
            ("episodic_reasoning", "trinity.brain.episodic_reasoning"),
            ("spaced_repetition", "trinity.brain.spaced_repetition"),
            ("regret_learning", "trinity.brain.regret_learning"),
            ("behavioral_contagion", "trinity.brain.behavioral_contagion"),
            ("divergent_thinking", "trinity.brain.divergent_thinking"),
            ("multi_agent_coordination", "trinity.brain.multi_agent_coordination"),
            ("reasoning_bank", "trinity.brain.reasoning_bank"),
            ("prospective_memory", "trinity.brain.prospective_memory"),
            ("surprise_encoding", "trinity.brain.surprise_encoding"),
            ("reflection_loop", "trinity.brain.reflection_loop"),
            ("stale_revocation", "trinity.brain.stale_revocation"),
            # 2026-09 (EXECUTION 297): 281-296 轮机制
            ("context_recovery", "trinity.brain.context_recovery"),
            ("step_confidence", "trinity.brain.step_confidence"),
            ("scheduled_forgetting", "trinity.brain.scheduled_forgetting"),
            ("introspective_reward", "trinity.brain.introspective_reward"),
            ("multifactor_value", "trinity.brain.multifactor_value"),
            ("rate_distortion", "trinity.brain.rate_distortion"),
            ("autobiographical_training", "trinity.brain.autobiographical_training"),
            ("feeling_first", "trinity.brain.feeling_first"),
            ("consciousness_index", "trinity.brain.consciousness_index"),
            ("narrative_memory", "trinity.brain.narrative_memory"),
            ("hybrid_memory", "trinity.brain.hybrid_memory"),
            ("thought_depth", "trinity.brain.thought_depth"),
            ("compression_spectrum", "trinity.brain.compression_spectrum"),
            ("evidence_plasticity", "trinity.brain.evidence_plasticity"),
            ("persistence_loop", "trinity.brain.persistence_loop"),
            ("opponent_awareness", "trinity.brain.opponent_awareness"),
            ("calibration", "trinity.brain.calibration"),
            ("context_sculptor", "trinity.brain.context_sculptor"),
            ("continuous_feedback", "trinity.brain.continuous_feedback"),
            ("dopamine_gated_memory", "trinity.brain.dopamine_gated_memory"),
            ("environment_coevolution", "trinity.brain.environment_coevolution"),
            ("execute_distill_verify", "trinity.brain.execute_distill_verify"),
            ("experience_feedback", "trinity.brain.experience_feedback"),
            ("fast_slow_decision", "trinity.brain.fast_slow_decision"),
            ("foresight_planning", "trinity.brain.foresight_planning"),
            ("generation_timing", "trinity.brain.generation_timing"),
            ("generative_memory", "trinity.brain.generative_memory"),
            ("gist_extraction", "trinity.brain.gist_extraction"),
            ("goal_commitment", "trinity.brain.goal_commitment"),
            ("goal_conditioned_memory", "trinity.brain.goal_conditioned_memory"),
            ("habituation", "trinity.brain.habituation"),
            ("identity_anchors", "trinity.brain.identity_anchors"),
            ("idle_reflection", "trinity.brain.idle_reflection"),
            ("intent_grounding", "trinity.brain.intent_grounding"),
            ("latent_memory", "trinity.brain.latent_memory"),
            ("memory_governance", "trinity.brain.memory_governance"),
            ("memory_index", "trinity.brain.memory_index"),
            ("memory_lineage", "trinity.brain.memory_lineage"),
            ("memory_transaction", "trinity.brain.memory_transaction"),
            ("narrative_continuity", "trinity.brain.narrative_continuity"),
            ("personality_crystallization", "trinity.brain.personality_crystallization"),
            ("pragmatic_curiosity", "trinity.brain.pragmatic_curiosity"),
            ("priority_replay", "trinity.brain.priority_replay"),
            ("social_emotional_learning", "trinity.brain.social_emotional_learning"),
            ("source_credibility", "trinity.brain.source_credibility"),
            ("subjective_perspective", "trinity.brain.subjective_perspective"),
            ("tiered_memory", "trinity.brain.tiered_memory"),
            ("time_awareness", "trinity.brain.time_awareness"),
            ("trait_activation", "trinity.brain.trait_activation"),
            ("world_rehearsal", "trinity.brain.world_rehearsal"),
            ("write_gate", "trinity.brain.write_gate"),
            ("agency_scale", "trinity.brain.agency_scale"),
            ("agent_governance", "trinity.brain.agent_governance"),
            ("conflict_resolution", "trinity.brain.conflict_resolution"),
            ("adaptive_plasticity", "trinity.brain.adaptive_plasticity"),
            ("adversarial_adaptation", "trinity.brain.adversarial_adaptation"),
            ("affect", "trinity.brain.affect"),
            ("affect_state", "trinity.brain.affect_state"),
            ("algorithmic_forgetting", "trinity.brain.algorithmic_forgetting"),
            ("append_only_memory", "trinity.brain.append_only_memory"),
            ("asynchronous_cognition", "trinity.brain.asynchronous_cognition"),
            ("attentional_blink", "trinity.brain.attentional_blink"),
            ("auto_research", "trinity.brain.auto_research"),
            ("autopoiesis", "trinity.brain.autopoiesis"),
            ("autotelic_agency", "trinity.brain.autotelic_agency"),
            ("bayesian_procedural", "trinity.brain.bayesian_procedural"),
            ("belief_collaboration", "trinity.brain.belief_collaboration"),
            ("belief_dynamics", "trinity.brain.belief_dynamics"),
            ("cognitive_quantization", "trinity.brain.cognitive_quantization"),
            ("compositional_generalization", "trinity.brain.compositional_generalization"),
            ("compression", "trinity.brain.compression"),
            ("context_attribution", "trinity.brain.context_attribution"),
            ("critique_learning", "trinity.brain.critique_learning"),
            ("delta_memory", "trinity.brain.delta_memory"),
            ("dream_cycle", "trinity.brain.dream_cycle"),
            ("dual_cognitive_loop", "trinity.brain.dual_cognitive_loop"),
            ("editable_topology", "trinity.brain.editable_topology"),
            ("emotional_valence", "trinity.brain.emotional_valence"),
            ("entropic_memory", "trinity.brain.entropic_memory"),
            ("event_logic_map", "trinity.brain.event_logic_map"),
            ("evolving_world_model", "trinity.brain.evolving_world_model"),
            ("evomind_governance", "trinity.brain.evomind_governance"),
            ("executive_memory", "trinity.brain.executive_memory"),
            ("fractal_cognition", "trinity.brain.fractal_cognition"),
            ("generative_associative", "trinity.brain.generative_associative"),
            ("global_workspace", "trinity.brain.global_workspace"),
            ("hebbian", "trinity.brain.hebbian"),
            ("hela_memory", "trinity.brain.hela_memory"),
            ("homeostatic_affect", "trinity.brain.homeostatic_affect"),
            ("integrated_cognition", "trinity.brain.integrated_cognition"),
            ("iterative_memory_evolution", "trinity.brain.iterative_memory_evolution"),
            ("jit_reinforcement", "trinity.brain.jit_reinforcement"),
            ("joint_exploration", "trinity.brain.joint_exploration"),
            ("knowledge_induction", "trinity.brain.knowledge_induction"),
            ("learn_to_remember", "trinity.brain.learn_to_remember"),
            ("memory_cot", "trinity.brain.memory_cot"),
            ("memory_orchestration", "trinity.brain.memory_orchestration"),
            ("memory_traces", "trinity.brain.memory_traces"),
            ("meta_improvement", "trinity.brain.meta_improvement"),
            ("metacognition", "trinity.brain.metacognition"),
            ("metacognitive_memory", "trinity.brain.metacognitive_memory"),
            ("multi_perspective", "trinity.brain.multi_perspective"),
            ("novelty_gate", "trinity.brain.novelty_gate"),
            ("observability_retention", "trinity.brain.observability_retention"),
            ("perception", "trinity.brain.perception"),
            ("persona_preference_emotion", "trinity.brain.persona_preference_emotion"),
            ("perspective_memory", "trinity.brain.perspective_memory"),
            ("recurrence_consolidation", "trinity.brain.recurrence_consolidation"),
            ("reflective_agency", "trinity.brain.reflective_agency"),
            ("reflective_context", "trinity.brain.reflective_context"),
            ("reflex_attention", "trinity.brain.reflex_attention"),
            ("retention_influence", "trinity.brain.retention_influence"),
            ("retrieval_planning", "trinity.brain.retrieval_planning"),
            ("rl_graph_evolution", "trinity.brain.rl_graph_evolution"),
            ("selective_inattention", "trinity.brain.selective_inattention"),
            ("self_caused_credit", "trinity.brain.self_caused_credit"),
            ("self_model", "trinity.brain.self_model"),
            ("semantic_workspace", "trinity.brain.semantic_workspace"),
            ("seven_layer_memory", "trinity.brain.seven_layer_memory"),
            ("signal_context", "trinity.brain.signal_context"),
            ("silent_scholar", "trinity.brain.silent_scholar"),
            ("source_evolution", "trinity.brain.source_evolution"),
            ("sovereign_layers", "trinity.brain.sovereign_layers"),
            ("spontaneous_evolution", "trinity.brain.spontaneous_evolution"),
            ("strategic_surprise", "trinity.brain.strategic_surprise"),
            ("task_memory_views", "trinity.brain.task_memory_views"),
            ("temporal_graph", "trinity.brain.temporal_graph"),
            ("think_before_speak", "trinity.brain.think_before_speak"),
            ("tool_graph_memory", "trinity.brain.tool_graph_memory"),
            ("value_encoder", "trinity.brain.value_encoder"),
            ("versioned_memory", "trinity.brain.versioned_memory"),
            ("wheel_of_intelligence", "trinity.brain.wheel_of_intelligence"),
            ("working_memory", "trinity.brain.working_memory"),

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
