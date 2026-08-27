"""Trinity client - MemoryPageTree mixin (PageIndex-inspired page-first retrieval).

2026-08-26（Phase 1）：
  - build_pagetree()   全量记忆 → 主题页树（纯元数据，零 LLM），保存到 <store>/pagetree.json
  - load_pagetree()    加载（带实例缓存，支持 force 刷新）
  - pagetree_search()  页优先检索：定位页 → 页内排序 → 基础召回兜底
  - search(page_tree=True) 显式启用（默认关闭，保持向后兼容）

Phase 2/3 预留：summary（LLM 节点摘要）、reason（活跃 goal 上下文重判）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from trinity.telemetry import traced

logger = logging.getLogger("trinity.core.client")

# 2026-08-27 (P0 优化 3): reason 判题 LRU 缓存 (query+候选指纹 -> selected)
_JUDGE_CACHE: dict = {}
_JUDGE_CACHE_TS: dict = {}
_JUDGE_CACHE_TTL = 600.0  # 10 分钟

_PAGETREE_FILE = "pagetree.json"


class _PagetreeMixin:
    def _pagetree_path(self) -> str:
        from ._construction import _TRINITY_STORE
        store_dir = _TRINITY_STORE or os.path.expanduser("~/.trinity/store")
        return os.path.join(store_dir, _PAGETREE_FILE)

    def _iter_all_memories(self, page_size: int = 1000):
        """分页遍历全部 active 记忆（adapter 只读，无锁）。"""
        if not self._adapter or not hasattr(self._adapter, "get_all_memories"):
            return
        offset = 0
        while True:
            rows = self._adapter.get_all_memories(limit=page_size, offset=offset)
            if not rows:
                break
            for r in rows:
                yield r
            if len(rows) < page_size:
                break
            offset += page_size

    @traced("pagetree.build")
    def build_pagetree(
        self,
        exclude_categories: Optional[List[str]] = None,
        exclude_tags: Optional[List[str]] = None,
        save: bool = True,
        page_size: int = 1000,
        with_vectors: bool = True,
    ) -> Dict[str, Any]:
        """全量记忆建主题页树（纯元数据 + 可选节点摘要向量）并保存到 <store>/pagetree.json。

        用法::

            mem = Trinity()
            stats = mem.build_pagetree(exclude_categories=["lme", "stress-test", "test"])
        """
        from trinity.retrieval.pagetree import MemoryPageTree

        records = list(self._iter_all_memories(page_size=page_size))
        old_tree = MemoryPageTree.load(self._pagetree_path())
        tree = MemoryPageTree()
        _with_vec = with_vectors and os.environ.get("TRINITY_PAGETREE_VECTORS", "on").lower() not in ("off", "0", "false")
        tree.build(
            records,
            exclude_categories=set(exclude_categories or []),
            exclude_tags=set(exclude_tags or []),
            with_vectors=False,
        )
        # 重建不丢 LLM 摘要（旧树恢复）→ 再嵌入节点向量（摘要优先）
        tree.restore_summaries(old_tree)
        if _with_vec:
            tree.embed_node_vectors()
        if save:
            tree.save(self._pagetree_path())
        self._pagetree = tree
        return {
            **tree.stats,
            "path": self._pagetree_path() if save else None,
            "built_at": tree.built_at,
        }

    def load_pagetree(self, force: bool = False) -> Optional[Any]:
        """加载页树（实例缓存；force=True 重新从磁盘加载）。"""
        if getattr(self, "_pagetree", None) is not None and not force:
            return self._pagetree
        from trinity.retrieval.pagetree import MemoryPageTree
        tree = MemoryPageTree.load(self._pagetree_path())
        self._pagetree = tree
        return tree

    @traced("pagetree.search")
    def pagetree_search(
        self,
        query: str,
        top_k: int = 10,
        page_k: int = 3,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        include_docs: bool = False,
        auto_build: bool = False,
        novel_only: bool = False,
    ) -> Dict[str, Any]:
        """页优先检索（PageIndex 式：先定位页、再读页内）。

        novel_only=True（hybrid 通道用，2026-08-26）：只返回基础召回
        未命中的页内记忆（"新颖召回"），只增不减，不挤占基础结果。

        - 无页树时：auto_build=True 则自动建树（需先有记忆），否则回退基础检索。
        - 返回结果带 page_path / page_title / page_node / source_channel 字段。
        """
        tree = self.load_pagetree()
        if tree is None:
            if auto_build:
                try:
                    self.build_pagetree(save=True)
                    tree = self.load_pagetree(force=True)
                except Exception as exc:
                    logger.warning("pagetree auto_build failed: %s", exc)
            if tree is None:
                logger.warning("pagetree_search: no page tree (build first); falling back to keyword search")
                raw = self._adapter.search_memories(
                    query=query, persona_id=persona_id or None,
                    tenant_id=tenant_id or self.tenant_id, agent_id=agent_id or None,
                    app_id=app_id, session_id=session_id, category=category,
                    top_k=top_k, include_docs=include_docs,
                ) if self._adapter else []
                return {"results": raw, "pushed_memories": []}

        def _base_fn(q: str, k: int) -> List[Dict[str, Any]]:
            if not self._adapter:
                return []
            try:
                return self._adapter.search_memories(
                    query=q, persona_id=persona_id or None,
                    tenant_id=tenant_id or self.tenant_id, agent_id=agent_id or None,
                    app_id=app_id, session_id=session_id, category=category,
                    top_k=k, include_docs=include_docs,
                )
            except Exception as exc:
                logger.warning("pagetree base search failed: %s", exc)
                return []

        out = tree.search(
            query=query, top_k=top_k, page_k=page_k, base_fn=_base_fn,
            filters={
                "persona_id": persona_id or "",
                "agent_id": agent_id or "",
                "session_id": session_id or "",
                "category": category or "",
            },
        )
        if novel_only:
            out["results"] = [
                r for r in out.get("results", [])
                if r.get("source_channel") == "pagetree" and not r.get("in_base")
            ]
        # 审计（与 search() 同口径）
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=None, action="search", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"query": query, "top_k": top_k, "mode": "pagetree",
                             "pages_used": out.get("pages_used", []),
                             "hits": len(out.get("results", []))},
                )
            except Exception:
                pass
        return {
            "results": out.get("results", []),
            "pushed_memories": [],
            "pages_used": out.get("pages_used", []),
            "filled_by_base": out.get("filled_by_base", 0),
        }

    # ── Phase 3: reason 模式（LLM 相关重判 + 活跃 goal 上下文）────────

    def _search_reason(
        self,
        query: str,
        top_k: int = 10,
        base_k: Optional[int] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        include_docs: bool = False,
        llm_model: Optional[str] = None,
        max_candidates: int = 30,
        deep: bool = False,
    ) -> Dict[str, Any]:
        """LLM 推理式检索（Phase 3，默认关闭，mode="reason" 显式启用）。

        流程（借鉴 PageIndex 的 chat_model 树搜索 / "相似≠相关"）：
          1) 候选：关键词 FTS 召回（top_k*2）+ 页树结果（已建树时）合并去重；
          2) 上下文：活跃 goal（structure_store.dsh_goals）拼入判定输入；
          3) LLM 判相关：从候选中挑出与 query（+活跃 goal）最相关的 top_k，
             按相关度排序（JSON 输出 memory_id 序列）；
          4) 兜底：无 LLM key / 调用失败 / 解析失败 → 候选原序返回。

        返回 dict 与 search() 同构：{"results": [...], "pushed_memories": []}，
        另附 "reason": {"selected": [...], "goals": n, "mode": "llm"|"fallback"}。
        """
        _deep = deep or os.environ.get("TRINITY_REASON_DEEP", "off").strip().lower() in ("1", "on", "true", "yes")
        if _deep:
            max_candidates = max(max_candidates, 50)
        base_k = base_k or max(top_k * 2, max_candidates)
        candidates: Dict[str, Dict[str, Any]] = {}

        # 1a) 关键词候选
        if self._adapter:
            try:
                for r in self._adapter.search_memories(
                    query=query, persona_id=persona_id or None,
                    tenant_id=tenant_id or self.tenant_id, agent_id=agent_id or None,
                    app_id=app_id, session_id=session_id, category=category,
                    top_k=base_k, include_docs=include_docs,
                ) or []:
                    mid = r.get("memory_id")
                    if mid:
                        candidates[mid] = r
            except Exception as exc:
                logger.warning("reason base recall failed: %s", exc)

        # 1b) 页树候选（已建树时）
        try:
            tree = self.load_pagetree()
            if tree is not None:
                pt_out = tree.search(query=query, top_k=max_candidates,
                                      page_k=(3 if _deep else 2))
                for r in pt_out.get("results", []):
                    mid = r.get("memory_id")
                    if mid and mid not in candidates:
                        candidates[mid] = r
        except Exception:
            pass

        # 1c) 向量/hybrid 候选（2026-08-26 二轮优化，holdout 实证）——
        #     近义改写查询 FTS 失效，语义通道是主要胜出者；judge 的
        #     候选池必须含向量召回（base 只给 FTS 序，改写查询覆盖不足）。
        if self._hybrid_retriever is not None:
            try:
                hv = self.search_hybrid(query=query, top_k=(20 if _deep else 10), strategy="rrf")
                for r in hv.get("results", []):
                    mid = r.get("memory_id")
                    if mid and mid not in candidates:
                        rec = dict(r)
                        # hybrid 返回 lean dict → 回补 content（judge 需要读文本）
                        if not rec.get("content") and self._adapter:
                            try:
                                full = self._adapter.get_memory(mid) or {}
                                rec["content"] = full.get("content", "")
                                rec["category"] = full.get("category", "")
                            except Exception:
                                rec["content"] = ""
                        candidates[mid] = rec
            except Exception as exc:
                logger.debug("reason hybrid candidates skipped: %s", exc)

        # 2026-08-26（二轮优化实证）：候选必须保持"基础召回优先 + 页新增追加"——
        # 按 score 重排会让页树高分 trait 记忆把 FTS 命中的答案事实挤出 LLM
        # 可见窗口（MS R@5 0.95→0.60 根因）。插入序 = base 先（FTS 序），
        # 页树新增（base 未命中的）追加在后，永不顶替基础结果。
        ordered = list(candidates.values())
        if not ordered:
            return {"results": [], "pushed_memories": [],
                    "reason": {"mode": "fallback", "goals": 0, "selected": []}}

        # 2) 活跃 goal 上下文（best-effort）
        goals_text = ""
        goal_count = 0
        try:
            from trinity.structure_store import goal_list
            gl = goal_list() or {}
            active = [g for g in gl.get("goals", [])
                      if str(g.get("status", "")).lower() in ("active", "in_progress")]
            goal_count = len(active)
            if active:
                goals_text = "\n".join(
                    "- " + str(g.get("objective", ""))[:200]
                    for g in active[:5]
                )
        except Exception:
            goal_count = 0

        # 3) LLM 判相关
        llm_used = False
        selected_ids: List[str] = []
        try:
            from trinity.llm.client import chat_completion, resolve_api_key

            key = resolve_api_key()
            if key:
                cand_text = "\n".join(
                    f"[{i}] {str(r.get('content', ''))[:280]}"
                    + (" [page: " + str(r.get("page_path", "")).split("/")[-1].strip() + "]"
                       if r.get("page_path") else "")
                    for i, r in enumerate(ordered[:max_candidates])
                )
                _is_changes_q = any(
                    k in query.lower()
                    for k in ("changes", "changed", "before", "after", "update",
                              "what happened", "significant", "first half",
                              "over the", "did the person", "started", "moved",
                              "launched", "got", "won", "implemented"))

                cond_rule = (
                    "4. When the question asks about CHANGES / what happened over a "
                    "PERIOD / updates (e.g. changes over the first half, before and "
                    "after), the answer needs SEVERAL event facts: pick 5-10 memories "
                    "describing concrete EVENTS (actions, launches, moves, "
                    "certifications, awards, projects) about the person/topic, even if "
                    "they look less similar to the question - the question asks for a "
                    "LIST of events, not one fact.\n"
                )
                sys_msg = (
                    "You are a memory relevance judge for a personal memory system. "
                    "Given the question and the user's ACTIVE GOALS, select the memory "
                    "excerpts that CONTAIN THE ANSWER FACTS. Rules:\n"
                    "1. Answer facts are usually NOT word-similar to the question — prefer "
                    "concrete factual statements (events, numbers, dates, names, "
                    "preferences, changes) over trait labels or generic descriptions.\n"
                    "2. For 'what changed / what happened regarding X' questions, pick "
                    "event facts about X, not identity/trait statements.\n"
                    "3. Select exactly " + str(top_k) + " items when available (at least 3).\n"
                    # 2026-08-26（下一步建议）：事件规则只进 deep 模式——类目化实测（v5 0.710 < v3 0.752）：规则 4 对 KU/TR 也有副作用；deep 模式（v4 配置）才带规则 4（MS 0.237 + holdout 0.663）。
                    + (cond_rule if _deep else "")
                    + 'Reply ONLY with JSON: {"selected": ["<idx>", ...]} where idx are '
                    + "the item numbers from the list, best first."
                )
                user = (
                    "Question: " + query
                    + ("\n\nACTIVE GOALS:\n" + goals_text if goals_text else "")
                    + "\n\nCANDIDATE MEMORIES:\n" + cand_text
                )
                from trinity.llm.client import resolve_model_for as _resolve_model
                _model = _resolve_model("retrieval_judge",
                                        llm_model or os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat"))
                # 2026-08-27 (P0 优化 3): 判题 LRU 缓存——相同 (query+候选指纹) 复用,
                # 省 LLM 调用 (官方 500 问评测 7h 的主成本)。TRINITY_REASON_CACHE=off 关闭。
                import hashlib as _hl
                _cache_on = os.environ.get("TRINITY_REASON_CACHE", "on").strip().lower() not in ("off", "0", "false")
                _finger = None
                _cached_sel = None
                if _cache_on:
                    _finger = _hl.sha256((query + "||" + cand_text[:4000] + "||" + sys_msg).encode()).hexdigest()[:24]
                    _cached_sel = _JUDGE_CACHE.get(_finger)
                _heur_sel = None
                if _cached_sel is not None:
                    content = ""
                elif os.environ.get("TRINITY_JUDGE_HEURISTIC", "on").strip().lower() not in ("off", "0", "false"):
                    # 2026-08-27（judge 蒸馏）：高词重叠候选启发式直接选中（跳过 LLM）。
                    # 词重叠率 >= 0.6 时 judge 必然选中——直接判定省 LLM 调用。
                    import jieba as _jieba
                    _qwords = set(t for t in _jieba.cut(query) if t.strip())
                    _heur = []
                    for _i, _r in enumerate(ordered[:max_candidates]):
                        _cw = set(t for t in _jieba.cut(str(_r.get("content") or "")[:400]) if t.strip())
                        if not _qwords or not _cw:
                            continue
                        _overlap = len(_qwords & _cw) / max(1, len(_qwords))
                        if _overlap >= 0.6:
                            _heur.append(str(_i))
                    if _heur:
                        content = ""
                        _heur_sel = _heur
                        sel = _heur
                        if _cache_on and _finger is not None:
                            _JUDGE_CACHE[_finger] = list(sel)
                            _JUDGE_CACHE_TS[_finger] = time.time()
                    else:
                        resp = chat_completion(
                            {"model": _model,
                             "messages": [{"role": "system", "content": sys_msg},
                                          {"role": "user", "content": user}],
                             "temperature": 0.0, "max_tokens": 200},
                            timeout=60,
                        )
                        content = resp.get("content", "")
                else:
                    resp = chat_completion(
                        {"model": _model,
                         "messages": [{"role": "system", "content": sys_msg},
                                      {"role": "user", "content": user}],
                         "temperature": 0.0, "max_tokens": 200},
                        timeout=60,
                    )
                    content = resp.get("content", "")
                import re as _re
                m = _re.search(r"\{[^{}]*\}", content)
                if _heur_sel is not None:
                    sel = _heur_sel
                elif _cached_sel is not None:
                    sel = _cached_sel
                else:
                    import json as _json
                    data = _json.loads(m.group(0)) if m else {}
                    sel = data.get("selected") or []
                    if _cache_on and _finger is not None:
                        _JUDGE_CACHE[_finger] = list(sel)
                        _JUDGE_CACHE_TS[_finger] = time.time()
                        if len(_JUDGE_CACHE) > 256:
                            _now = time.time()
                            for _k in [k for k, t in _JUDGE_CACHE_TS.items() if _now - t > _JUDGE_CACHE_TTL]:
                                _JUDGE_CACHE.pop(_k, None)
                                _JUDGE_CACHE_TS.pop(_k, None)
                for s in sel:
                    idx = str(s).strip()
                    if idx.isdigit() and 0 <= int(idx) < len(ordered):
                        selected_ids.append(ordered[int(idx)]["memory_id"])
                llm_used = bool(selected_ids)
        except Exception as exc:
            logger.warning("reason LLM judge failed, fallback to candidates: %s", exc)

        if not llm_used:
            selected_ids = [r["memory_id"] for r in ordered[:top_k]]
        else:
            # 2026-08-26（二轮优化实证）：judge 只重排、不截断——
            # LLM 常只选 2-4 条（全量池 MS R@5 0.60 根因），从基础序
            # 填充剩余位，保证最终召回 >= 关键词基线（judge 决定排序，
            # 基础序保证覆盖）。
            _seen = set(selected_ids)
            for r in ordered:
                if len(selected_ids) >= top_k:
                    break
                if r["memory_id"] not in _seen:
                    _seen.add(r["memory_id"])
                    selected_ids.append(r["memory_id"])

        # 4) 组装结果
        by_id = {r["memory_id"]: r for r in ordered}
        results = [by_id[mid] for mid in selected_ids if mid in by_id]
        results = results[:top_k]
        # 审计
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=None, action="search", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"query": query, "top_k": top_k, "mode": "reason",
                             "llm": llm_used, "goals": goal_count,
                             "hits": len(results)},
                )
            except Exception:
                pass
        return {
            "results": results,
            "pushed_memories": [],
            "reason": {"mode": "llm" if llm_used else "fallback",
                       "goals": goal_count,
                       "selected": selected_ids[:top_k]},
        }

