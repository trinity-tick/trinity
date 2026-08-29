# -*- coding: utf-8 -*-
"""LLM 调用适配层（2026-08-24, COMPARISON_VS_2026_SOTA_R7 P1-7）。

解决推理模型（deepseek-v4-pro 等）与 chat 模型（deepseek-chat）的响应
格式差异——EXECUTION 43 轮实测：v4-pro 的输出在 ``reasoning_content``，
``content`` 为空且 ``finish_reason=length``，导致现有调用方全部取空。

本模块提供统一入口：
  - ``chat_completion(payload)``：OpenAI 兼容 POST，自动解析
    reasoning_content / content / finish_reason / usage；
  - ``extract_answer(response, max_tokens)``：从响应提取最终答案——
    content 非空用 content；为空（推理模型）时从 reasoning_content 提取
    最后一段实质文本作为答案；
  - ``reasoning_budget()``：按模型推断 thinking budget 建议（可被
    TRINITY_LLM_THINKING_TOKENS 覆盖），供调用方设置 max_tokens。

不改变既有调用方行为：无 key / 网络失败时抛异常由调用方兜底
（与 proposition_extractor / offload 的既有契约一致）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.llm.client")

# 推理模型名单（输出在 reasoning_content，content 可能为空）
REASONING_MODEL_MARKERS = ("v4-pro", "reasoner", "r1", "o1", "o3", "thinking", "-pro")


def is_reasoning_model(model: str) -> bool:
    """判断模型是否可能是推理模型（输出走 reasoning_content）。"""
    m = (model or "").lower()
    return any(mark in m for mark in REASONING_MODEL_MARKERS)


def resolve_api_key() -> Optional[str]:
    """解析 LLM API key（优先级：TRINITY_LLM_API_KEY → DEEPSEEK_API_KEY）。"""
    for name in ("TRINITY_LLM_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


def resolve_base_url() -> str:
    """解析 LLM base URL（默认 DeepSeek v1）。"""
    return os.environ.get("TRINITY_LLM_BASE_URL") or "https://api.deepseek.com/v1"


def resolve_default_model() -> str:
    """解析默认模型（TRINITY_LLM_MODEL → deepseek-chat）。"""
    return os.environ.get("TRINITY_LLM_MODEL") or "deepseek-chat"


def resolve_model_for(task_type: str, fallback: Optional[str] = None) -> str:
    """任务分级模型路由（2026-08-26 Codex 借鉴 Phase 3）。

    TRINITY_LLM_ROUTING 环境变量：JSON 对象（{"summarize": "deepseek-chat", ...}）
    或 "task=model,task2=model2" 逗号列表；未配置/未命中回退默认模型。

    约定任务类型（Phase 3）：summarize（页树/会话摘要，便宜模型）、
    retrieval_judge（reason 检索判题，可配强模型）、decay（衰减摘要）。
    """
    default = fallback or resolve_default_model()
    raw = os.environ.get("TRINITY_LLM_ROUTING", "").strip()
    if not raw:
        return default
    try:
        import json as _json
        if raw.lstrip().startswith("{"):
            table = _json.loads(raw)
            if not isinstance(table, dict):
                return default
        else:
            table = {}
            for part in raw.split(","):
                k, _, v = part.partition("=")
                if k.strip() and v.strip():
                    table[k.strip()] = v.strip()
    except Exception:
        return default
    return str(table.get(task_type, default))


def reasoning_budget(model: Optional[str] = None) -> int:
    """推理模型的 thinking budget（token）。

    推理模型需要更大的生成预算（reasoning + 答案都算 max_tokens）；
    可用 TRINITY_LLM_THINKING_TOKENS 显式覆盖。chat 模型返回 0（不适用）。
    """
    if is_reasoning_model(model or resolve_default_model()):
        return int(os.environ.get("TRINITY_LLM_THINKING_TOKENS", "4096"))
    return 0


# ── Prompt cache 前缀管理（2026-08-24, R8 P1-7）────────────────────────
# DeepSeek/OpenAI 前缀缓存命中要求"前缀字节级稳定"（实测可 2 折成本）。
# 实践：系统提示固定在前、变体（问题/证据）全放 user 尾部；system 首行
# 带稳定 tag 便于审计与版本化（变体绝不放 system，避免污染缓存前缀）。


def stable_prefix_messages(
    system: str,
    user: str,
    tag: str = "trinity-llm",
) -> List[Dict[str, str]]:
    """构造前缀缓存友好的 messages。

    把稳定系统提示（可含 tag 头）固定为第一条 system，变体 user 放尾部——
    同一 system 的所有调用共享缓存前缀；tag 变更 = 缓存失效（版本化）。

    Args:
        system: 系统提示（保持字节级稳定，勿内联变量）。
        user: 用户消息（可含变体：问题/证据/上下文）。
        tag: 稳定前缀标识（如 "trinity-qa-v1"）；变体会失效缓存。

    Returns:
        [{"role": "system", "content": f"[{tag}]\n{system}"}, {"role": "user", "content": user}]
    """
    sys_content = system
    if tag:
        sys_content = f"[{tag}]\n{system}"
    return [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user},
    ]


def cache_hit_stats(usage: Dict[str, Any]) -> Dict[str, Any]:
    """从 usage 提取前缀缓存命中统计（DeepSeek 返回
    prompt_cache_hit_tokens / prompt_cache_miss_tokens；OpenAI 兼容格式
    cached_tokens；均缺失时返回零值）。"""
    usage = usage or {}
    hit = (
        usage.get("prompt_cache_hit_tokens")
        or usage.get("cached_tokens")
        or 0
    )
    miss = usage.get("prompt_cache_miss_tokens") or 0
    total_prompt = usage.get("prompt_tokens") or 0
    hit = int(hit or 0)
    miss = int(miss or 0)
    if not total_prompt:
        total_prompt = hit + miss
    rate = (hit / total_prompt * 100) if total_prompt > 0 else 0.0
    return {
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "prompt_tokens": int(total_prompt),
        "cache_hit_rate_pct": round(rate, 2),
    }


# 2026-08-29（P0 本地 judge）：Ollama 本地路由（网络免疫 + 成本↓）
_LOCAL_BASE = os.environ.get("TRINITY_LOCAL_LLM_BASE", "http://127.0.0.1:11434/v1")
_LOCAL_MODEL = os.environ.get("TRINITY_LOCAL_LLM_MODEL", "qwen3:4b")
_local_ok: Optional[bool] = None  # None=未探测


def local_llm_available() -> bool:
    """探测 Ollama 本地服务（一次探测，缓存结果）。"""
    global _local_ok
    if _local_ok is not None:
        return _local_ok
    try:
        import urllib.request as _ur
        base_root = _LOCAL_BASE.rsplit("/v1", 1)[0]
        with _ur.urlopen(base_root + "/api/tags", timeout=3) as _r:
            _local_ok = (_r.status == 200)
    except Exception:
        _local_ok = False
    return _local_ok


def chat_completion_local(payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    """本地 Ollama 判题（OpenAI 兼容端点）。失败抛异常（调用方回退云端）。"""
    import urllib.request as _ur
    import json as _j
    body = dict(payload)
    body["model"] = _LOCAL_MODEL
    req = _ur.Request(_LOCAL_BASE + "/chat/completions",
                      data=_j.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=timeout) as resp:
        data = _j.loads(resp.read().decode("utf-8"))
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return {"content": msg.get("content", ""), "reasoning": "",
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", ""), "model": _LOCAL_MODEL,
            "usage": data.get("usage", {})}


def chat_completion(
    payload: Dict[str, Any],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 120,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """OpenAI 兼容 /chat/completions 调用，返回规范化响应。

    Args:
        payload: 请求体（model / messages / temperature / max_tokens ...）。
        api_key: 缺省自动解析（TRINITY_LLM_API_KEY → DEEPSEEK_API_KEY）。
        base_url: 缺省 TRINITY_LLM_BASE_URL 或 DeepSeek v1。
        timeout: 请求超时秒数。
        response_format: Structured Outputs（2026 标准契约）——
            {"type": "json_schema", "json_schema": {"name": ..., "schema": {...}}}
            或 {"type": "json_object"}；缺省不传（兼容旧调用）。
        reasoning_effort: 推理预算分层（2026 标准参数）——
            "low"（fast thinking：写入/提取/压缩）/ "medium" /
            "high"（slow thinking：检索决策/冲突消解/多跳 QA）；
            缺省不传（服务端默认）。仅对支持该参数的模型生效，其余忽略。

    Returns:
        {
          "content":      最终文本（content 或 reasoning 提取，可能为空串）,
          "reasoning":    推理过程文本（chat 模型为空串）,
          "finish_reason": "stop" | "length" | ...,
          "model":        实际模型,
          "usage":        {"prompt_tokens": n, "completion_tokens": n, ...},
          "cache":        前缀缓存命中统计,
          "raw":          原始响应 dict,
        }

    Raises:
        urllib.error.HTTPError / URLError / json.JSONDecodeError：
        网络或服务错误（调用方按既有契约兜底）。
    """
    key = api_key or resolve_api_key()
    if not key:
        raise RuntimeError("no LLM api key (TRINITY_LLM_API_KEY / DEEPSEEK_API_KEY)")

    base = (base_url or resolve_base_url()).rstrip("/")
    url = base + "/chat/completions"

    # 推理模型缺省 thinking budget：调用方未显式给 max_tokens 时补足
    model = payload.get("model") or resolve_default_model()
    if is_reasoning_model(model):
        budget = reasoning_budget(model)
        if "max_tokens" not in payload and budget:
            payload = dict(payload)
            payload["max_tokens"] = budget

    # 2026-08-24（R9 后续 P0）：Structured Outputs + reasoning effort 显式透传
    if response_format is not None:
        payload = dict(payload)
        payload["response_format"] = response_format
    if reasoning_effort is not None:
        payload = dict(payload)
        payload["reasoning_effort"] = reasoning_effort

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    return normalize_response(body)


def parse_structured_response(
    resp: Dict[str, Any],
    schema: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """从结构化输出响应解析 JSON 对象（2026 标准契约的消费端）。

    - content 为 JSON 字符串 → json.loads 解析；
    - content 为 JSON 对象（部分实现直接返回对象）→ 原样；
    - schema 提供时做**语义校验**（必填字段存在性 + 类型抽查）——
      schema 校验不通过返回 None（调用方降级文本解析/重试）；
    - 解析失败返回 None（不抛异常，调用方按既有容错契约兜底）。

    Args:
        resp: chat_completion 的规范化响应。
        schema: JSON Schema（仅用 required + properties 做轻量语义校验）。

    Returns:
        解析后的 dict；失败返回 None。
    """
    content = (resp or {}).get("content", "")
    if isinstance(content, dict):
        obj = content
    else:
        try:
            obj = json.loads(str(content))
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    if schema:
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        for field in required:
            if field not in obj:
                return None
        for field, val in obj.items():
            prop = properties.get(field)
            if not prop:
                continue
            ptype = prop.get("type")
            if ptype == "array" and not isinstance(val, list):
                return None
            if ptype == "string" and not isinstance(val, str):
                return None
            if ptype == "integer" and not isinstance(val, int):
                return None
            if ptype == "number" and not isinstance(val, (int, float)):
                return None
            # 嵌套数组项校验：items 的 required 字段存在性（字符串 item 跳过——
            # 服务端可能简化 items 为字符串数组，由调用方解析端兼容）
            if ptype == "array" and prop.get("items"):
                item_schema = prop["items"]
                item_required = item_schema.get("required") or []
                for item in val:
                    if isinstance(item, str):
                        continue
                    if not isinstance(item, dict):
                        return None
                    for rf in item_required:
                        if rf not in item:
                            return None
    return obj


def normalize_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """把 OpenAI 兼容响应规范化为统一结构（纯函数，便于测试）。

    处理推理模型（reasoning_content 有值、content 为空或 finish_reason=length）
    与普通 chat 模型两种形态。
    """
    choices = body.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content: Optional[str] = message.get("content")
    reasoning: Optional[str] = message.get("reasoning_content")
    finish_reason = choice.get("finish_reason")

    # 推理模型 content 为空 → 从 reasoning_content 提取最终答案
    if (content is None or not str(content).strip()) and reasoning:
        extracted = extract_answer_from_reasoning(reasoning)
        content = extracted

    return {
        "content": (content or "").strip(),
        "reasoning": (reasoning or "").strip(),
        "finish_reason": finish_reason,
        "model": body.get("model") or "",
        "usage": (body.get("usage") or {}),
        # 2026-08-24（R8 P1-7）：前缀缓存命中统计（DeepSeek prompt_cache_*）
        "cache": cache_hit_stats(body.get("usage") or {}),
        "raw": body,
    }


def extract_answer_from_reasoning(reasoning: str) -> str:
    """从推理文本提取最终答案。

    推理模型在 reasoning_content 末尾通常有结论性段落；策略：
    1. 若文本含明确的答案标记（Answer:/答案是:/最终答案:）取其后内容；
    2. 否则取最后一段非空、且长度合理的文本（≥4 字符且不含明显
       推理动词短语的段）。
    """
    text = (reasoning or "").strip()
    if not text:
        return ""

    # 1) 答案标记
    for marker in ("最终答案:", "答案是:", "答案：", "Answer:", "ANSWER:",
                   "the answer is", "The answer is", "The answer:",
                   "结论是", "结论：", "因此答案是"):
        idx = text.rfind(marker)
        if idx >= 0:
            tail = text[idx + len(marker):].strip()
            if tail:
                return _clean_answer(tail)

    # 2) 最后一段非空文本
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    for para in reversed(paragraphs):
        cleaned = _clean_answer(para)
        if len(cleaned) >= 4:
            return cleaned
    return _clean_answer(text)


_ANSWER_VERBS = (
    "因此", "所以", "综上", "结论", "答案是", "可见", "综上所述",
    "so", "therefore", "thus", "hence", "in conclusion", "conclusion",
    "综上所述", "最终", "最终结果",
)


def _clean_answer(segment: str) -> str:
    """清理答案段：截断到合理长度、去掉引导词前缀（可多轮剥离）。"""
    seg = segment.strip().strip('"\'`').strip()
    if not seg:
        return ""
    # 去掉"因此/所以/综上/答案是..."引导前缀（可嵌套：因此，答案是 5）
    for _ in range(4):
        matched = False
        for v in _ANSWER_VERBS:
            if seg.startswith(v):
                seg = seg[len(v):].lstrip(":：,， \t")
                matched = True
                break
        if not matched:
            break
    # 截断超长段（保留首个换行前）
    first_line = seg.splitlines()[0].strip()
    return first_line[:512]
