#!/usr/bin/env python3
"""trinity/brain/value_encoder.py — 价值驱动记忆编码（2026-09，EXECUTION 105）

认知依据：大脑记忆编码强度由价值/情感显著性驱动（杏仁核通路），
不是统一权重。对标 "Learning What to Remember: A Cognitively Grounded
Multi-Factor Value Model for Agentic Memory"（2025）的五因素价值模型。

实现：LLM（DeepSeek，凭证 ~/.dsh/.credentials.yaml）评估五因素——
  novelty 新颖性 / salience 情感-决策显著性 / goal_relevance 目标相关性
  / retrievability 可复用性 / urgency 时效紧迫性 —— 加权得 value∈[0,1]。
失败降级返回 None（调用方保持原 importance，不破坏现状）。

零第三方依赖（urllib 直调 OpenAI 兼容端点，与 memory_ops.py 同款模式）。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("trinity.brain.value")

FACTORS = ["novelty", "salience", "goal_relevance", "retrievability", "urgency"]
WEIGHTS: Dict[str, float] = {
    "novelty": 0.30,
    "salience": 0.25,
    "goal_relevance": 0.20,
    "retrievability": 0.15,
    "urgency": 0.10,
}
VALUE_MODEL_VERSION = "v1"


def _load_api_key() -> Optional[str]:
    """从凭证文件读取 LLM API key（DEEPSEEK_API_KEY 优先 TRINITY_LLM_API_KEY 兜底）。"""
    try:
        cred = open(os.path.expanduser("~/.dsh/.credentials.yaml"),
                    encoding="utf-8-sig").read()
        for line in cred.splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY") or s.startswith("TRINITY_LLM_API_KEY"):
                return s.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception as e:  # noqa: BLE001
        logger.debug("load key failed: %s", e)
    return None


def llm_chat(prompt: str, max_tokens: int = 500, temperature: float = 0.3,
             timeout: int = 90, model: Optional[str] = None) -> Optional[str]:
    """OpenAI 兼容 chat 调用（凭证文件取 key）。失败返回 None。"""
    key = _load_api_key()
    if not key:
        return None
    base = os.environ.get("TRINITY_LLM_BASE_URL",
                          "https://api.deepseek.com/v1").rstrip("/")
    mdl = model or os.environ.get("TRINITY_LLM_MODEL") or "deepseek-chat"
    payload = {
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_chat failed: %s", e)
        # 2026-09 (EXECUTION 123): 本地降级——DeepSeek API 不可用时切
        # Ollama 本地模型（默认 qwen3:4b，TRINITY_LLM_LOCAL_MODEL 可配；
        # TRINITY_LLM_LOCAL_FALLBACK=0 关闭）。本地慢但保住离线可用性。
        if os.environ.get("TRINITY_LLM_LOCAL_FALLBACK", "1") != "0":
            try:
                _local = os.environ.get("TRINITY_LLM_LOCAL_MODEL") or "qwen3:4b"
                _body = json.dumps({
                    "model": _local, "prompt": prompt, "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                }).encode("utf-8")
                _req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate", data=_body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(_req, timeout=timeout + 60) as _resp:
                    _rb = json.loads(_resp.read().decode("utf-8"))
                return _rb.get("response")
            except Exception as e2:
                logger.warning("local llm fallback failed: %s", e2)
        return None


# ── 快速价值启发式（写入时实时编码，毫秒级，零 LLM 依赖）───────────
# 认知依据：第一印象快速评估（系统 1）+ 深度加工（系统 2/每日补标）。
HIGH_WORDS = [
    "事故", "教训", "偏好", "必须", "禁止", "风险", "故障", "决策",
    "上线", "评审", "安全", "密钥", "凭证", "灾难", "故障", "事故",
    "重要", "警告",
]
MED_WORDS = [
    "计划", "任务", "方案", "步骤", "经验", "总结", "修复", "配置",
    "部署", "迁移", "优化", "问题", "流程", "规则",
]
LOW_WORDS = ["闲聊", "随便", "日常", "hello", "测试写入"]
HIGH_CATEGORIES = {"decision", "preference", "incident", "security", "lesson"}
LOW_CATEGORIES = {"chat", "greeting", "test"}


def quick_value(content: str, category: str = "general") -> float:
    """写入时快速价值评估（规则启发式，0-1）。

    - 高显著词（事故/偏好/安全…）：0.65 起，每词 +0.1（上限 0.95）
    - 中显著词（计划/修复/配置…）：0.60 起，每词 +0.05
    - 类别加权：decision/preference/incident/security → >=0.75
    - 闲聊类：<=0.35
    毫秒级，无外部依赖。
    """
    content = str(content or "")
    cat = str(category or "general").lower()
    hits_high = sum(1 for w in HIGH_WORDS if w in content)
    hits_med = sum(1 for w in MED_WORDS if w in content)
    if hits_high:
        v = min(0.95, 0.65 + 0.10 * hits_high)
    elif hits_med:
        v = min(0.85, 0.60 + 0.05 * hits_med)
    else:
        v = 0.5
    if cat in HIGH_CATEGORIES:
        v = max(v, 0.75)
    if cat in LOW_CATEGORIES:
        v = min(v, 0.35)
    if any(w in content for w in LOW_WORDS):
        v = min(v, 0.35)
    return round(min(1.0, max(0.1, v)), 2)


def estimate_value(content: str,
                   meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """多因素价值评估。

    Returns:
        {"value": 0-1, "factors": {...}, "reason": str, "version": "v1"} 或
        None（LLM 不可用/解析失败——调用方保持原 importance）。
    """
    prompt = (
        "你是记忆系统的价值评估器。评估下面这条记忆对未来的价值，"
        "从五个因素打分（每项 0-1 之间的小数）：\n"
        "novelty 新颖性：信息是否新、是否含新事实或新决策；\n"
        "salience 情感-决策显著性：是否涉及重要决定、痛点、偏好、事故、教训；\n"
        "goal_relevance 目标相关性：是否与长期目标/进行中任务相关；\n"
        "retrievability 可复用性：未来是否会被反复检索使用；\n"
        "urgency 时效紧迫性：是否有时间敏感性（到期/临时有效）。\n"
        "只输出 JSON，不要其他文字："
        '{"novelty":0.8,"salience":0.6,"goal_relevance":0.7,'
        '"retrievability":0.5,"urgency":0.2,"reason":"一句话理由"}\n'
        "记忆内容：" + str(content)[:600]
    )
    raw = llm_chat(prompt, max_tokens=300)
    if not raw:
        return None
    try:
        s = raw.strip()
        _fence = chr(96) * 3
        if s.startswith(_fence):
            s = s.split("\n", 1)[-1]
            if s.endswith(_fence):
                s = s[:-3]
        if s.startswith("json"):
            s = s[4:].lstrip()
        data = json.loads(s)
        factors = {k: max(0.0, min(1.0, float(data[k])))
                   for k in FACTORS if k in data and data[k] is not None}
        if len(factors) < 3:
            return None
        value = sum(WEIGHTS[k] * factors[k] for k in factors)
        return {
            "value": round(value, 3),
            "factors": factors,
            "reason": str(data.get("reason", ""))[:200],
            "version": VALUE_MODEL_VERSION,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("estimate_value parse failed: %s | raw=%.120s", e, raw)
        return None


def batch_estimate(items: List[str]) -> List[Optional[Dict[str, Any]]]:
    """批量价值评估（2026-09，EXECUTION 105.10）：一次 LLM 调用评估多条记忆，
    每日补标成本降约 80%（5 条/次）。逐条解析，失败条目降级为 None。

    Returns:
        list，长度与 items 相同；每条为 estimate_value 同构 dict 或 None。
    """
    if not items:
        return []
    batch = []
    for i, c in enumerate(items):
        batch.append("[" + str(i) + "] " + str(c)[:300])
    prompt = (
        "你是记忆系统的价值评估器。评估下面每条记忆对未来的价值，"
        "从五个因素打分（每项 0-1 之间的小数）：\n"
        "novelty 新颖性 / salience 情感-决策显著性 / goal_relevance 目标相关性"
        " / retrievability 可复用性 / urgency 时效紧迫性。\n"
        "只输出 JSON 数组，不要其他文字，数组长度与输入相同：\n"
        '[{"novelty":0.8,"salience":0.6,"goal_relevance":0.7,'
        '"retrievability":0.5,"urgency":0.2,"reason":"一句话"}, ...]\n'
        "输入：\n" + "\n".join(batch)
    )
    raw = llm_chat(prompt, max_tokens=1200, temperature=0.2)
    if not raw:
        return [None] * len(items)
    try:
        s = raw.strip()
        _fence = chr(96) * 3
        if s.startswith(_fence):
            s = s.split("\n", 1)[-1]
            if s.endswith(_fence):
                s = s[:-3]
        if s.startswith("json"):
            s = s[4:].lstrip()
        data = json.loads(s)
        if not isinstance(data, list):
            return [None] * len(items)
        results = []
        for entry in data[:len(items)]:
            if not isinstance(entry, dict):
                results.append(None)
                continue
            try:
                factors = {k: max(0.0, min(1.0, float(entry[k])))
                           for k in FACTORS if k in entry and entry[k] is not None}
                if len(factors) < 3:
                    results.append(None)
                    continue
                value = sum(WEIGHTS[k] * factors[k] for k in factors)
                results.append({
                    "value": round(value, 3),
                    "factors": factors,
                    "reason": str(entry.get("reason", ""))[:200],
                    "version": VALUE_MODEL_VERSION,
                })
            except Exception:
                results.append(None)
        while len(results) < len(items):
            results.append(None)
        return results
    except Exception as e:  # noqa: BLE001
        logger.warning("batch_estimate parse failed: %s", e)
        return [None] * len(items)


def recall_reconstruct(query: str, memories: list, top_k: int = 8) -> Optional[str]:
    """重建式回忆：把检索到的记忆片段重建为连贯的回忆叙述（对标 R3Mem 思想）。

    memories: [{"content","created_at"}]，按相关度排序。
    """
    if not memories:
        return None
    items = []
    for m in memories[:top_k]:
        created = str(m.get("created_at") or "?")
        content = str(m.get("content"))[:280]
        items.append("- (" + created + ") " + content)
    prompt = (
        "你是长程记忆系统的大脑，正在进行一次【回忆】。"
        "基于检索到的记忆片段，重建一段连贯、自然、带时间感的回忆叙述：\n"
        "1. 整合相关片段，补充合理衔接（不编造记忆中没有的事实）；\n"
        "2. 标注时间锚点（如 '8月底'）；\n"
        "3. 结尾给出'不确定/记忆模糊'的部分；\n"
        "4. 控制在 250 字以内。\n"
        "查询意图：" + str(query)[:200] + "\n"
        "记忆片段：\n" + "\n".join(items)
    )
    return llm_chat(prompt, max_tokens=700, temperature=0.4)
