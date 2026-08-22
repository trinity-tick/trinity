"""Skill 自动锻造管线（Execution Trace -> 共性模式 -> 可复用 Skill）

对标行业 roadmap 空白：把系统执行轨迹（sidecar jsonl / 任意 tool/action/result 流）归纳为
可复用的 Skill markdown，供写入 data/skills/auto/{name}.md 使用。

组成：
  - parse_traces(records): 轨迹 -> "问题->动作->结果" 序列（字段名多取一容错，按 trace/session 分组）
  - extract_patterns(sequences): LLM 归纳（默认 on；有 key 真实调用，无 key/失败降级为规则式聚类）
  - render_skill(name, domain, pattern, traces_count): 渲染 Skill markdown（YAML front-matter）
  - write_skill(md, name, out_dir): 写文件（文件名安全化）
  - store_skill_meta(store, skill_md): 把 skill 摘要写一条记忆（category=skill），store 为 None 即 dry-run

LLM 调用遵循 proposition_extractor 的模式：stdlib urllib 调 OpenAI 兼容 /chat/completions、
deepseek-chat、JSON 解析容错、无 key 降级。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("trinity.memory.skill_forge")

# ---- 字段名容错 ----
# 按优先级取第一个存在的键。
_ACTION_KEYS = ("action", "tool", "tool_name", "op", "operation", "skill", "phase")
_INPUT_KEYS = ("input", "args", "arguments", "params", "payload", "query")
_RESULT_KEYS = ("result", "output", "response", "return", "ret")
_ERROR_KEYS = ("error", "err", "exception", "failure", "status")
_TS_KEYS = ("ts", "timestamp", "time", "t", "pruned_at", "created_at")
_SESSION_KEYS = ("session", "session_id", "trace_id", "trace", "conversation_id")

FRONT_MATTER_KEYS = ("name", "domain", "source", "traces_count", "created_at", "generated_by")

_SOURCE = "skill_forge"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _first(record: Dict[str, Any], keys: tuple) -> Optional[Any]:
    """按优先级从 record（含嵌套 payload/skill_state）取第一个非空值。"""
    if not isinstance(record, dict):
        return None
    for k in keys:
        val = record.get(k)
        if val is not None and val != "":
            return val
        # payload 可能是 dict 或 "{'skill': ...}" 字符串
        if k in ("skill", "phase", "action", "tool"):
            pl = record.get("payload")
            if isinstance(pl, dict):
                pv = pl.get(k)
                if pv:
                    return pv
            elif isinstance(pl, str):
                pv = _parse_python_like_dict(pl).get(k)
                if pv:
                    return pv
    return None


def _parse_python_like_dict(text: str) -> Dict[str, Any]:
    """解析 "{'skill': 'file-organizer', 'phase': 'scan'}" 这类 Python 风格 dict 字符串。

    JSON 解析优先；失败则用简单正则抽取 key: value。
    """
    if not isinstance(text, str):
        return {}
    t = text.strip()
    if t.startswith("{"):
        try:
            data = json.loads(t)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    out: Dict[str, Any] = {}
    # 匹配 'key': 'value' 或 "key": "value"
    for m in re.finditer(r"(['\"]?)([A-Za-z_][\w-]*)\1\s*:\s*(['\"])(.*?)\3\s*[,}]", t, re.S):
        out[m.group(2)] = m.group(4)
    return out


def _to_text(val: Any, limit: int = 300) -> str:
    """把任意值转成可读单行文本。"""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(val)[:limit]
    return str(val)[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename(name: str) -> str:
    """文件名安全化：非法字符替换为 _，去除残留路径分隔符与非法名。"""
    if not name:
        return "unnamed"
    # 去除路径信息
    name = name.replace("\\", "/").split("/")[-1]
    # 保留字母数字、-、_，其余替换为 _
    cleaned = re.sub(r"[^\w\-]", "_", name, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    cleaned = cleaned.lower()
    if not cleaned or cleaned in {".", ".."}:
        return "unnamed"
    return cleaned[:96]


# ---------------------------------------------------------------------------
# 1. 轨迹解析
# ---------------------------------------------------------------------------
def _group_key(record: Dict[str, Any], fallback: str) -> str:
    s = _first(record, _SESSION_KEYS)
    if s is not None:
        return safe_filename(_to_text(s)) or fallback
    return fallback


def parse_traces(records: "List[Dict[str, Any]]") -> "List[Dict[str, Any]]":
    """从轨迹记录提取 "问题->动作->结果" 序列，按 trace/session 分组。

    返回 [{trace_id, ts, action, input, result, error, raw}...]，按 trace 分组且组内按 ts 排序。
    字段名多取一容错：action 取 action/tool/tool_name/op/skill/phase；
    input 取 input/args/params/payload；result 取 result/output/response；
    error 取 error/err/exception/status。
    """
    seqs: List[Dict[str, Any]] = []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        # 解析 payload（可能是 dict 或 Python 风格字符串）
        pl = rec.get("payload")
        if isinstance(pl, dict):
            base = {**rec, **{k: v for k, v in pl.items() if v not in (None, "")}}
        elif isinstance(pl, str):
            base = {**rec, **{k: v for k, v in _parse_python_like_dict(pl).items() if v not in (None, "")}}
        else:
            base = rec

        action = _to_text(_first(base, _ACTION_KEYS))
        if not action:
            continue
        ts_raw = _first(base, _TS_KEYS)
        ts = ts_raw if ts_raw is not None else idx
        trace_id = _group_key(base, f"trace_{idx}")

        entry = {
            "trace_id": trace_id,
            "ts": ts,
            "action": action,
            "input": _to_text(_first(base, _INPUT_KEYS)),
            "result": _to_text(_first(base, _RESULT_KEYS)),
            "error": _to_text(_first(base, _ERROR_KEYS)),
            "raw": _to_text(pl, limit=120) if pl is not None else _to_text(action, limit=120),
        }
        if trace_id not in groups:
            groups[trace_id] = []
            order.append(trace_id)
        groups[trace_id].append(entry)

    for tid in order:
        seqs.extend(sorted(groups[tid], key=lambda e: (e["ts"] is None, e["ts"])))
    return seqs


# ---------------------------------------------------------------------------
# 2. LLM 配置与调用
# ---------------------------------------------------------------------------
def _get_llm_config() -> Optional[Dict[str, str]]:
    api_key = os.environ.get("TRINITY_SKILL_API_KEY") or os.environ.get("TRINITY_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("TRINITY_SKILL_BASE_URL") or "https://api.deepseek.com/v1"
    model = os.environ.get("TRINITY_SKILL_MODEL") or "deepseek-chat"
    return {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}


def _llm_json_call(system: str, user: str, timeout: float = 90.0) -> str:
    import urllib.request
    cfg = _get_llm_config()
    if not cfg:
        raise RuntimeError("no skill LLM key configured")
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("TRINITY_SKILL_MAX_TOKENS", "4000")),
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


_SYSTEM_PROMPT = """你是一个技能归纳器。给定一组 agent 执行轨迹（action/input/result/error），归纳出其中的共性操作模式，
生成一个可复用 Skill 的规范化 JSON。只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块，格式：
{
  "name": "简短技能名（如 file-organizer）",
  "domain": "领域分类（如 data-organization）",
  "summary": "一句话描述适用场景",
  "steps": ["1. ...", "2. ...", "3. ..."],
  "pitfalls": ["坑位1", "坑位2"]
}
要求：steps 2-8 条，每条是一个明确可执行步骤；pitfalls 0-4 条，指出常见失败点。"""


def _truncate_sequences(sequences: List[Dict[str, Any]], limit: int = 250) -> List[Dict[str, Any]]:
    text = json.dumps(sequences, ensure_ascii=False)
    if len(text) <= limit:
        return sequences
    out: List[Dict[str, Any]] = []
    acc = 0
    for s in sequences:
        piece = json.dumps(s, ensure_ascii=False)
        if acc + len(piece) > limit:
            break
        acc += len(piece)
        out.append(s)
    return out or sequences[:1]


def _parse_pattern(text: str) -> Optional[Dict[str, Any]]:
    """LLM 输出 JSON 解析容错：去 markdown 代码块、抽取最外层 {}、截断右括号补全。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0:
        if end <= start:
            # 无闭合右括号的截断 JSON：裁到开头，准备补全
            t = t[start:]
        else:
            t = t[start : end + 1]
        try:
            data = json.loads(t)
        except (ValueError, TypeError):
            # 截断容错：尝试补全右括号
            try:
                data = json.loads(t + "}" * (t.count("{") - t.count("}")))
            except (ValueError, TypeError):
                # 纯文本降级：把整段当 summary
                if len(t) > 8:
                    return {"summary": re.sub(r"\s+", " ", t)[:300], "steps": [], "pitfalls": []}
                return None
    else:
        # 完全没有 JSON 结构：把整段当 summary
        body = re.sub(r"\s+", " ", t).strip()
        if len(body) > 8:
            return {"summary": body[:300], "steps": [], "pitfalls": []}
        return None
    if not isinstance(data, dict):
        return None
    out: Dict[str, Any] = {}
    for key, cast in (("name", str), ("domain", str), ("summary", str)):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    steps = data.get("steps")
    if isinstance(steps, list):
        out["steps"] = [str(s).strip() for s in steps if str(s).strip()]
    else:
        out["steps"] = []
    pitfalls = data.get("pitfalls") or data.get("pitfall")
    if isinstance(pitfalls, list):
        out["pitfalls"] = [str(p).strip() for p in pitfalls if str(p).strip()]
    else:
        out["pitfalls"] = []
    return out


