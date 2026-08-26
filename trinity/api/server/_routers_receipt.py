# -*- coding: utf-8 -*-
"""审计回执路由（2026-08-24, R9 后续 P1-④：可证明记忆 AgentPrizm 对齐）。

"证明 agent 记住了什么"——基于现有 SHA-256 审计链 + 版本链封装
**可对账回执（receipt）**：给定 memory_id，返回
  - 当前内容哈希（sha256_hash，基于明文）
  - 版本链摘要（memory_versions 数量 + 首末版本哈希）
  - 审计链摘要（动作序列 + 链式 checksum 完整性验证结果）
  - 对账校验公式（验证者可独立重算）

用途：企业合规/审计场景"证明某记忆在某时间存在且未被篡改"；
与 docs/EVAL_CREDIBILITY.md 的可证明性方向一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ._deps import get_memory

logger = logging.getLogger("trinity.api.audit_receipt")

router = APIRouter()


def _content_sha256(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def build_receipt(mem, memory_id: str) -> Dict[str, Any]:
    """构建记忆回执（纯函数逻辑，便于测试与复用）。

    Args:
        mem: Trinity 实例（_adapter 提供审计/版本查询）。
        memory_id: 目标记忆。

    Returns:
        receipt dict；记忆不存在时抛 LookupError。
    """
    adapter = getattr(mem, "_adapter", None)
    if adapter is None:
        raise RuntimeError("engine unavailable (no adapter)")

    memory = adapter.get_memory(memory_id)
    if not memory:
        raise LookupError(f"memory not found: {memory_id}")

    # 1) 当前状态哈希（基于明文，与去重/一致性链同源）
    content = str(memory.get("content") or "")
    current_hash = _content_sha256(content)
    stored_hash = memory.get("sha256_hash") or ""

    # 2) 版本链摘要
    versions: List[Dict[str, Any]] = []
    try:
        versions = adapter.get_version_chain(memory_id) or []
    except Exception:
        versions = []
    version_count = len(versions)
    first_ver = versions[0] if versions else None
    last_ver = versions[-1] if versions else None

    # 3) 审计链摘要（该记忆的全部动作）
    audit_entries: List[Dict[str, Any]] = []
    try:
        audit_entries = adapter.get_audit_trail(memory_id) or []
    except Exception:
        audit_entries = []
    audit_actions = [a.get("action", "") for a in audit_entries]

    # 4) 审计链完整性（全链校验——该记忆是否处于未篡改链中）
    integrity: Dict[str, Any] = {"checked": False, "ok": None, "detail": ""}
    try:
        res = adapter.verify_audit_integrity()
        integrity = {
            "checked": True,
            "ok": bool(res.get("integrity_ok")),
            "detail": res.get("details", ""),
        }
    except Exception as exc:
        integrity = {"checked": True, "ok": False, "detail": f"verify failed: {exc}"}

    return {
        "schema": "trinity-receipt-v1",
        "memory_id": memory_id,
        "status": str(memory.get("status") or "unknown"),
        "current_hash": current_hash,
        "stored_hash": stored_hash,
        "hash_match": bool(stored_hash) and current_hash == stored_hash,
        "version_count": version_count,
        "first_version": {
            "version_id": (first_ver or {}).get("version_id") or (first_ver or {}).get("id"),
            "sha256_hash": (first_ver or {}).get("sha256_hash") or (first_ver or {}).get("content_hash"),
        } if first_ver else None,
        "last_version": {
            "version_id": (last_ver or {}).get("version_id") or (last_ver or {}).get("id"),
            "sha256_hash": (last_ver or {}).get("sha256_hash") or (last_ver or {}).get("content_hash"),
        } if last_ver else None,
        "audit_actions": audit_actions,
        "audit_count": len(audit_entries),
        "audit_integrity": integrity,
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "verify_hint": (
            "independent verification: recompute sha256(plaintext content) and compare "
            "with current_hash; run GET /audit/integrity for full-chain check"
        ),
    }


@router.get("/audit/receipt/{memory_id}", tags=["Audit"], summary="可证明记忆回执")
async def audit_receipt(memory_id: str):
    """返回指定记忆的可对账回执（AgentPrizm 式可证明记忆）。

    - current_hash：当前内容 SHA-256（验证者可独立重算）；
    - version_count / 首末版本哈希：变更历史摘要；
    - audit_actions / audit_integrity：审计链动作与完整性校验结果；
    - hash_match：存储哈希与重算哈希是否一致（篡改检测）。
    """
    mem = get_memory()
    try:
        return build_receipt(mem, memory_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/audit/integrity", tags=["Audit"], summary="审计链完整性校验")
async def audit_integrity():
    """全审计链完整性校验（篡改检测）——回执的对账依据。"""
    mem = get_memory()
    adapter = getattr(mem, "_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="engine unavailable (no adapter)")
    try:
        return adapter.verify_audit_integrity()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
