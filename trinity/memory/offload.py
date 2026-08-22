#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短期记忆 Mermaid 符号卸载（context offload，对标 TencentDB Agent Memory 的符号化记忆）。

思路：把一段任务轨迹（entries 列表）从上下文"卸载"到磁盘符号载体上——
  1. 原文逐条落盘       ~/.trinity/offload/refs/{task_id}/{node_id}.md
  2. 生成 Mermaid 画布   ~/.trinity/offload/canvases/{task_id}.mmd
     节点 = 步骤摘要/结果（label 含 node_id，便于图↔原文 drill_down 往返）
  3. 索引                ~/.trinity/offload/canvases/{task_id}.index.json
     node_id -> {ref 路径, 摘要, ts}
  4. drill_down(node_id) / search_offload(query) 反向检索原文

对标点：
  - TencentDB Agent Memory 的"符号化记忆"：把长上下文压缩成可寻址、可图解、
    可无 LLM 检索的符号节点，而不是存整段上下文。
  - 本模块是纯文件实现（贴合仓库"轨道/sidecar jsonl 落盘"传统），不进 SQLite
    运行时大库，不写 ~/.trinity/store/trinity_store.db。

路径策略（全部可用环境覆盖）：
  - TRINITY_OFFLOAD_DIR  覆盖根目录（默认 ~/.trinity/offload），测试用临时目录。
  - TRINITY_OFFLOAD_LLM  on/1/true → 摘要优先 LLM 归纳；否则规则模式（默认 off）。
  - TRINITY_LLM_API_KEY / DEEPSEEK_API_KEY 其一存在才允许 LLM 调用；无 key 降级规则。
  - TRINITY_DEBUGOFFLOAD=1   打印调试日志。

容错契约：所有公开函数对损坏文件/缺目录 try/except 降级，绝不抛异常；
          读失败返回 None / 空列表，写失败跳过该节点并继续。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.memory.offload")

# 目录布局
_REFS_DIR = "refs"
_CANVASES_DIR = "canvases"

# 规则抽取默认长度
RULE_MAX_CHARS = 80


def offload_root() -> str:
    """offload 根目录（TRINITY_OFFLOAD_DIR 覆盖，默认 ~/.trinity/offload）。"""
    return os.environ.get("TRINITY_OFFLOAD_DIR") or os.path.join(
        os.path.expanduser("~"), ".trinity", "offload"
    )


def _refs_dir() -> str:
    return os.path.join(offload_root(), _REFS_DIR)


def _canvases_dir() -> str:
    return os.path.join(offload_root(), _CANVASES_DIR)


def _debug(message: str) -> None:
    if os.environ.get("TRINITY_DEBUGOFFLOAD", "off").lower() in ("on", "1", "true", "yes"):
        print(f"[offload] {message}")


# ───────────────────────────────────────────────────────────────────────────
# 路径/命中辅助
# ───────────────────────────────────────────────────────────────────────────
def node_ref_path(task_id: str, seq: int) -> str:
    """ref 单文件路径（node_id = {task_id}:{seq} → refs/{task_id}/{seq}.md）。"""
    safe_task = _safe_component(task_id)
    return os.path.join(_refs_dir(), safe_task, f"{seq}.md")


def node_id_path(node_id: str) -> Optional[str]:
    """由 node_id（{task_id}:{seq}）反解 ref 文件路径；非法/越界返回 None。"""
    m = re.match(r"^(.+):(\d+)$", node_id)
    if not m:
        return None
    task_id, seq = m.group(1), int(m.group(2))
    return node_ref_path(task_id, seq)


def _safe_component(name: str) -> str:
    """把标识净化成安全的路径组件（防路径穿越），空值给 default。"""
    s = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]", "_", str(name or ""))
    s = s.strip("._")
    return s or "task"


def index_path(task_id: str) -> str:
    return os.path.join(_canvases_dir(), _safe_component(task_id) + ".index.json")