# ---------------------------------------------------------------------------
# 2b. 规则式降级：按动作类型聚类高频序列
# ---------------------------------------------------------------------------
def _fallback_pattern(sequences: List[Dict[str, Any]]) -> Dict[str, Any]:
    """规则式归纳：按归一化动作聚类，输出高频动作作为步骤清单。

    无 LLM key / LLM 失败时使用，保证产出含步骤、不崩。
    """
    from collections import Counter, OrderedDict

    action_counter: Counter = Counter()
    by_action: Dict[str, List[str]] = {}
    for s in sequences:
        act = _normalize_action(s.get("action") or "")
        if not act:
            continue
        action_counter[act] += 1
        if act not in by_action:
            by_action[act] = []
        if len(by_action[act]) < 3:
            by_action[act].append(s.get("raw") or s.get("input") or s.get("action"))

    steps: List[str] = []
    for act, cnt in action_counter.most_common():
        sample = (by_action.get(act) or [""])[0]
        if sample:
            steps.append(f"执行 {act}：{_to_text(sample, 80)}")
        else:
            steps.append(f"执行 {act}")
    if not steps:
        steps = ["记录并归纳执行轨迹"]

    errors = [s.get("error") for s in sequences if s.get("error")]
    pitfalls = [f"注意 {e[:80]}" for e in dict.fromkeys(errors)][:4]

    top = action_counter.most_common(1)[0][0] if action_counter else "general-task"
    summary = "根据执行轨迹归纳的高频操作流程，覆盖动作："
    summary += ", ".join(a for a, _ in action_counter.most_common(6)) or "无"

    return {
        "name": top,
        "domain": "general",
        "summary": summary,
        "steps": steps,
        "pitfalls": pitfalls,
    }


