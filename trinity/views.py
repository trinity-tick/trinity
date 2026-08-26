# -*- coding: utf-8 -*-
"""Memory Views — 借鉴 Budibase 表视图的声明式命名检索视图（Phase 2，默认关闭）。

views.yaml（~/.trinity/views.yaml）定义命名视图，search(view="name") 单参数展开：

    views:
      wms-decision:
        categories: [decision, wms_knowledge]
        tags: [wms]
        min_importance: 0.6
        sort: importance        # importance | recency | relevance（默认保持检索序）
        top_k: 10
      recent-sessions:
        personas: [default]
        sort: recency
        top_k: 20

安全：只读配置；显式 search 参数优先于视图缺省；视图不存在时返回 None（调用方忽略）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.views")

_VIEWS_FILE = os.path.join(
    os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity")), "views.yaml"
)

_VIEW_CACHE: Dict[str, Dict[str, Any]] = {}
_VIEW_MTIME: float = 0.0
_VIEW_LOCK = threading.Lock()

_ALLOWED_KEYS = {"categories", "tags", "personas", "min_importance", "sort", "top_k", "visibility"}


def load_views(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """加载 views.yaml（带 mtime 缓存；文件变更自动重载）。"""
    global _VIEW_CACHE, _VIEW_MTIME
    if not os.path.exists(_VIEWS_FILE):
        return {}
    try:
        mtime = os.path.getmtime(_VIEWS_FILE)
    except Exception:
        mtime = 0.0
    if not force and _VIEW_CACHE and abs(mtime - _VIEW_MTIME) < 1e-6:
        return _VIEW_CACHE
    with _VIEW_LOCK:
        try:
            import yaml
            with open(_VIEWS_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            views: Dict[str, Dict[str, Any]] = {}
            for name, spec in (data.get("views") or {}).items():
                if not isinstance(spec, dict):
                    continue
                views[str(name)] = {k: v for k, v in spec.items() if k in _ALLOWED_KEYS}
            _VIEW_CACHE = views
            _VIEW_MTIME = mtime
            return views
        except Exception as exc:
            logger.warning("views load failed: %s", exc)
            return {}


def resolve(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """按名字取视图；不存在返回 None。"""
    if not name:
        return None
    return load_views().get(name)


def _parse_tags(tags: Any) -> List[str]:
    if isinstance(tags, list):
        return [str(t) for t in tags]
    if isinstance(tags, str):
        import json as _json
        try:
            parsed = _json.loads(tags)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except Exception:
            pass
    return []


def apply_view(results: List[Dict[str, Any]], view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """视图后置过滤/排序（不改动原结果 dict）。"""
    out = list(results)
    view_cats = view.get("categories") or []
    if view_cats:
        wanted = {str(c).lower() for c in view_cats}
        out = [r for r in out if str(r.get("category") or "").lower() in wanted]
    view_personas = view.get("personas") or []
    if view_personas:
        wanted = {str(p).lower() for p in view_personas}
        out = [r for r in out if str(r.get("persona_id") or "default").lower() in wanted]
    view_tags = view.get("tags") or []
    if view_tags:
        wanted = {str(t).lower() for t in view_tags}
        out = [r for r in out if wanted & {t.lower() for t in _parse_tags(r.get("tags"))}]
    min_imp = view.get("min_importance")
    if min_imp is not None:
        out = [r for r in out if float(r.get("importance") or r.get("importance_score") or 0.0) >= float(min_imp)]
    sort = view.get("sort")
    if sort == "importance":
        out.sort(key=lambda r: float(r.get("importance") or r.get("importance_score") or 0.0), reverse=True)
    elif sort == "recency":
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    top_k = view.get("top_k")
    if top_k:
        out = out[: int(top_k)]
    return out
