# -*- coding: utf-8 -*-
"""行级可见性规则 — 借鉴 Budibase Row-Level Security（Phase 3，默认关闭）。

在 persona/agent/tenant 身份隔离之上，提供记忆**行级**规则过滤：
表达式作用于 memories 元数据字段，白名单字段 + 参数化值（防注入）。

语法（AND 组合，Phase 1 不支持 OR）::

    category != 'lme' AND importance >= 0.6 AND tags CONTAINS 'wms'

算子: =  !=  >  >=  <  <=  IN  NOT_IN  CONTAINS
字段: category importance tags persona_id agent_id session_id modality status

用法::

    from trinity.security.visibility import to_sql, matches
    where, params = to_sql("importance >= 0.6 AND category IN ('decision','wms_knowledge')")
    ok = matches({"importance": 0.8, "category": "decision"}, "importance >= 0.6")
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

ALLOWED_FIELDS = {
    "category", "importance", "tags", "persona_id", "agent_id",
    "session_id", "modality", "status",
}
_OPS = ("NOT_IN", "IN", "CONTAINS", ">=", "<=", "!=", ">", "<", "=")

_TOKEN_RE = re.compile(
    r"\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<op>NOT_IN|IN|CONTAINS|>=|<=|!=|>|<|=)\s*"
    r"(?P<value>'(?:[^'\\]|\\')*'|\"(?:[^\"\\]|\\\")*\"|[\w.\-]+)\s*"
    r"(?P<and>AND)?\s*",
    re.IGNORECASE,
)


class VisibilityError(ValueError):
    pass


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _coerce(v: str) -> Any:
    v = _strip_quotes(v)
    try:
        return float(v)
    except ValueError:
        pass
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
    return v


def parse_visibility(expr: str) -> List[Tuple[str, str, Any]]:
    """解析表达式 → [(field, op, value), ...]；空串/None → []。

    按顶层 " AND " 切分（值内不允许出现裸 AND），逐条件匹配
    字段/算子/值；IN/NOT_IN 支持 ('a','b') 列表值。
    """
    if not expr or not str(expr).strip():
        return []
    s = str(expr).strip()
    conds: List[Tuple[str, str, Any]] = []
    for part in re.split(r"\s+AND\s+", s, flags=re.IGNORECASE):
        part = part.strip()
        if not part:
            continue
        m = re.match(
            r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*"
            r"(?P<op>NOT_IN|IN|CONTAINS|>=|<=|!=|>|<|=)\s*"
            r"(?P<rest>.+?)\s*$",
            part, re.IGNORECASE,
        )
        if not m:
            raise VisibilityError(f"visibility parse error near: {part[:40]!r}")
        field = m.group("field").lower()
        op = m.group("op").upper()
        if field not in ALLOWED_FIELDS:
            raise VisibilityError(f"field not allowed: {field}")
        if op not in _OPS:
            raise VisibilityError(f"op not allowed: {op}")
        rest = m.group("rest").strip()
        if op in ("IN", "NOT_IN"):
            if not (rest.startswith("(") and rest.endswith(")")):
                raise VisibilityError(f"IN/NOT_IN needs (..) list: {rest[:40]!r}")
            inner = rest[1:-1]
            items = []
            for tok in inner.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                items.append(_coerce(tok))
            value: Any = items
        else:
            # 单值必须是 引号串/数字/裸词（无空格）——尾随垃圾拒绝
            if not re.match(r"^('(?:[^'\\]|\\')*'|\"(?:[^\"\\]|\\\")*\"|[\w.\-]+)$", rest):
                raise VisibilityError(f"invalid value: {rest[:40]!r}")
            value = _coerce(rest)
        conds.append((field, op, value))
    return conds


def _sql_cond(field: str, op: str, value: Any) -> Tuple[str, Any]:
    if op in ("IN", "NOT_IN"):
        items = value if isinstance(value, list) else [value]
        placeholders = ", ".join("?" for _ in items)
        sql = f"{field} IN ({placeholders})" if op == "IN" else f"{field} NOT IN ({placeholders})"
        return sql, tuple(items)
    if op == "CONTAINS":
        return f"tags LIKE ?", f'%"{value}"%'
    if op == "=":
        return f"{field} = ?", value
    if op == "!=":
        return f"{field} != ?", value
    return f"{field} {op} ?", value


def to_sql(expr: str) -> Tuple[Optional[str], Tuple[Any, ...]]:
    """表达式 → (WHERE 片段, 参数元组)；无规则返回 (None, ())。"""
    conds = parse_visibility(expr)
    if not conds:
        return None, ()
    parts, params = [], []
    for field, op, value in conds:
        sql, p = _sql_cond(field, op, value)
        parts.append(sql)
        if isinstance(p, tuple):
            params.extend(p)
        else:
            params.append(p)
    return " AND ".join(parts), tuple(params)


def matches(record: Dict[str, Any], expr: str) -> bool:
    """Python 侧行级匹配（后置过滤用）。"""
    conds = parse_visibility(expr)
    for field, op, value in conds:
        actual = record.get(field)
        if field == "tags":
            actual = record.get("tags") or []
            if isinstance(actual, str):
                import json as _json
                try:
                    actual = _json.loads(actual)
                except Exception:
                    actual = []
        if op == "=":
            if str(actual) != str(value):
                return False
        elif op == "!=":
            if str(actual) == str(value):
                return False
        elif op in (">", ">=", "<", "<="):
            try:
                a, b = float(actual), float(value)
            except (TypeError, ValueError):
                return False
            if op == ">" and not a > b:
                return False
            if op == ">=" and not a >= b:
                return False
            if op == "<" and not a < b:
                return False
            if op == "<=" and not a <= b:
                return False
        elif op == "IN":
            wanted = value if isinstance(value, list) else [value]
            if actual not in wanted:
                return False
        elif op == "NOT_IN":
            wanted = value if isinstance(value, list) else [value]
            if actual in wanted:
                return False
        elif op == "CONTAINS":
            if str(value) not in [str(t) for t in actual]:
                return False
    return True