_ACTION_NORM_RE = re.compile(r"[^a-z0-9_]")


def _normalize_action(action: str) -> str:
    a = action.lower().strip()
    if not a:
        return ""
    if len(a) > 50:
        a = a[:50]
    return _ACTION_NORM_RE.sub("_", a) or "action"


def extract_patterns(
    sequences: List[Dict[str, Any]],
    llm_enabled: bool = True,
    llm_call: Optional[Callable[[str, str], str]] = None,
) -> Dict[str, Any]:
    """归纳共性模式。

    llm_enabled=False 或未配置 key（/调用失败）→ 规则式降级。
    llm_call 可注入假 LLM（测试用），默认用真实 _llm_json_call。
    """
    if not llm_enabled:
        return _fallback_pattern(sequences)
    cfg = _get_llm_config()
    call = llm_call or _llm_json_call
    if cfg:
        try:
            user = "执行轨迹:\n" + json.dumps(_truncate_sequences(sequences), ensure_ascii=False)
            raw = call(_SYSTEM_PROMPT, user)
            pattern = _parse_pattern(raw)
            if pattern:
                return pattern
            logger.warning("skill forge LLM returned non-parse, fallback rule")
        except Exception as e:  # noqa: BLE001 — LLM 失败一律降级
            logger.warning("skill forge LLM failed: %s, fallback rule", e)
    return _fallback_pattern(sequences)


