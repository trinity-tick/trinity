# engine_diagnostics.py — Diagnostics / Ground Truth subsystem extracted from engine.py
# GroundTruthEpisodes (P124 MemMachine)
# status: frozen (2026-09 EXECUTION 163)
from __future__ import annotations
import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

# Shared constants imported from engine_core
from trinity.modules.second_brain.engine_core import (
    PAPERS, VERSION, SEP, SUB,
    ContextAction, ExecutionGear, GovernanceState, CertificateStatus,
    MemoryErrorType, CacheWriteDecision, ConsolidationPhase,
    ContextObject, ContextCommit, MemoryHead, ProvenanceRecord,
    ContinuityState, SafetyAlarm, ExactKVEntry, ConsolidationRecord,
    ValueCategoryMapping,
)

class GroundTruthEpisodes:
    """
    CB52: GroundTruthEpisodes -- 基于Episode的完整记忆存储
    论文: MemMachine (LongMemEval 93.0%, LoCoMo 91.7%), P124

    对齐 MemMachine 核心设计:

    1. 完整 Episode 存储: 按会话保存完整对话轮次, 不做损失性LLM提取摘要
       - Short-term memory: 最近 N 轮原始对话
       - Long-term episodic memory: 历史完整 episode
       - Profile memory: 跨 episode 的稳定用户画像

    2. Contextualized Retrieval: 核匹配 + 上下文窗口扩展
       - 找到核匹配后, 自动扩展到前后 N 轮对话, 确保跨轮证据完整
       - 检索阶段深度调优(而非依赖更好的摄取)

    3. Retrieval Agent 自适应路由:
       - direct: 简单事实直接检索
       - parallel decomposition: 复杂查询并行拆解
       - iterative chain-of-query: 多跳推理链式查询

    检索阶段优化维度(MemMachine):
    - retrieval depth tuning (+4.2%)
    - context formatting (+2.0%)
    - search prompt design (+1.8%)
    - query bias correction (+1.4%)

    Token 效率: 比 Mem0 少 80% 输入 token

    集成:
    - CB50 ContextualChunkIngestion 的 session 缓存对接
    - CB48 AgentNativeCuration 的写路径集成
    - CB45 ProgressiveCascade 的五级检索集成
    """

    RETRIEVAL_DIRECT = "direct"
    RETRIEVAL_PARALLEL = "parallel_decomposition"
    RETRIEVAL_ITERATIVE = "iterative_chain_of_query"

    def __init__(self,
                 short_term_size: int = 20,
                 context_window_extension: int = 5,
                 retrieval_depth: int = 3):
        self.short_term_size = short_term_size
        self.context_window_extension = context_window_extension
        self.retrieval_depth = retrieval_depth

        # Short-term memory: 最近 N 轮
        self.short_term_buffer: deque = deque(maxlen=short_term_size)

        # Long-term episodic memory: 完整 episode 存储
        self.episodes: dict[str, dict] = {}
        self.episode_index: dict[str, list[str]] = defaultdict(list)

        # Profile memory: 跨 episode 稳定用户画像
        self.profile: dict[str, Any] = {
            "identity": {}, "preferences": {}, "facts": {},
            "skills": {}, "relationships": {},
        }

        # 全文关键词索引(用于核匹配)
        self.keyword_index: dict[str, set[str]] = defaultdict(set)

        # 统计
        self.total_episodes: int = 0
        self.total_turns: int = 0
        self.total_retrievals: int = 0
        self.retrieval_stats: dict[str, int] = {
            "direct": 0, "parallel_decomposition": 0, "iterative_chain_of_query": 0,
        }

        # 集成引用
        self.cb45_ref = None
        self.cb48_ref = None
        self.cb50_ref = None

    def ingest_episode(self, episode_id: str, turns: list[dict],
                       metadata: dict = None) -> dict:
        """
        摄入完整 episode: 保存原始对话轮次, 不做损失性提取。

        同时更新:
        - Short-term buffer
        - 关键词索引
        - Profile memory(跨episode稳定画像)
        """
        start_time = time.time()

        episode = {
            "episode_id": episode_id,
            "turns": turns,
            "turn_count": len(turns),
            "metadata": metadata or {},
            "ingested_at": start_time,
            "token_estimate": sum(len(t.get("content", "")) // 4 for t in turns),
        }
        self.episodes[episode_id] = episode
        self.total_episodes += 1
        self.total_turns += len(turns)

        # 更新 short-term buffer
        for turn in turns:
            self.short_term_buffer.append({
                "episode_id": episode_id,
                "turn": turn,
                "timestamp": turn.get("timestamp", start_time),
            })

        # 构建关键词索引
        episode_keywords = set()
        for turn in turns:
            content = turn.get("content", "")
            keywords = self._extract_keywords(content)
            episode_keywords.update(keywords)
            for kw in keywords:
                self.keyword_index[kw].add(episode_id)
        self.episode_index[episode_id] = list(episode_keywords)

        # 更新 profile memory
        self._update_profile(turns)

        # CB48 写路径集成
        if self.cb48_ref:
            for turn in turns:
                self.cb48_ref.curate(
                    f"[EpisodeTurn] {turn.get('content', '')[:200]}",
                    source_type="episode", source_id=episode_id,
                    round_idx=0, agent_id="cb52_ingestion",
                    cb45_instance=self.cb45_ref,
                )

        # CB50 session 缓存对接
        if self.cb50_ref and hasattr(self.cb50_ref, "ingest_session"):
            self.cb50_ref.ingest_session(
                episode_id, turns,
                session_metadata=metadata or {"source": "cb52_episode"},
            )

        elapsed = time.time() - start_time
        return {
            "episode_id": episode_id,
            "turns_ingested": len(turns),
            "keywords_indexed": len(episode_keywords),
            "profile_updated": True,
            "elapsed_ms": round(elapsed * 1000, 1),
        }

    def retrieve(self, query: str,
                 strategy: str = RETRIEVAL_DIRECT,
                 top_k: int = 10) -> dict:
        """
        检索: 支持三种自适应路由策略。

        Retrieval Agent 自适应路由:
        - direct: 核匹配 + Contextualized Retrieval(前后 N 轮扩展)
        - parallel_decomposition: 复杂查询并行拆解
        - iterative_chain_of_query: 多跳推理链式查询
        """
        self.total_retrievals += 1

        if strategy == self.RETRIEVAL_DIRECT:
            result = self._direct_retrieval(query, top_k)
            self.retrieval_stats["direct"] += 1
        elif strategy == self.RETRIEVAL_PARALLEL:
            result = self._parallel_retrieval(query, top_k)
            self.retrieval_stats["parallel_decomposition"] += 1
        elif strategy == self.RETRIEVAL_ITERATIVE:
            result = self._iterative_retrieval(query, top_k)
            self.retrieval_stats["iterative_chain_of_query"] += 1
        else:
            result = self._direct_retrieval(query, top_k)

        return result

    def _direct_retrieval(self, query: str, top_k: int) -> dict:
        """
        Direct Retrieval: 核匹配 + 上下文窗口扩展。

        1. 关键词核匹配找到命中 episode
        2. 对每个命中 episode, 找到精确匹配的 turn
        3. 扩展到前后 context_window_extension 轮
        4. 返回完整上下文片段
        """
        query_keywords = self._extract_keywords(query)

        # 核匹配: 按关键词命中数排序 episode
        episode_scores = defaultdict(float)
        for kw in query_keywords:
            matching_episodes = self.keyword_index.get(kw, set())
            for ep_id in matching_episodes:
                episode_scores[ep_id] += 1.0 / len(query_keywords)

        ranked_episodes = sorted(episode_scores.items(), key=lambda x: -x[1])[:top_k]

        results = []
        for ep_id, score in ranked_episodes:
            episode = self.episodes.get(ep_id)
            if not episode:
                continue

            # 找到最匹配的 turn
            best_turn_idx = self._find_best_turn(query, episode["turns"])

            # Contextualized Retrieval: 扩展到前后 N 轮
            ext = self.context_window_extension
            start_idx = max(0, best_turn_idx - ext)
            end_idx = min(len(episode["turns"]), best_turn_idx + ext + 1)

            context_turns = episode["turns"][start_idx:end_idx]
            context_text = "\n".join(
                f"[{t.get('role', 'unknown')}]: {t.get('content', '')}"
                for t in context_turns
            )

            results.append({
                "episode_id": ep_id,
                "relevance_score": round(score, 3),
                "nucleus_turn_idx": best_turn_idx,
                "context_window": [start_idx, end_idx],
                "context_turns": context_turns,
                "context_text": context_text,
                "context_token_estimate": len(context_text) // 4,
                "turn_count_in_window": len(context_turns),
            })

        # 检索深度优化: 多层排序
        results = self._apply_retrieval_optimizations(results, query)

        return {
            "query": query,
            "strategy": self.RETRIEVAL_DIRECT,
            "total_matches": len(results),
            "query_keywords": query_keywords,
            "results": results,
            "short_term_hits": self._check_short_term(query),
        }

    def _parallel_retrieval(self, query: str, top_k: int) -> dict:
        """
        Parallel Decomposition: 将复杂查询拆解为子查询并行执行。

        拆解策略:
        - 识别查询中的子句(以 and/or/also/plus 等分割)
        - 每个子句独立执行 direct retrieval
        - 合并去重排序
        """
        sub_queries = self._decompose_query(query)
        if len(sub_queries) <= 1:
            return self._direct_retrieval(query, top_k)

        all_results = []
        seen_episodes = set()

        for sub_q in sub_queries:
            sub_result = self._direct_retrieval(sub_q, top_k // len(sub_queries) + 1)
            for r in sub_result["results"]:
                if r["episode_id"] not in seen_episodes:
                    all_results.append(r)
                    seen_episodes.add(r["episode_id"])

        all_results.sort(key=lambda x: -x["relevance_score"])
        results = all_results[:top_k]

        return {
            "query": query,
            "strategy": self.RETRIEVAL_PARALLEL,
            "sub_queries": sub_queries,
            "total_matches": len(results),
            "results": results,
            "short_term_hits": self._check_short_term(query),
        }

    def _iterative_retrieval(self, query: str, top_k: int) -> dict:
        """
        Iterative Chain-of-Query: 多跳推理链式查询。

        流程:
        1. 第一次检索找到初始 episode
        2. 从初始 episode 中提取实体/线索
        3. 用新线索发起第二轮检索
        4. 重复至 retrieval_depth 用完或无新发现
        """
        current_query = query
        all_results = []
        seen_episodes = set()
        chain_log = []

        for hop in range(self.retrieval_depth):
            result = self._direct_retrieval(current_query, top_k)
            chain_log.append({
                "hop": hop + 1,
                "query": current_query,
                "matches": len(result["results"]),
            })

            new_episodes = [
                r for r in result["results"]
                if r["episode_id"] not in seen_episodes
            ]
            if not new_episodes:
                break

            for r in new_episodes:
                all_results.append(r)
                seen_episodes.add(r["episode_id"])

            # 从本轮结果中提取新线索
            new_clues = self._extract_clues_from_results(new_episodes)
            if not new_clues:
                break

            current_query = " ".join(new_clues[:5])

        return {
            "query": query,
            "strategy": self.RETRIEVAL_ITERATIVE,
            "hops": len(chain_log),
            "chain_log": chain_log,
            "total_matches": len(all_results),
            "results": all_results,
            "short_term_hits": self._check_short_term(query),
        }

    def get_short_term(self, n: int = None) -> list[dict]:
        """获取 short-term buffer 内容"""
        if n is None:
            n = self.short_term_size
        items = list(self.short_term_buffer)[-n:]
        return items

    def get_profile(self) -> dict:
        """获取 profile memory"""
        return {
            "identity": dict(self.profile["identity"]),
            "preferences": dict(self.profile["preferences"]),
            "facts": dict(self.profile["facts"]),
            "skills": dict(self.profile["skills"]),
            "relationships": dict(self.profile["relationships"]),
        }

    def query_episodes(self, keyword: str = None,
                       date_start: float = None,
                       date_end: float = None) -> list[dict]:
        """按条件查询 episode"""
        results = []
        for ep_id, ep in self.episodes.items():
            if date_start and ep["ingested_at"] < date_start:
                continue
            if date_end and ep["ingested_at"] > date_end:
                continue
            if keyword:
                ep_text = " ".join(
                    t.get("content", "") for t in ep["turns"])
                if keyword.lower() not in ep_text.lower():
                    continue
            results.append({
                "episode_id": ep_id,
                "turn_count": ep["turn_count"],
                "token_estimate": ep["token_estimate"],
                "ingested_at": ep["ingested_at"],
                "metadata": ep.get("metadata", {}),
            })
        return sorted(results, key=lambda x: x["ingested_at"], reverse=True)

    def adaptive_route(self, query: str) -> str:
        """自适应路由: 根据查询复杂度选择检索策略"""
        query_lower = query.lower()
        complex_signals = [
            "and also", "what about", "compared to", "versus",
            "how did", "what happened after", "then what",
            "relationship between", "connection between",
        ]
        multi_hop_signals = [
            "chain of", "sequence", "steps", "process",
            "first", "then", "finally", "after that",
            "consequence", "resulted in", "led to",
        ]

        multi_part_count = sum(1 for s in complex_signals if s in query_lower)
        hop_count = sum(1 for s in multi_hop_signals if s in query_lower)

        if hop_count >= 2:
            return self.RETRIEVAL_ITERATIVE
        elif multi_part_count >= 2 or len(query.split()) > 15:
            return self.RETRIEVAL_PARALLEL
        else:
            return self.RETRIEVAL_DIRECT

    def _find_best_turn(self, query: str, turns: list[dict]) -> int:
        """找到与查询最匹配的 turn 索引"""
        query_keywords = set(self._extract_keywords(query))
        best_idx = 0
        best_score = -1
        for i, turn in enumerate(turns):
            content = turn.get("content", "")
            turn_keywords = set(self._extract_keywords(content))
            overlap = len(query_keywords & turn_keywords)
            if overlap > best_score:
                best_score = overlap
                best_idx = i
        return best_idx

    def _apply_retrieval_optimizations(self, results: list[dict],
                                        query: str) -> list[dict]:
        """
        应用 MemMachine 检索阶段四维优化:
        1. retrieval depth tuning (score boosting for deeper matches)
        2. context formatting (按相关度二次排序)
        3. search prompt design (query bias correction)
        4. query bias correction (实体权重调整)
        """
        if not results:
            return results

        # retrieval depth tuning: 提升更多轮次的 episode
        for r in results:
            ep = self.episodes.get(r["episode_id"])
            if ep:
                depth_bonus = min(0.1, ep["turn_count"] * 0.002)
                r["relevance_score"] = round(r["relevance_score"] + depth_bonus, 3)

        # context formatting: 二次排序
        results.sort(key=lambda x: (-x["relevance_score"],
                                     -x.get("turn_count_in_window", 0)))

        # query bias correction: 查询中高频词的权重衰减
        query_words = query.lower().split()
        word_freq = {}
        for w in query_words:
            word_freq[w] = word_freq.get(w, 0) + 1
        high_freq_words = {w for w, c in word_freq.items() if c > 1}

        for r in results:
            bias_penalty = sum(
                0.05 for w in high_freq_words
                if w in r.get("context_text", "").lower()
            )
            r["relevance_score"] = round(
                max(0.01, r["relevance_score"] - bias_penalty), 3)

        return results

    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词"""
        text_lower = text.lower()
        words = re.findall(r'\b[a-z]{3,}\b', text_lower)
        stopwords = {"the", "and", "for", "that", "this", "with", "from",
                     "have", "are", "was", "not", "but", "you", "your",
                     "can", "what", "how", "when", "where", "which", "who",
                     "will", "just", "about", "like", "been", "has", "had",
                     "did", "does", "would", "could", "should", "there",
                     "their", "they", "them", "then", "than", "some", "any"}
        return [w for w in words if w not in stopwords]

    def _decompose_query(self, query: str) -> list[str]:
        """拆解复杂查询为子查询"""
        separators = [" and also ", " also ", ", and ", " and ",
                      " plus ", " compared to ", " versus ", " vs "]
        for sep in separators:
            if sep in query.lower():
                parts = re.split(re.escape(sep), query, flags=re.IGNORECASE)
                return [p.strip() for p in parts if p.strip()]
        return [query]

    def _extract_clues_from_results(self, results: list[dict]) -> list[str]:
        """从检索结果中提取新线索(用于迭代检索)"""
        all_text = " ".join(r.get("context_text", "") for r in results)
        keywords = self._extract_keywords(all_text)
        word_freq = {}
        for kw in keywords:
            word_freq[kw] = word_freq.get(kw, 0) + 1
        # 取出现频率最高的新词作为线索
        sorted_words = sorted(word_freq.items(), key=lambda x: -x[1])
        return [w for w, _ in sorted_words[:10]]

    def _check_short_term(self, query: str) -> list[dict]:
        """检查 short-term buffer 中的命中"""
        query_kw = set(self._extract_keywords(query))
        hits = []
        for item in list(self.short_term_buffer)[-10:]:
            content = item["turn"].get("content", "")
            turn_kw = set(self._extract_keywords(content))
            overlap = len(query_kw & turn_kw)
            if overlap > 0:
                hits.append({
                    "episode_id": item["episode_id"],
                    "content": content[:200],
                    "overlap": overlap,
                    "timestamp": item["timestamp"],
                })
        return sorted(hits, key=lambda x: -x["overlap"])

    def _update_profile(self, turns: list[dict]):
        """从 episode 提取并更新 profile memory(跨 episode 稳定画像)"""
        for turn in turns:
            content = turn.get("content", "")
            content_lower = content.lower()
            role = turn.get("role", "")

            # 身份信息检测
            if role == "user":
                identity_patterns = [
                    (r"my name is (\w+)", "identity", "name"),
                    (r"i am (\w+)", "identity", "name"),
                    (r"i'?m (?:a |an )?(\w+)", "identity", "role"),
                    (r"i live in (\w[\w\s]+)", "identity", "location"),
                    (r"i work (?:at|for|as) ([\w\s]+)", "identity", "work"),
                ]
                for pattern, category, key in identity_patterns:
                    match = re.search(pattern, content_lower)
                    if match:
                        self.profile[category][key] = match.group(1).strip()

                # 偏好检测
                pref_patterns = [
                    r"(?:i (?:prefer|like|love|enjoy)) ([\w\s]+)",
                    r"(?:my favorite .*? is) ([\w\s]+)",
                ]
                for pattern in pref_patterns:
                    match = re.search(pattern, content_lower)
                    if match:
                        pref_key = f"pref_{len(self.profile['preferences'])}"
                        self.profile["preferences"][pref_key] = match.group(1).strip()

                # 事实检测
                fact_patterns = [
                    (r"i (?:have|own) (?:a |an )?([\w\s]+)", "possession"),
                    (r"i (?:know|understand|can) ([\w\s]+)", "skill"),
                ]
                for pattern, key in fact_patterns:
                    match = re.search(pattern, content_lower)
                    if match:
                        self.profile["facts"][key] = match.group(1).strip()

        # 限制 profile 大小
        for category in self.profile:
            if isinstance(self.profile[category], dict) and len(self.profile[category]) > 50:
                keys_to_remove = sorted(self.profile[category].keys())[:10]
                for k in keys_to_remove:
                    del self.profile[category][k]

    def get_stats(self) -> dict:
        return {
            "total_episodes": self.total_episodes,
            "total_turns": self.total_turns,
            "short_term_size": len(self.short_term_buffer),
            "episode_index_size": len(self.episode_index),
            "keyword_index_size": len(self.keyword_index),
            "total_retrievals": self.total_retrievals,
            "retrieval_stats": dict(self.retrieval_stats),
            "profile_size": sum(
                len(v) if isinstance(v, dict) else 1
                for v in self.profile.values()),
        }

    def diagnostics(self) -> dict:
        return {
            "architecture": "MemMachine GroundTruthEpisodes (P124)",
            "memory_types": "short_term + long_term_episodic + profile",
            "architecture_principle": "ground_truth_preserving_no_lossy_extraction",
            "retrieval": "contextualized_nucleus_match + context_window_extension",
            "routing_strategies": [
                "direct", "parallel_decomposition", "iterative_chain_of_query",
            ],
            "retrieval_optimizations": {
                "retrieval_depth_tuning": "+4.2%",
                "context_formatting": "+2.0%",
                "search_prompt_design": "+1.8%",
                "query_bias_correction": "+1.4%",
            },
            "token_efficiency": "80% fewer input tokens vs Mem0",
            "integrations": [
                "CB50_ContextualChunkIngestion (session caching)",
                "CB48_AgentNativeCuration (write path)",
                "CB45_ProgressiveCascade (L5 retrieval)",
            ],
            "stats": self.get_stats(),
        }


print("[P124] GroundTruthEpisodes (CB52) initialized -- MemMachine aligned")


# ============ CB53: BEAM-LIGHT 评测框架 (P125, ICLR 2026 BEAM) ============

