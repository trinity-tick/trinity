# -*- coding: utf-8 -*-
"""RouteReasoner — 产品化 QA 推理模块（2026-08-17, 建议3 产品化落地）

把 LongMemEval_S 基准已验证的生成/检索策略（benchmark/lme_route3.py 提炼，
judge3 口径下: turn 粒度 multi +24pp / REL+inner2 temporal +9pp /
pref 两段式 +24pp）封装为生产可复用服务，供 Trinity.reason / REST /reason /
DSH 工具调用。

策略路由（--route 等价）:
  multi-session             : turn 粒度检索 + [DATE] 前缀 + top-16 turns
  temporal-reasoning        : session 检索 + [DATE] + [REL: N days] + inner2 过滤 + 时间线排序
  single-session-preference : 两段式（stage1 偏好抽取 → stage2 个性化作答）
  knowledge-update / others : session 检索 + [DATE] + plain 生成

LLM: DeepSeek（凭证 ~/.dsh/.credentials.yaml 的 DEEPSEEK_API_KEY；模型可配）。
无凭证/失败 → 返回 error 字段，调用方（Trinity.reason）回退 OpenDomainReasoner。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 提示词模板（与基准一致）────────────────────────────────────────

GEN_SYS_PLAIN = (
    "You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble."
)

GEN_SYS_TEMPORAL = (
    "You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a REL marker (days before the question date) showing when the conversation happened. "
    "Read ALL excerpts carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact (date + relative days). "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent."
)

PREF_STAGE1 = (
    "You are analyzing a user conversation archive to personalize a response to: {question}\n"
    "Extract the user preferences that are RELEVANT to answering this question, as CONCRETE anchors: "
    "specific tools/platforms/products they use, their preferred style/tone, budget or experience level, "
    "past choices or opinions they expressed. Output a compact bullet list of 3-8 specific preferences. "
    "If nothing is evident, output exactly: NONE"
)

PREF_STAGE2 = (
    "You are a personal assistant who knows this user well. User preferences (concrete anchors): {summary}\n"
    "Answer the question with a personalized reply that is SPECIFIC and actionable: "
    "recommend concrete resources/options/products that match the user's actual tools and level. "
    "Follow the user preferences closely; answer the question directly; do not restate it."
)

DATE_RE = re.compile(r"\[(?:DATE|date): ([^\]]+)\]")


def parse_date(s: Any) -> Optional[datetime]:
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", str(s or ""))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def route_for(qtype: Optional[str]) -> str:
    """按题型路由策略（未知/None → plain）。"""
    t = (qtype or "").lower()
    if "multi" in t:
        return "turn"
    if "temporal" in t:
        return "temporal"
    if "pref" in t:
        return "pref"
    return "plain"


def _ensure_date_prefix(content: str, created_at: Any) -> str:
    """内容无 [DATE:] 时用记忆 created_at 补日期前缀（temporal 策略前提）。

    2026-08-21：生产摄入可能未带时间戳（08-17 全量 60.4% 的 temporal 39.1%
    回退根因）。created_at 兼容 "YYYY-MM-DD ..." 与 "YYYY/MM/DD ..."。
    """
    if not content or "[DATE:" in content:
        return content
    if not created_at:
        return content
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(created_at))
    if not m:
        m = re.match(r"(\d{4})/(\d{2})/(\d{2})", str(created_at))
    if not m:
        return content
    return "[DATE: %s/%s/%s] %s" % (m.group(1), m.group(2), m.group(3), content)


def _split_turns(text: str) -> List[str]:
    return re.split(r"\n(?=\[(?:user|assistant|system)\])", text)


def build_prompt(
    question: str,
    evidence: List[Dict[str, Any]],
    strategy: str,
    question_date: Optional[str] = None,
    top_turns: int = 16,
    inner2_max_turns: int = 40,
) -> Dict[str, str]:
    """按策略构造 system/user 提示词（纯函数，可单测）。"""
    qdate = parse_date(question_date)
    q_terms = set(re.findall(r"[a-z0-9]+", question.lower()))

    if strategy == "turn":
        # 2026-09-02（推理质量修复）：移植 temporal/pref 的证据过滤——原始实现把
        # 检索到的整块内容（含 JSON slots 噪音/超长文档）直接塞给 LLM，实测
        # multi-session 问题恒答 UNKNOWN（证据其实包含答案）。现按查询词过滤 +
        # 每轮截断 1000 字符 + 每块最多 8 轮，显著去噪。
        ctx: List[str] = []
        for e in evidence:
            c = (e.get("content") or "").strip()
            if not c:
                continue
            turns = _split_turns(c)[:inner2_max_turns]
            kept = []
            # 2026-09-02: 中文/无拉丁词查询（q_terms<=1）词过滤会误伤——保底 5 条
            _keep_min = 5 if len(q_terms) <= 1 else 2
            for t_ in turns:
                tl = t_.lower()
                if any(term in tl for term in q_terms) or len(kept) < _keep_min:
                    kept.append(t_[:1000])
            ctx.append("\n".join(kept[:8]) if kept else c[:12000])
        ctx = ctx[:top_turns]
        ctx_text = "\n" + "\n===TURN===\n".join(ctx)
        return {"system": GEN_SYS_PLAIN,
                "user": "Conversation excerpts:" + ctx_text + "\n\nQuestion: " + question + "\nAnswer:"}

    if strategy == "temporal":
        ctx2: List[str] = []
        for e in evidence:
            c = (e.get("content") or "").strip()
            turns = _split_turns(c)[:inner2_max_turns]
            kept = []
            for t_ in turns:
                tl = t_.lower()
                if any(term in tl for term in q_terms) or len(kept) < 2:
                    kept.append(t_[:1000])
            ctx2.append("\n".join(kept[:8]) if kept else c[:12000])
        blocks: List[Tuple[Optional[datetime], str]] = []
        for c in ctx2:
            m = DATE_RE.search(c)
            d = parse_date(m.group(1)) if m else None
            rel = ""
            if d and qdate:
                rel = " [REL: " + str((qdate - d).days) + " days before question date]"
            if m:
                c = c.replace(m.group(0), m.group(0) + rel, 1)
            blocks.append((d, c))
        blocks.sort(key=lambda x: x[0] if x[0] else datetime.max)
        ctx_text = "\n" + "===SESSION===" + "\n".join(b[1] for b in blocks)
        return {"system": GEN_SYS_TEMPORAL,
                "user": "Conversation excerpts:" + ctx_text + "\n\nQuestion: " + question + "\nAnswer:"}

    if strategy == "pref":
        # 2026-08-17 优化：与 opt3 pref3（全量 SS-P 60%）对齐——inner2 过滤 + 保留 top-5 证据
        ctx_pref: List[str] = []
        for e in evidence[:5]:
            c = (e.get("content") or "").strip()
            if not c:
                continue
            turns = _split_turns(c)[:inner2_max_turns]
            kept = []
            for t_ in turns:
                tl = t_.lower()
                if any(term in tl for term in q_terms) or len(kept) < 2:
                    kept.append(t_[:1000])
            ctx_pref.append("\n".join(kept[:8]) if kept else c[:12000])
        ctx_plain = "\n" + "===SESSION===" + "\n".join(ctx_pref)
        return {"system": PREF_STAGE1.format(question=question),
                "user": "Conversation excerpts:" + ctx_plain[:12000]}

    # plain / knowledge-update
    # 2026-09-02（回归修复）：中文查询的词过滤会误伤答案（q_terms 只有拉丁词，
    # 中文答案条目被丢弃 → UNKNOWN；实测保底 2 条恰好是噪音）。改为仅截断去噪。
    ctx_plain2: List[str] = [
        (e.get("content") or "").strip()[:1500]
        for e in evidence
        if (e.get("content") or "").strip()
    ]
    ctx_text = "\n" + "===SESSION===" + "\n".join(ctx_plain2)
    return {"system": GEN_SYS_PLAIN,
            "user": "Conversation excerpts:" + ctx_text + "\n\nQuestion: " + question + "\nAnswer:"}


class RouteReasoner:
    """生产化 QA 推理器（已验证策略封装）。"""

    def __init__(
        self,
        search_fn: Optional[Callable] = None,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        top_k: int = 12,
        turn_top_k: int = 16,
        api_base: str = "https://api.deepseek.com/v1/chat/completions",
    ):
        self._search_fn = search_fn
        self._api_key = api_key or self._load_key()
        self.model = model
        self.top_k = top_k
        self.turn_top_k = turn_top_k
        self.api_base = api_base

    @staticmethod
    def _load_key() -> Optional[str]:
        cred = os.path.expanduser("~/.dsh/.credentials.yaml")
        if not os.path.exists(cred):
            return None
        for line in open(cred, encoding="utf-8-sig"):
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
        return None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _retrieve(self, question: str, agent_id: Optional[str], persona_id: Optional[str],
                  top_k: int) -> List[Dict[str, Any]]:
        if self._search_fn is None:
            return []
        hits = self._search_fn(question, top_k=top_k, agent_id=agent_id, persona_id=persona_id)
        hit_list = hits.get("results", []) if isinstance(hits, dict) else (hits or [])
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for h in hit_list:
            c = (h.get("content") or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            # 2026-08-21：temporal 策略前提是证据带 [DATE:]——生产摄入可能未带
            # 时间戳（08-17 全量 60.4% 的 temporal 39.1% 回退根因），用记忆
            # created_at 自动补齐日期前缀，保证 REL/时间线排序可用。
            h = dict(h)
            h["content"] = _ensure_date_prefix(c, h.get("created_at"))
            out.append(h)
        return out

    # ── 2026-08-25（ENGRAM 式证据组织）─────────────────────────────
    # 按记忆类型（episodic/semantic/procedural）组织证据 + 预算门控：
    # - 类型映射：category → 类型（episodic: session/episodic/lme；
    #   semantic: decision/knowledge/general/consolidation；procedural: skill/procedure）
    # - 查询路由：qtype → 类型优先级（pref/knowledge → semantic 优先；
    #   multi/temporal → episodic 优先）
    # - 预算：按类型配额截断 + 去重 + [TYPE] 前缀序列化（LLM 更易区分证据来源）
    # env TRINITY_EVIDENCE_TYPED=on 启用（默认 off，向后兼容）
    _TYPE_OF_CATEGORY = {
        "session": "episodic", "episodic": "episodic", "lme": "episodic",
        "decision": "semantic", "knowledge": "semantic", "general": "semantic",
        "consolidation": "semantic", "kb_harvested": "semantic",
        "skill": "procedural", "procedure": "procedural",
    }
    _QTYPE_TYPE_PRIORITY = {
        "pref": ["semantic", "episodic", "procedural"],
        "knowledge-update": ["semantic", "episodic", "procedural"],
        "multi-session": ["episodic", "semantic", "procedural"],
        "temporal": ["episodic", "semantic", "procedural"],
    }

    def _organize_evidence(self, evidence: list, qtype: Optional[str],
                           budget: int = 12) -> list:
        """ENGRAM 式：类型分组 → 路由优先级配额 → 去重 → [TYPE] 前缀。"""
        try:
            typed: dict = {"episodic": [], "semantic": [], "procedural": []}
            for e in evidence:
                cat = str(e.get("category") or "").strip().lower()
                t = self._TYPE_OF_CATEGORY.get(cat, "semantic")
                typed[t].append(e)
            priority = self._QTYPE_TYPE_PRIORITY.get(str(qtype or "").lower(),
                                                     ["semantic", "episodic", "procedural"])
            out: list = []
            seen: set = set()
            # 按优先级分配预算（首个类型 50%，其余均分）
            n = len(priority)
            for i, t in enumerate(priority):
                pool = typed.get(t, [])
                if not pool:
                    continue
                quota = int(budget * (0.5 if i == 0 else 0.5 / max(n - 1, 1)))
                for e in pool[:max(quota, 1)]:
                    c = (e.get("content") or "").strip()
                    if not c or c in seen:
                        continue
                    seen.add(c)
                    e = dict(e)
                    e["content"] = f"[{t.upper()}] " + c
                    out.append(e)
            return out[:budget]
        except Exception:
            return evidence

    def _chat(self, system: str, user: str, max_tokens: int = 350, timeout: int = 120) -> str:
        # 2026-08-24（P1-7）：走统一 LLM 适配层——兼容推理模型
        # （reasoning_content 输出 / finish_reason=length）与 chat 模型；
        # 模型可用 TRINITY_LLM_MODEL 覆盖（默认 deepseek-chat）。
        # P1-7：stable_prefix_messages 把系统提示固定为缓存前缀（tag 版本化），
        # 变体（问题/证据）全放 user 尾部——最大化 DeepSeek 前缀缓存命中。
        from trinity.llm.client import (
            chat_completion,
            resolve_default_model,
            stable_prefix_messages,
        )
        messages = stable_prefix_messages(system, user, tag="trinity-qa-v1")
        payload = {
            "model": self.model or resolve_default_model(),
            "messages": messages,
            "temperature": 0.0, "max_tokens": max_tokens,
        }
        resp = chat_completion(
            payload,
            api_key=self._api_key,
            base_url=self.api_base.rsplit("/chat/completions", 1)[0],
            timeout=timeout,
        )
        return resp["content"]

    def answer(
        self,
        question: str,
        qtype: Optional[str] = None,
        question_date: Optional[str] = None,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """完整管线：路由 → 检索 → 提示词 → 生成。"""
        if not self.available:
            return {"answer": None, "strategy": route_for(qtype), "error": "no api key"}
        strategy = route_for(qtype)
        top_k = self.turn_top_k if strategy == "turn" else self.top_k
        evidence = self._retrieve(question, agent_id, persona_id, top_k=top_k)
        # 2026-08-25（闭环修复：时间巩固消费）：semantic 优先类型
        # （pref/knowledge-update）额外检索 consolidation 摘要（agent=consolidation
        # 命名空间），作为高价值证据前置——TiMem 语义：偏好/知识题先用巩固摘要。
        _qtype_l = str(qtype or "").lower()
        if _qtype_l in ("pref", "single-session-preference", "knowledge-update"):
            try:
                _cons = self._search_fn(question, top_k=2, agent_id="consolidation")
                _cl = _cons.get("results", []) if isinstance(_cons, dict) else (_cons or [])
                _seen2 = {e.get("content", "") for e in evidence}
                for h in _cl:
                    c = (h.get("content") or "").strip()
                    if c and c not in _seen2:
                        _seen2.add(c)
                        h2 = dict(h)
                        h2["content"] = _ensure_date_prefix(c, h2.get("created_at"))
                        h2["_consolidated"] = True
                        evidence.insert(0, h2)  # 前置高价值摘要
            except Exception:
                pass
        # 2026-08-25（ENGRAM 式）：类型组织 + 证据预算（env 开关）
        if os.environ.get("TRINITY_EVIDENCE_TYPED", "off").strip().lower() in ("1", "on", "true", "yes"):
            evidence = self._organize_evidence(evidence, qtype, budget=top_k)
        if not evidence:
            return {"answer": "UNKNOWN", "strategy": strategy, "evidence": [], "error": None}

        t0 = time.time()
        try:
            if strategy == "pref":
                p = build_prompt(question, evidence, "pref", question_date)
                summary = self._chat(p["system"], p["user"], max_tokens=220)
                if "NONE" in summary.upper() and len(summary) < 30:
                    answer = "UNKNOWN"
                else:
                    s2 = PREF_STAGE2.format(summary=summary[:900])
                    answer = self._chat(s2, "Question: " + question, max_tokens=280)
            else:
                p = build_prompt(question, evidence, strategy, question_date,
                                 top_turns=self.turn_top_k)
                answer = self._chat(p["system"], p["user"], max_tokens=350)
        except Exception as exc:
            return {"answer": None, "strategy": strategy, "error": f"{type(exc).__name__}: {exc}",
                    "latency_s": round(time.time() - t0, 2)}

        return {
            "answer": answer,
            "strategy": strategy,
            "n_evidence": len(evidence),
            "latency_s": round(time.time() - t0, 2),
            "error": None,
        }
