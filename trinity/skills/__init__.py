# -*- coding: utf-8 -*-
"""Trinity Skills Runtime — 借鉴 DSH（DeepSeek Harness）skill 机制（Phase 3）。

把 trinity/data/skills/*.md（带 YAML frontmatter）变成可发现、可按需加载、
可检索匹配的技能资产：
  - list_skills()     技能注册表（name/description/when_to_use）
  - load_skill(name)  加载技能内容
  - match_skills(q)   按查询关键词匹配技能（供 agent 检索时附技能提示）

frontmatter 格式:
    ---
    name: trinity-ops
    description: 一句话描述
    when_to_use: 何时使用
    ---
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.skills")

# data/skills 位于项目根（trinity/data/skills）
_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "skills",
)

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", re.MULTILINE)


def _parse_frontmatter(text: str) -> Dict[str, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}
    meta = {}
    for key, value in _KEY_RE.findall(m.group(1)):
        meta[key.strip()] = value.strip().strip("'\"")
    return meta


def _strip_frontmatter(text: str) -> str:
    return _FM_RE.sub("", text, count=1).lstrip()


def _skills_dir() -> str:
    # 运行时解析（测试可覆盖 TRINITY_HOME 无关；固定项目路径）
    return _SKILLS_DIR


def list_skills() -> List[Dict[str, Any]]:
    """技能注册表（读 data/skills/*.md 的 frontmatter）。"""
    out = []
    try:
        for fname in sorted(os.listdir(_skills_dir())):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(_skills_dir(), fname)
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            meta = _parse_frontmatter(text)
            out.append({
                "name": meta.get("name") or fname[:-3],
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
                "file": fname,
            })
    except Exception as exc:
        logger.warning("skills list failed: %s", exc)
    return out


def load_skill(name: str) -> Optional[Dict[str, Any]]:
    """按名字加载技能（含 frontmatter 元数据 + 正文内容）。"""
    for fname in os.listdir(_skills_dir()):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(_skills_dir(), fname)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        meta = _parse_frontmatter(text)
        if meta.get("name") == name or fname[:-3] == name:
            return {
                "name": meta.get("name") or fname[:-3],
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
                "content": _strip_frontmatter(text),
            }
    return None


_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "trinity"}


def _terms(text: str) -> set:
    words = set()
    try:
        import jieba
        for t in jieba.cut(text or ""):
            t = t.strip().lower()
            if len(t) >= 2:
                words.add(t)
    except Exception:
        pass
    words |= set(re.findall(r"[a-z0-9]{2,}", (text or "").lower()))
    return {w for w in words if w not in _STOP}


def match_skills(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """按查询关键词匹配技能（name/description/when_to_use/标题）。"""
    q = _terms(query)
    if not q:
        return []
    scored = []
    for skill in list_skills():
        hay = " ".join([skill.get("name", ""), skill.get("description", ""),
                        skill.get("when_to_use", "")])
        hay_terms = _terms(hay)
        score = len(q & hay_terms)
        # 查询含技能名 → 高权重
        if q & {skill.get("name", "").lower()}:
            score += 3
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top_k]]