# ---------------------------------------------------------------------------
# 3. 渲染 Skill markdown
# ---------------------------------------------------------------------------
def render_skill(
    name: str,
    domain: str,
    pattern: Dict[str, Any],
    traces_count: int,
    source: str = _SOURCE,
) -> str:
    """渲染 Skill markdown：YAML front-matter + 正文（适用场景/步骤/坑位）。

    front-matter 键：name/domain/source/traces_count/created_at/generated_by。
    """
    steps = pattern.get("steps") or []
    pitfalls = pattern.get("pitfalls") or []
    summary = (pattern.get("summary") or "").strip() or f"{domain} 领域的高频操作流程"
    generated_by = "skill_forge" + (f"/llm" if ("llm" in name or pattern.get("_by") == "llm") else "/rule")

    lines = [
        "---",
        f"name: {name}",
        f"domain: {domain}",
        f"source: {source}",
        f"traces_count: {traces_count}",
        f"created_at: {_now_iso()}",
        f"generated_by: {generated_by}",
        "---",
        "",
        f"# {name}",
        "",
        "## 适用场景",
        "",
        summary,
        "",
        "## 步骤",
        "",
    ]
    if steps:
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. {s}")
    else:
        lines.append("（无明确步骤，待人工补充）")
    lines.append("")
    lines.append("## 坑位")
    lines.append("")
    if pitfalls:
        for p in pitfalls:
            lines.append(f"- {p}")
    else:
        lines.append("（暂无已知坑位）")
    lines.append("")
    return "\n".join(lines)


def write_skill(md: str, name: str, out_dir: str) -> str:
    """把 skill markdown 写到 out_dir/{name}.md，返回写入的绝对路径。"""
    safe = safe_filename(name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, safe + ".md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    # 确认无 BOM
    with open(path, "rb") as f:
        head = f.read(3)
    if head == b"\xef\xbb\xbf":
        with open(path, "rb") as f:
            content = f.read()[3:]
        with open(path, "wb") as f:
            f.write(content)
    return os.path.abspath(path)


# ---------------------------------------------------------------------------
# 4. 记忆摘要写入
# ---------------------------------------------------------------------------
def store_skill_meta(store: Optional[Callable[..., Any]], skill_md: str) -> Optional[Any]:
    """把 skill 摘要写一条记忆（category=skill）。

    用注入的 store 函数（如 engine 的 store_memory）；store 为 None 即 dry-run 不写，
    返回 None。store 需要接受 keyword args（content/persona_id/session_id/agent_id/role/
    importance/tags/category/metadata）。
    """
    if store is None:
        return None
    name = _extract_front_matter(skill_md, "name") or "unnamed"
    domain = _extract_front_matter(skill_md, "domain") or "general"
    source = _extract_front_matter(skill_md, "source") or _SOURCE
    traces_count = _extract_front_matter(skill_md, "traces_count")
    summary = _extract_summary(skill_md)
    try:
        kwargs: Dict[str, Any] = {
            "content": f"[skill:{name}] {summary}",
            "persona_id": "default",
            "agent_id": "default",
            "role": "assistant",
            "importance": 0.6,
            "tags": ["skill", domain, "auto-generated"],
            "category": "skill",
            "metadata": {
                "skill_name": name,
                "domain": domain,
                "source": source,
                "traces_count": traces_count,
                "forge": _SOURCE,
                "created_at": _extract_front_matter(skill_md, "created_at"),
            },
        }
        return store(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("skill_meta store failed: %s", e)
        return None


def _extract_front_matter(skill_md: str, key: str) -> str:
    """从 front-matter 里抽一个简单 key: value。"""
    m = re.search(
        r"^---\s*\n(.*?)^---\s*$", skill_md, re.M | re.S
    )
    if m:
        body = m.group(1)
        for line in body.splitlines():
            if line.startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    return ""


def _extract_summary(skill_md: str) -> str:
    """从正文 适用场景 段抽摘要首句。"""
    m = re.search(r"## 适用场景\s*\n\s*\n([^\n]+)", skill_md)
    if m:
        return m.group(1).strip()[:200]
    return skill_md.strip()[:200]
