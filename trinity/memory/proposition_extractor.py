"""命题化 v2 — 写路径命题提取器（M2 原型，2026-08-18）

在 ingest（写入）路径上提取原子命题并落库，与 verbatim 并存：
  - 4 类命题: user_preference / user_fact / user_done / agent_done
  - 输出: JSON 数组 [{type, proposition, ts, expires}]
  - 存储: category=proposition, tags=[proposition, type],
          metadata={proposition_type, temporal, source_memory_id}
  - 开关: TRINITY_PROPOSITION_EXTRACT=on 才提取（默认 off，行为不变）
  - LLM: OpenAI 兼容（DEEPSEEK_API_KEY / TRINITY_LLM_API_KEY），无 key 降级 mock
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.memory.proposition_extractor")

PROPOSITION_TYPES = ("user_preference", "user_fact", "user_done", "agent_done")

IMPORTANCE_BY_TYPE = {
    "user_preference": 0.75,
    "user_fact": 0.70,
    "user_done": 0.65,
    "agent_done": 0.60,
}

PROPOSITION_SYSTEM_PROMPT = """你是一个记忆命题提取器。把用户/助手的对话内容提炼为原子记忆命题。

要求：
1. 一条命题只含一个事实，不总结、不推断。
2. 四类命题：
   - user_preference: 用户的偏好、希望、要求（如"我喜欢深色模式"）
   - user_fact: 用户的身份、事实信息（如"我是项目经理"）
   - user_done: 用户做过的事（如"我完成了对标分析"）
   - agent_done: 助手/agent 做过的事（如"我生成了文档"）
3. 只输出 JSON 数组，不要其他文字：
   [{"type": "user_preference", "proposition": "用户喜欢深色模式", "ts": "2026-08-18", "expires": null}]
4. 无命题可提取时输出 []
5. ts 用对话时间或当前日期；expires 未知则为 null

示例输入：用户："我是供应链项目经理，我喜欢用深色模式，我昨天完成了 WMS 对标"
示例输出：[{"type":"user_fact","proposition":"用户是供应链项目经理","ts":"2026-08-18","expires":null},{"type":"user_preference","proposition":"用户喜欢深色模式","ts":"2026-08-18","expires":null},{"type":"user_done","proposition":"用户昨天完成了 WMS 对标","ts":"2026-08-18","expires":null}]"""


def _get_llm_config() -> Optional[Dict[str, str]]:
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("TRINITY_LLM_BASE_URL") or "https://api.deepseek.com/v1"
    model = os.environ.get("TRINITY_LLM_MODEL") or "deepseek-chat"
    return {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}


def _llm_json_call(system: str, user: str, timeout: float = 60.0) -> str:
    import urllib.request
    cfg = _get_llm_config()
    if not cfg:
        raise RuntimeError("no LLM key configured")
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": int(os.environ.get("TRINITY_PROPOSITION_MAX_TOKENS", "1500")),
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _parse_propositions(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:].strip()
    start, end = t.find("["), t.rfind("]")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    try:
        data = json.loads(t)
    except (ValueError, TypeError):
        logger.warning("proposition parse failed: %s", text[:120])
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type") or "")
        prop = str(item.get("proposition") or "").strip()
        if ptype not in PROPOSITION_TYPES or not prop or len(prop) > 300:
            continue
        out.append({
            "type": ptype,
            "proposition": prop,
            "ts": str(item.get("ts") or "") or None,
            "expires": item.get("expires"),
        })
    return out


def extract_propositions_mock(content: str, role: str = "user") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    text = content or ""
    if not text.strip():
        return out

    def add(ptype: str, prop: str) -> None:
        if prop and len(prop) <= 300 and not any(p["proposition"] == prop for p in out):
            out.append({"type": ptype, "proposition": prop, "ts": None, "expires": None})

    if role == "user":
        if any(k in text for k in ("喜欢", "偏好", "希望", "请", "要", "别用")):
            add("user_preference", "用户偏好：" + text[:60])
        if any(k in text for k in ("我是", "我的", "我负责", "我在")):
            add("user_fact", "用户事实：" + text[:60])
        if any(k in text for k in ("完成", "做了", "已", "写好了", "处理了")):
            add("user_done", "用户完成：" + text[:60])
    else:
        if any(k in text for k in ("已为", "我帮", "生成了", "完成了", "已生成")):
            add("agent_done", "agent 完成：" + text[:60])
    return out


def extract_enabled() -> bool:
    return os.environ.get("TRINITY_PROPOSITION_EXTRACT", "off").lower() in ("on", "1", "true", "yes")


def extract_propositions(content: str, role: str = "user") -> List[Dict[str, Any]]:
    if _get_llm_config():
        try:
            raw = _llm_json_call(PROPOSITION_SYSTEM_PROMPT, "对话内容(role=" + role + "):\n" + (content or "")[:6000])
            props = _parse_propositions(raw)
            if props:
                return props
            logger.warning("LLM returned empty propositions, fallback mock")
        except Exception as e:
            logger.warning("LLM proposition extract failed: %s, fallback mock", e)
    return extract_propositions_mock(content, role)


def extract_and_store(
    adapter: Any,
    content: str,
    source_memory_id: str,
    agent_id: str = "default",
    session_id: Optional[str] = None,
    persona_id: str = "default",
    role: str = "user",
    timestamp: Optional[str] = None,
    max_propositions: int = 10,
) -> int:
    if not extract_enabled():
        return 0
    props = extract_propositions(content, role)[:max_propositions]
    written = 0
    for p in props:
        meta = {
            "proposition_type": p["type"],
            "temporal": p.get("ts") or timestamp,
            "source_memory_id": source_memory_id,
            "extractor": "proposition_v2",
        }
        try:
            adapter.store_memory(
                content="[命题:" + p["type"] + "] " + p["proposition"],
                persona_id=persona_id,
                session_id=session_id,
                agent_id=agent_id,
                role=role,
                importance=IMPORTANCE_BY_TYPE.get(p["type"], 0.5),
                tags=["proposition", p["type"]],
                category="proposition",
                metadata=meta,
            )
            written += 1
        except Exception as e:
            logger.warning("proposition store failed: %s", e)
    if written:
        logger.info("proposition: stored %d/%d from source %s", written, len(props), source_memory_id)
    return written


def maybe_extract_after_store(adapter: Any, store_kwargs: Dict[str, Any], result: Dict[str, Any]) -> int:
    mid = result.get("memory_id") if isinstance(result, dict) else None
    if not mid or not extract_enabled():
        return 0
    return extract_and_store(
        adapter,
        content=store_kwargs.get("content", ""),
        source_memory_id=mid,
        agent_id=store_kwargs.get("agent_id", "default"),
        session_id=store_kwargs.get("session_id"),
        persona_id=store_kwargs.get("persona_id", "default"),
        role=store_kwargs.get("role", "user"),
    )
