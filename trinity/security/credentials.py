"""Unified PG credential resolution + psycopg2.connect patch (2026-09-02).

背景：brain/*、api/* 等 90+ 模块硬编码 psycopg2.connect(host="127.0.0.1", ...,
user="trinity", password="trinity")。此处集中解析凭证并全局补丁 psycopg2.connect：
仅当调用点参数等于默认兜底值时用解析值覆盖（尊重显式定制的非默认参数）。

解析优先级：环境变量 TRINITY_PG_* → ~/.dsh/.credentials.yaml → 默认值。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

_FALLBACK: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 5432,
    "dbname": "trinity",
    "user": "trinity",
    "password": "trinity",
}
_ENV_MAP = {
    "TRINITY_PG_HOST": "host",
    "TRINITY_PG_PORT": "port",
    "TRINITY_PG_DB": "dbname",
    "TRINITY_PG_USER": "user",
    "TRINITY_PG_PASSWORD": "password",
}


def _load_yaml() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if not p.exists():
            return out
        cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
        for envk, key in _ENV_MAP.items():
            if envk in cfg:
                out[key] = int(cfg[envk]) if key == "port" else cfg[envk]
    except Exception:
        pass
    return out


def resolve_credentials() -> Dict[str, Any]:
    """返回解析后的连接参数（env → yaml → 默认）。"""
    creds = dict(_FALLBACK)
    creds.update(_load_yaml())
    for envk, key in _ENV_MAP.items():
        v = os.environ.get(envk)
        if v:
            creds[key] = int(v) if key == "port" else v
    return creds


_CREDS: Dict[str, Any] = resolve_credentials()


def pg_connect(*args: Any, **kwargs: Any):
    """psycopg2.connect with resolved credentials as defaults."""
    import psycopg2
    for k, v in _CREDS.items():
        kwargs.setdefault(k, v)
    return psycopg2.connect(*args, **kwargs)


def patch_psycopg2() -> bool:
    """全局补丁 psycopg2.connect：存量硬编码默认值自动替换为解析凭证。幂等。"""
    try:
        import psycopg2
    except Exception:
        return False
    if getattr(psycopg2.connect, "_trinity_patched", False):
        return True
    _orig = psycopg2.connect

    def _patched(*args: Any, **kwargs: Any):
        for k, v in _CREDS.items():
            cur = kwargs.get(k)
            if cur is None or cur == _FALLBACK.get(k):
                kwargs[k] = v
        return _orig(*args, **kwargs)

    _patched._trinity_patched = True  # type: ignore[attr-defined]
    psycopg2.connect = _patched
    return True


def resolve_backend() -> str:
    """TRINITY_STORAGE_BACKEND 解析：环境变量 → ~/.dsh/.credentials.yaml → ''。

    2026-09-02（API 自举）：未注入 env 时回退 credentials 文件，使
    python -m trinity.api.server 等任意入口默认走 PG 主存储而非 SQLite 镜像。
    """
    v = os.environ.get("TRINITY_STORAGE_BACKEND", "").strip().lower()
    if v:
        return v
    # 2026-09-02 fix：TRINITY_DB_PATH / TRINITY_STORE 显式指定 SQLite 时不得被 yaml 的 PG 后端覆盖
    # （pytest fixture 与 LongMemEval runner 只设其一即表达 SQLite 隔离意图；缺守卫曾致
    #  基准 ingest 误写 PG 主库 lme_* agent 8,029 条污染——见第 23 轮）
    if os.environ.get("TRINITY_DB_PATH") or os.environ.get("TRINITY_STORE"):
        return ""
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
            return str(cfg.get("TRINITY_STORAGE_BACKEND", "")).strip().lower()
    except Exception:
        pass
    return ""


_patch_applied: bool = patch_psycopg2()