def canvas_path(task_id: str) -> str:
    return os.path.join(_canvases_dir(), _safe_component(task_id) + ".mmd")


# ───────────────────────────────────────────────────────────────────────────
# LLM 摘要（OpenAI 兼容 /chat/completions，deepseek-chat，无 key 降级）
# 复用 proposition_extractor 的同款 urllib 调用风格，但独立实现以免耦合。
# ───────────────────────────────────────────────────────────────────────────
def llm_enabled() -> bool:
    """TRINITY_OFFLOAD_LLM 为 on 且存在 LLM key 才算可能启用 LLM。"""
    if os.environ.get("TRINITY_OFFLOAD_LLM", "off").lower() not in ("on", "1", "true", "yes"):
        return False
    return bool(os.environ.get("TRINITY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


def _llm_summarize(content: str, node_type: str = "step") -> str:
    """用 LLM 归纳一条 content 的短摘要；任何失败都抛异常交给上层兜底。"""
    import urllib.request

    api_key = os.environ.get("TRINITY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("TRINITY_LLM_BASE_URL") or "https://api.deepseek.com/v1"
    model = os.environ.get("TRINITY_LLM_MODEL") or "deepseek-chat"
    system = (
        "你是短期记忆符号卸载器。把一条记忆轨迹节点压缩成一句话摘要（≤60字），"
        "只概括，不推断、不评价、不要 markdown。"
    )
    user = f"node_type={node_type}\ncontent:\n{(content or '')[:2000]}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    out = (body["choices"][0]["message"]["content"] or "").strip()
    return out or ""


def rule_summarize(content: str, max_chars: int = RULE_MAX_CHARS) -> str:
    """规则抽取摘要：取首句；首句过长/无标点则截断前 N 字符。"""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if not text:
        return ""
    # 优先按中英句号/感叹/问号/换行切首句
    m = re.match(r"^(.+?[。！？!?；;])", text)
    if m:
        first = m.group(1).strip()
        return first[:max_chars]
    return text[:max_chars].rstrip()


def make_summary(content: str, node_type: str = "step") -> str:
    """生成节点摘要：LLM 开启且有 key 时尝试 LLM，否则规则抽取；LLM 失败降级规则。"""
    if llm_enabled():
        try:
            out = _llm_summarize(content, node_type)
            if out:
                return out
        except Exception as e:  # pragma: no cover — 网络/解析失败
            logger.warning("offload LLM summarize failed, fallback rule: %s", e)
    return rule_summarize(content)


# ───────────────────────────────────────────────────────────────────────────
# Mermaid 画布生成
# ───────────────────────────────────────────────────────────────────────────
def _sanitize_mermaid_label(text: str, max_len: int = 120) -> str:
    """净化 Mermaid label：压缩空白、截断、转义引号/中括号。"""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if len(t) > max_len:
        t = t[:max_len].rstrip() + "…"
    t = t.replace('"', "'")
    return t


def _mermaid_node_id(seq: int, task_id: str) -> str:
    """Mermaid 内部节点名（数字开头+含冒号的 label 需加引号避坑）。"""
    return f"n{seq}_{_safe_component(task_id)[:16]}"


def render_canvas(task_id: str, nodes: List[Dict[str, Any]]) -> str:
    """把规范化节点列表渲染成 graph LR Mermaid 文本。

    node: {seq, node_id, summary, content, node_type, ts, ref}
    节点 label = 摘要 + 尾注 `[id:{node_id}]`；连续节点用 --> 串联。
    """
    lines = ["graph LR", f"    %% task_id: {_safe_component(task_id)}"]
    prev_meta = ""
    for node in nodes:
        seq = int(node.get("seq", 0))
        summary = _sanitize_mermaid_label(str(node.get("summary") or "…"))
        meta = f"[id:{node.get('node_id','')}]"
        label = f"{summary} {meta}" if summary else meta
        nid = _mermaid_node_id(seq, task_id)
        lines.append(f'    {nid}["{label}"]')
        if prev_meta:
            lines.append(f"    {prev_meta} --> {nid}")
        prev_meta = nid
    # 无节点时也补一个占位，避免空画布
    if not nodes:
        lines.append('    n0["(empty offload canvas)"]')
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────────────
# 核心：offload_task
# ───────────────────────────────────────────────────────────────────────────
def offload_task(task_id: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """卸载一个任务轨迹。

    entries: [{node_type?, summary?, content, ts?}...]
        强制字段: content
        可选字段: node_type（步骤类型，默认 "step"）、summary（优先采用）、ts
    返回: {task_id, canvas_path, node_count, nodes: [...]}
    对损坏/缺失 source 的节点跳过；整套操作 try/except，失败也返回结构。

    同 task_id 重跑 = 覆盖写：先清空该 task 的 refs 与索引/画布，再
    以本次 entries 为准重建（旧节点不再残留）。
    """
    result = {"task_id": task_id, "canvas_path": "", "node_count": 0, "nodes": []}
    try:
        safe_task = _safe_component(task_id)
        task_refs_dir = os.path.join(_refs_dir(), safe_task)
        os.makedirs(task_refs_dir, exist_ok=True)
        os.makedirs(_canvases_dir(), exist_ok=True)

        # 覆盖写：清空旧 refs（保留目录）
        try:
            for old in os.listdir(task_refs_dir):
                full = os.path.join(task_refs_dir, old)
                if os.path.isfile(full):
                    os.remove(full)
        except Exception as e:
            _debug(f"clean refs {task_refs_dir} failed: {e}")

        nodes: List[Dict[str, Any]] = []
        for seq, raw in enumerate(entries or []):
            if not isinstance(raw, dict):
                continue
            content = raw.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            node_type = str(raw.get("node_type") or "step")
            summary_given = raw.get("summary")
            ts = raw.get("ts")

            node_id = f"{safe_task}:{seq}"
            ref_path = node_ref_path(task_id, seq)

            # ① 原文落盘
            try:
                with open(ref_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                _debug(f"wrote ref {node_id} -> {ref_path} ({len(content)} chars)")
            except Exception as e:  # pragma: no cover — 权限/磁盘异常
                logger.error("offload ref write failed for %s: %s", node_id, e)
                continue

            # ③ 摘要：自带 summary > LLM/规则
            if summary_given:
                summary = str(summary_given)
            else:
                summary = make_summary(content, node_type)

            nodes.append({
                "seq": seq,
                "node_id": node_id,
                "summary": summary,
                "content": content,
                "node_type": node_type,
                "ts": ts,
                "ref": ref_path,
            })

        # ② Mermaid 画布
        mermaid = render_canvas(task_id, nodes)
        mmd_path = canvas_path(task_id)
        try:
            with open(mmd_path, "w", encoding="utf-8") as fh:
                fh.write(mermaid)
            _debug(f"wrote canvas {mmd_path} with {len(nodes)} nodes")
        except Exception as e:  # pragma: no cover
            logger.error("offload canvas write failed: %s", e)

        # ④ 索引
        index = {n["node_id"]: _entry_index_record(n) for n in nodes}
        idx_path = index_path(task_id)
        try:
            with open(idx_path, "w", encoding="utf-8") as fh:
                json.dump(index, fh, ensure_ascii=False, indent=2)
        except Exception as e:  # pragma: no cover
            logger.error("offload index write failed: %s", e)

        result.update({
            "canvas_path": mmd_path,
            "node_count": len(nodes),
            "nodes": nodes,
        })
    except Exception as e:  # pragma: no cover — 顶层兜底
        logger.error("offload_task failed: %s", e)
    return result


def _entry_index_record(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref": node.get("ref", ""),
        "summary": node.get("summary", ""),
        "ts": node.get("ts"),
        "node_type": node.get("node_type", "step"),
        "content_preview": str(node.get("content", ""))[:120],
    }


# ───────────────────────────────────────────────────────────────────────────
# 读回与检索
# ───────────────────────────────────────────────────────────────────────────
def drill_down(node_id: str) -> Optional[Dict[str, Any]]:
    """读回节点的原文 + 元信息；损坏/缺失返回 None。

    node_id 形如 {task_id}:{seq}（唯一匹配 ref 文件）。
    """
    try:
        ref = node_id_path(node_id)
        if not ref or not os.path.isfile(ref):
            return None
        with open(ref, "r", encoding="utf-8") as fh:
            content = fh.read()
        return {
            "node_id": node_id,
            "content": content,
            "ref": ref,
        }
    except Exception as e:
        logger.warning("offload drill_down failed for %s: %s", node_id, e)
        return None


def search_offload(
    query: str,
    task_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """在索引 + refs 上做关键词检索（re 匹配），返回命中 node_id 列表。

    匹配位置：
      - 索引记录（summary / content_preview）
      - ref 原文全文
    返回 [{node_id, snippet, summary, ts, ref}]，按命中拒序（索引先、任一匹配）。
    task_id 缺省时扫根目录下全部任务。
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        pattern = re.compile(re.escape(q), re.IGNORECASE)
    except Exception:  # pragma: no cover
        return []

    hits: List[Dict[str, Any]] = []
    task_ids = [task_id] if task_id else _list_task_ids()
    for tid in (task_ids or []):
        try:
            idx = _load_index(tid)
            for node_id, rec in (idx or {}).items():
                if len(hits) >= limit:
                    break
                summary = str(rec.get("summary") or "")
                preview = str(rec.get("content_preview") or "")
                snippet = None
                if pattern.search(summary) or pattern.search(preview):
                    snippet = preview or summary
                # 回退全文匹配
                if not snippet:
                    body = _read_ref_text(rec.get("ref"))
                    if body and pattern.search(body):
                        snippet = _slice_hit(body, q)
                if snippet is not None:
                    hits.append({
                        "node_id": node_id,
                        "snippet": snippet[:200],
                        "summary": summary,
                        "ts": rec.get("ts"),
                        "ref": rec.get("ref"),
                    })
        except Exception as e:  # pragma: no cover
            logger.warning("offload search index scan failed for %s: %s", tid, e)
    return hits


def _list_task_ids() -> List[str]:
    """从 refs/ 顶层目录枚举任务 id（缺目录容错）。"""
    base = _refs_dir()
    if not os.path.isdir(base):
        return []
    try:
        return [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except Exception:  # pragma: no cover
        return []


def _load_index(task_id: str) -> Optional[Dict[str, Any]]:
    """加载一个 task 的索引 JSON；损坏返回 None（降级）。"""
    path = index_path(task_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("offload index load failed for %s: %s", task_id, e)
        return None


def _read_ref_text(ref: Optional[str]) -> Optional[str]:
    """读 ref 原文；损坏/缺失返回 None（降级）。"""
    if not ref or not os.path.isfile(ref):
        return None
    try:
        with open(ref, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def _slice_hit(text: str, query: str) -> str:
    """截取 query 命中位置附近的片段；query 缺失时取开头。"""
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[:200]
    start = max(0, idx - 40)
    piece = text[start : idx + len(query) + 60]
    if start > 0:
        piece = "…" + piece
    return piece


def get_canvas(task_id: str) -> Optional[str]:
    """返回某 task 的画布 Mermaid 文本；缺失返回 None（降级）。"""
    path = canvas_path(task_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception as e:
        logger.warning("offload canvas read failed for %s: %s", task_id, e)
        return None


__all__ = [
    "offload_root",
    "offload_task",
    "drill_down",
    "search_offload",
    "get_canvas",
    "render_canvas",
    "make_summary",
    "rule_summarize",
    "llm_enabled",
    "node_ref_path",
    "node_id_path",
]
